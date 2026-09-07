"""Durable managed-lifecycle records must fail closed, not disappear on failure."""

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from skills_auditor.lifecycle.common import (
    LifecycleError, atomic_json, canonical_entry, canonical_json, digest, utc_now,
)
from skills_auditor.lifecycle.repository import Repository


class TestLifecycleCommon(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def test_canonical_serialization_and_errors(self):
        self.assertEqual(canonical_json({"b": 2, "a": "好"}), '{"a":"好","b":2}')
        self.assertEqual(digest({"b": 2, "a": 1}), digest({"a": 1, "b": 2}))
        with self.assertRaises(ValueError):
            canonical_json({"not_json": float("nan")})
        error = LifecycleError("broken", "failed", details={"phase": "read"})
        self.assertEqual(error.exit_code, 3)
        self.assertEqual(error.to_dict()["code"], "broken")
        self.assertEqual(error.to_dict()["details"], {"phase": "read"})
        self.assertEqual(str(error), "failed")
        self.assertTrue(utc_now().endswith("Z"))

    def test_canonical_entry_resolves_parent_not_leaf(self):
        real = self.root / "real"
        real.mkdir()
        parent = self.root / "alias"
        parent.symlink_to(real, target_is_directory=True)
        leaf = real / "skill"
        leaf.symlink_to("missing")
        self.assertEqual(canonical_entry(parent / "skill"), real.resolve() / "skill")
        self.assertNotEqual(canonical_entry(leaf), leaf.resolve())

    def test_atomic_json_replaces_and_syncs_file_then_directory(self):
        path = self.root / "record.json"
        path.write_text("old", encoding="utf-8")
        real_fsync = os.fsync
        kinds = []

        def sync(fd):
            import stat
            kinds.append("directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
            return real_fsync(fd)

        with mock.patch("skills_auditor.lifecycle.common.os.fsync", side_effect=sync):
            self.assertEqual(atomic_json(path, {"ok": True}), path.resolve())
        self.assertEqual(json.loads(path.read_text()), {"ok": True})
        self.assertEqual(kinds, ["file", "directory"])
        self.assertEqual(list(self.root.iterdir()), [path])

    def test_atomic_json_preserves_old_file_on_staging_or_replace_failure(self):
        for failure in ("encode", "fsync", "replace"):
            with self.subTest(failure=failure):
                path = self.root / "record.json"
                path.write_text("old", encoding="utf-8")
                if failure == "encode":
                    value = {"bad": object()}
                    context = mock.patch("skills_auditor.lifecycle.common.os.replace", wraps=os.replace)
                    expected = TypeError
                else:
                    value = {"new": True}
                    context = mock.patch(
                        "skills_auditor.lifecycle.common.os." + failure,
                        side_effect=OSError("injected " + failure),
                    )
                    expected = OSError
                with context, self.assertRaises(expected):
                    atomic_json(path, value)
                self.assertEqual(path.read_text(), "old")
                self.assertEqual(list(self.root.iterdir()), [path])

    def test_atomic_json_directory_sync_failure_reports_uncertain_publication(self):
        path = self.root / "record.json"
        with mock.patch("skills_auditor.lifecycle.common.os.fsync", side_effect=[None, OSError("directory sync")]):
            with self.assertRaisesRegex(OSError, "directory sync"):
                atomic_json(path, {"new": True})
        self.assertEqual(json.loads(path.read_text()), {"new": True})
        self.assertEqual(list(self.root.iterdir()), [path])

    def test_atomic_json_cleanup_failure_preserves_primary_exception(self):
        path = self.root / "record.json"
        path.write_text("old", encoding="utf-8")
        with mock.patch("skills_auditor.lifecycle.common.os.replace", side_effect=OSError("primary")), mock.patch.object(Path, "unlink", side_effect=OSError("cleanup")):
            with self.assertRaisesRegex(OSError, "primary"):
                atomic_json(path, {"new": True})
        self.assertEqual(path.read_text(), "old")
        staging = [p for p in self.root.iterdir() if p != path]
        self.assertEqual(len(staging), 1)
        self.assertEqual(json.loads(staging[0].read_text()), {"new": True})

    def test_atomic_json_rejects_symlink_destination_without_touching_referent(self):
        original = self.root / "original"
        original.write_text("private", encoding="utf-8")
        path = self.root / "record.json"
        path.symlink_to(original)
        with self.assertRaises(LifecycleError) as caught:
            atomic_json(path, {"new": True})
        self.assertEqual(caught.exception.code, "unsafe_path")
        self.assertTrue(path.is_symlink())
        self.assertEqual(original.read_text(), "private")

    def test_atomic_json_partial_write_and_flush_failures_clean_real_staging(self):
        for phase in ("write", "flush"):
            with self.subTest(phase=phase):
                path = self.root / "record.json"
                path.write_text("old", encoding="utf-8")
                real_temporary = tempfile.NamedTemporaryFile

                class FailingHandle:
                    def __init__(self, handle):
                        self.handle = handle
                        self.name = handle.name

                    def __enter__(self):
                        return self

                    def __exit__(self, *args):
                        self.handle.close()

                    def write(self, text):
                        if phase == "write":
                            self.handle.write(text[:5])
                            raise OSError("partial write")
                        return self.handle.write(text)

                    def flush(self):
                        raise OSError("flush failure")

                with mock.patch("skills_auditor.lifecycle.common.tempfile.NamedTemporaryFile", side_effect=lambda **kwargs: FailingHandle(real_temporary(**kwargs))):
                    with self.assertRaises(OSError):
                        atomic_json(path, {"new": "approved"})
                self.assertEqual(path.read_text(), "old")
                self.assertEqual(list(self.root.iterdir()), [path])

    def test_atomic_json_temp_creation_failure_and_root_path_rejection(self):
        with mock.patch("skills_auditor.lifecycle.common.tempfile.NamedTemporaryFile", side_effect=OSError("no temporary file")):
            with self.assertRaisesRegex(OSError, "no temporary file"):
                atomic_json(self.root / "new.json", {})
        self.assertEqual(list(self.root.iterdir()), [])
        with self.assertRaises(LifecycleError):
            canonical_entry(Path("/"))

    def test_atomic_json_syncs_new_ancestry_before_file_publication(self):
        from skills_auditor.lifecycle.common import fsync_directory
        path = self.root / "new" / "nested" / "record.json"
        with mock.patch("skills_auditor.lifecycle.common.fsync_directory", wraps=fsync_directory) as sync:
            atomic_json(path, {"durable": True})
        self.assertEqual([call.args[0] for call in sync.call_args_list], [self.root / "new", self.root, self.root / "new" / "nested", self.root / "new", path.parent])
        self.assertEqual(json.loads(path.read_text()), {"durable": True})

    def test_existing_nondirectory_parent_does_not_get_replaced(self):
        parent = self.root / "not-a-directory"
        parent.write_text("preserve", encoding="utf-8")
        with self.assertRaises(NotADirectoryError):
            atomic_json(parent / "record.json", {})
        self.assertEqual(parent.read_text(), "preserve")


class TestLifecycleRepository(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "state"
        self.repo = Repository(self.root)
        self.addCleanup(self.repo.close)

    def test_create_update_compare_and_swap_and_sorted_list(self):
        self.assertIsNone(self.repo.get("skill", "missing"))
        record = self.repo.put("skill", "b", {"name": "first"})
        self.assertEqual(record, {"kind": "skill", "id": "b", "revision": 1, "data": {"name": "first"}})
        self.repo.put("skill", "a", {"name": "other"})
        self.assertEqual([r["id"] for r in self.repo.list("skill")], ["a", "b"])
        for revision in (0, 2):
            with self.subTest(revision=revision), self.assertRaises(LifecycleError) as caught:
                self.repo.put("skill", "b", {"name": "wrong"}, expected_revision=revision)
            self.assertEqual(caught.exception.code, "revision_conflict")
            self.assertEqual(self.repo.get("skill", "b"), record)
        updated = self.repo.put("skill", "b", {"name": "updated"}, expected_revision=1)
        self.assertEqual(updated["revision"], 2)
        self.assertEqual(self.repo.list("unknown"), [])

    def test_grouped_records_and_events_commit_or_rollback_together(self):
        with self.repo.atomic():
            self.repo.put("skill", "a", {"ok": True})
            event = self.repo.append_event("a", "registered", {"reference": "a"}, "human", "cli")
        self.assertEqual(event["sequence"], 1)
        self.assertEqual(event["actor"], "human")
        self.assertEqual(self.repo.events("a"), [event])
        with self.assertRaisesRegex(RuntimeError, "abort"):
            with self.repo.atomic():
                self.repo.put("skill", "a", {"ok": False}, expected_revision=1)
                self.repo.append_event("a", "changed", {}, "agent", "test")
                raise RuntimeError("abort")
        self.assertEqual(self.repo.get("skill", "a")["revision"], 1)
        self.assertEqual(self.repo.events("a"), [event])

    def test_nested_atomic_rollback_isolated_with_savepoint(self):
        with self.repo.atomic():
            self.repo.put("skill", "outer", {})
            with self.assertRaises(ValueError):
                with self.repo.atomic():
                    self.repo.put("skill", "inner", {})
                    raise ValueError("rollback inner")
            self.repo.append_event("outer", "created", {}, "human", "cli")
        self.assertIsNotNone(self.repo.get("skill", "outer"))
        self.assertIsNone(self.repo.get("skill", "inner"))
        self.assertEqual(len(self.repo.events("outer")), 1)

    def test_committed_records_and_events_survive_process_restart(self):
        self.repo.put("skill", "stable", {"name": "durable"})
        self.repo.append_event("stable", "created", {"version": "H1"}, "human", "cli")
        script = "from pathlib import Path; from skills_auditor.lifecycle.repository import Repository; import json,sys; r=Repository(Path(sys.argv[1])); print(json.dumps([r.get('skill','stable'),r.events('stable')])); r.close()"
        child = subprocess.run([sys.executable, "-c", script, str(self.root)], capture_output=True, text=True, check=True)
        record, events = json.loads(child.stdout)
        self.assertEqual(record, self.repo.get("skill", "stable"))
        self.assertEqual(events, self.repo.events("stable"))

    def test_process_death_rolls_back_uncommitted_record_and_event_batch(self):
        self.repo.put("skill", "stable", {"version": "H1"})
        script = "from pathlib import Path; from skills_auditor.lifecycle.repository import Repository; import sys; r=Repository(Path(sys.argv[1]));\nwith r.atomic():\n r.put('skill','stable',{'version':'H2'},expected_revision=1)\n r.append_event('stable','changed',{},'agent','test')\n print('staged',flush=True)\n sys.stdin.read()"
        child = subprocess.Popen([sys.executable, "-c", script, str(self.root)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "staged")
            child.kill()
            child.communicate(timeout=10)
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)
        with Repository(self.root, create=False) as restarted:
            self.assertEqual(restarted.get("skill", "stable")["data"], {"version": "H1"})
            self.assertEqual(restarted.events("stable"), [])
            restarted.put("skill", "stable", {"version": "H3"}, expected_revision=1)

    def test_separate_connections_cas_and_busy_fail_closed(self):
        with Repository(self.root) as other:
            self.repo.put("skill", "a", {})
            other.put("skill", "a", {"new": 1}, expected_revision=1)
            with self.assertRaises(LifecycleError) as caught:
                self.repo.put("skill", "a", {"stale": 1}, expected_revision=1)
            self.assertEqual(caught.exception.code, "revision_conflict")
            with self.repo.atomic():
                with self.assertRaises(LifecycleError) as busy:
                    other.put("skill", "b", {})
                self.assertEqual(busy.exception.code, "repository_busy")
        self.assertIsNone(self.repo.get("skill", "b"))

    def test_invalid_record_and_event_inputs_leave_no_partial_state(self):
        for kind, identifier, data, revision in [("", "a", {}, 0), ("skill", "", {}, 0), ("skill", "a", [], 0), ("skill", "a", {}, -1), ("skill", "a", {}, True), ("skill", "a", {"nan": float("nan")}, 0)]:
            with self.subTest(kind=kind, identifier=identifier, revision=revision), self.assertRaises(LifecycleError) as caught:
                self.repo.put(kind, identifier, data, expected_revision=revision)
            self.assertEqual(caught.exception.code, "invalid_record")
        with self.assertRaises(LifecycleError):
            self.repo.append_event("a", "changed", [], "agent", "test")
        self.assertEqual(self.repo.list("skill"), [])
        self.assertEqual(self.repo.events("a"), [])
        for reader in (lambda: self.repo.get("", "a"), lambda: self.repo.list(""), lambda: self.repo.events("")):
            with self.assertRaises(LifecycleError) as caught:
                reader()
            self.assertEqual(caught.exception.code, "invalid_record")
        with self.assertRaises(LifecycleError) as caught:
            self.repo.append_event("a", "created", {"bad": object()}, "human", "cli")
        self.assertEqual(caught.exception.code, "invalid_record")

    def test_commit_failure_rolls_back_but_post_commit_failure_is_uncertain(self):
        connection = self.repo._connection

        class CommitFault:
            def __init__(self, after):
                self.after = after

            def execute(self, statement, *args):
                if statement == "COMMIT":
                    if self.after:
                        connection.execute(statement, *args)
                    raise sqlite3.OperationalError("disk I/O failure at commit")
                return connection.execute(statement, *args)

        for after in (False, True):
            with self.subTest(after=after):
                identifier = "after" if after else "before"
                with mock.patch.object(self.repo, "_connection", CommitFault(after)):
                    with self.assertRaises(LifecycleError) as caught:
                        self.repo.put("skill", identifier, {"version": "H1"})
                self.assertEqual(caught.exception.code, "repository_io_error")
                self.assertEqual(self.repo.get("skill", identifier) is not None, after)
                self.assertEqual(self.repo._depth, 0)

    def _raw(self, sql, parameters=()):
        with sqlite3.connect(str(self.root / "state.sqlite3")) as connection:
            connection.execute(sql, parameters)

    def test_invalid_json_checksum_and_revision_fail_closed(self):
        for mutation in ("data = '{'", "data = '[]'", "data = '{}'", "revision = 0"):
            with self.subTest(mutation=mutation):
                self._raw("DELETE FROM records")
                self.repo.put("skill", "a", {"version": "H1"})
                self._raw("UPDATE records SET " + mutation)
                for read in (lambda: self.repo.get("skill", "a"), lambda: self.repo.list("skill")):
                    with self.assertRaises(LifecycleError) as caught:
                        read()
                    self.assertEqual(caught.exception.code, "repository_corrupt")

    def test_tampered_event_and_append_only_guards(self):
        self.repo.append_event("a", "created", {"version": "H1"}, "human", "cli")
        for sql in ("UPDATE events SET event_type='altered'", "DELETE FROM events"):
            with self.subTest(sql=sql), self.assertRaises(sqlite3.DatabaseError):
                self._raw(sql)
        # A local database administrator can remove guards; checksums still detect edits.
        self._raw("DROP TRIGGER events_no_update")
        self._raw("UPDATE events SET payload='{}'")
        self._raw("CREATE TRIGGER events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT, 'events are append-only'); END")
        with self.assertRaises(LifecycleError) as caught:
            self.repo.events("a")
        self.assertEqual(caught.exception.code, "repository_corrupt")

    def test_malformed_event_json_fails_closed(self):
        self.repo.append_event("a", "created", {}, "human", "cli")
        self._raw("DROP TRIGGER events_no_update")
        self._raw("UPDATE events SET payload='{' ")
        self._raw("CREATE TRIGGER events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT, 'events are append-only'); END")
        with self.assertRaises(LifecycleError) as caught:
            self.repo.events("a")
        self.assertEqual(caught.exception.code, "repository_corrupt")

    def test_corrupt_database_is_not_recreated(self):
        self.repo.close()
        path = self.root / "state.sqlite3"
        path.write_bytes(b"not a sqlite database")
        with self.assertRaises(LifecycleError) as caught:
            Repository(self.root)
        self.assertEqual(caught.exception.code, "repository_corrupt")
        self.assertEqual(path.read_bytes(), b"not a sqlite database")

    def test_empty_existing_database_is_not_silently_initialized(self):
        self.repo.close()
        path = self.root / "state.sqlite3"
        path.write_bytes(b"")
        with self.assertRaises(LifecycleError) as caught:
            Repository(self.root)
        self.assertEqual(caught.exception.code, "repository_corrupt")
        self.assertEqual(path.read_bytes(), b"")

    def test_missing_schema_and_unsupported_version_are_rejected(self):
        self.repo.close()
        self._raw("UPDATE metadata SET value='999' WHERE key='schema_version'")
        with self.assertRaises(LifecycleError) as caught:
            Repository(self.root)
        self.assertEqual(caught.exception.code, "repository_corrupt")
        self._raw("UPDATE metadata SET value='1' WHERE key='schema_version'")
        self._raw("DROP TABLE records")
        with self.assertRaises(LifecycleError):
            Repository(self.root)

    def test_changed_schema_version_is_rejected_by_already_open_readers(self):
        self.repo.put("skill", "a", {})
        self._raw("UPDATE metadata SET value='999' WHERE key='schema_version'")
        with self.assertRaises(LifecycleError) as caught:
            self.repo.get("skill", "a")
        self.assertEqual(caught.exception.code, "repository_corrupt")

    def test_named_but_inoperative_append_only_guard_is_rejected(self):
        self.repo.close()
        self._raw("DROP TRIGGER events_no_delete")
        self._raw("CREATE TRIGGER events_no_delete BEFORE DELETE ON events BEGIN SELECT 1; END")
        with self.assertRaises(LifecycleError) as caught:
            Repository(self.root)
        self.assertEqual(caught.exception.code, "repository_corrupt")

    def test_schema_with_same_columns_but_no_identity_constraint_is_rejected(self):
        self.repo.close()
        self._raw("DROP TABLE records")
        self._raw("CREATE TABLE records (kind TEXT, id TEXT, revision INTEGER, data TEXT, checksum TEXT)")
        with self.assertRaises(LifecycleError) as caught:
            Repository(self.root)
        self.assertEqual(caught.exception.code, "repository_corrupt")

    def test_unreadable_repository_reports_stable_error_without_valid_state(self):
        with mock.patch("skills_auditor.lifecycle.repository.sqlite3.connect", side_effect=sqlite3.OperationalError("unable to open database file")):
            with self.assertRaises(LifecycleError) as caught:
                Repository(self.root, create=False)
        self.assertEqual(caught.exception.code, "repository_io_error")
        with mock.patch.object(Path, "mkdir", side_effect=OSError("directory unreadable")):
            with self.assertRaises(LifecycleError) as caught:
                Repository(self.root / "new")
        self.assertEqual(caught.exception.code, "repository_io_error")

    def test_database_symlink_is_rejected(self):
        self.repo.close()
        database = self.root / "state.sqlite3"
        original = self.root / "original.sqlite3"
        database.rename(original)
        database.symlink_to(original.name)
        before = original.read_bytes()
        with self.assertRaises(LifecycleError) as caught:
            Repository(self.root)
        self.assertEqual(caught.exception.code, "unsafe_path")
        self.assertEqual(original.read_bytes(), before)

    def test_open_existing_mode_never_initializes_missing_state(self):
        missing = self.root / "not-created"
        with self.assertRaises(LifecycleError) as caught:
            Repository(missing, create=False)
        self.assertEqual(caught.exception.code, "repository_missing")
        self.assertFalse(missing.exists())
        missing.mkdir()
        with self.assertRaises(LifecycleError) as caught:
            Repository(missing, create=False)
        self.assertEqual(caught.exception.code, "repository_missing")
        self.assertEqual(list(missing.iterdir()), [])
        with Repository(self.root, create=False) as opened:
            self.assertEqual(opened.list("skill"), [])

    @staticmethod
    def _stop_initializer(child):
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)

    def _paused_initializer(self, root):
        script = "from pathlib import Path; from skills_auditor.lifecycle.repository import Repository; import sys; original=Repository._initialize;\ndef paused(self):\n print('initializing',flush=True)\n sys.stdin.read()\n original(self)\n self.put('initialization','marker',{'creator':'first'})\nRepository._initialize=paused\nwith Repository(Path(sys.argv[1])) as r:\n print('opened',flush=True)"
        child = subprocess.Popen([sys.executable, "-c", script, str(root)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self._stop_initializer, child)
        self.assertEqual(child.stdout.readline().strip(), "initializing")
        return child

    def test_killed_initializer_never_publishes_empty_final_database(self):
        root = self.root / "killed-init"
        child = self._paused_initializer(root)
        self.assertFalse((root / "state.sqlite3").exists())
        staging = list(root.glob(".state-init-*.sqlite3"))
        self.assertEqual(len(staging), 1)
        child.kill()
        child.communicate(timeout=10)
        before = staging[0].read_bytes()
        with Repository(root) as restarted:
            self.assertEqual(restarted.list("skill"), [])
            restarted.put("skill", "created-after-restart", {})
        # Recovery never guesses that another attempt's staged file is disposable.
        self.assertEqual(staging[0].read_bytes(), before)
        with Repository(root, create=False) as reopened:
            self.assertIsNotNone(reopened.get("skill", "created-after-restart"))

    def test_transient_initialization_io_failure_can_be_retried(self):
        root = self.root / "transient-init"
        with mock.patch.object(Repository, "_initialize", side_effect=OSError("transient init I/O")):
            with self.assertRaises(LifecycleError) as caught:
                Repository(root)
        self.assertEqual(caught.exception.code, "repository_io_error")
        self.assertFalse((root / "state.sqlite3").exists())
        self.assertEqual(list(root.glob(".state-init-*.sqlite3")), [])
        with Repository(root) as retried:
            self.assertEqual(retried.list("skill"), [])

    def test_concurrent_real_creators_open_the_same_initialized_database(self):
        root = self.root / "concurrent-init"
        first = self._paused_initializer(root)
        script = "from pathlib import Path; from skills_auditor.lifecycle.repository import Repository; import json,sys; print('starting',flush=True);\nwith Repository(Path(sys.argv[1])) as r:\n print(json.dumps(r.get('initialization','marker')),flush=True)"
        second = subprocess.Popen([sys.executable, "-c", script, str(root)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self._stop_initializer, second)
        self.assertEqual(second.stdout.readline().strip(), "starting")
        first.stdin.close()
        first.stdin = None
        first_output, first_error = first.communicate(timeout=10)
        second_output, second_error = second.communicate(timeout=10)
        self.assertEqual(first.returncode, 0, first_error)
        self.assertEqual(second.returncode, 0, second_error)
        self.assertEqual(first_output.strip(), "opened")
        self.assertEqual(json.loads(second_output)["data"], {"creator": "first"})
        self.assertEqual(list(root.glob(".state-init-*.sqlite3")), [])

    def test_publication_then_error_does_not_destroy_committed_initialization(self):
        root = self.root / "publication-uncertain"
        real_link = os.link

        def published_then_failed(source, destination, *args, **kwargs):
            real_link(source, destination, *args, **kwargs)
            raise OSError("uncertain post-publication outcome")

        with mock.patch("skills_auditor.lifecycle.repository.os.link", side_effect=published_then_failed):
            with self.assertRaises(LifecycleError) as caught:
                Repository(root)
        self.assertEqual(caught.exception.code, "repository_io_error")
        self.assertTrue((root / "state.sqlite3").exists())
        with Repository(root) as retried:
            self.assertEqual(retried.list("skill"), [])
        self.assertEqual(list(root.glob(".state-init-*.sqlite3")), [])

    def test_staging_sync_failure_is_retryable_without_a_final_database(self):
        root = self.root / "staging-sync-failed"
        root.mkdir()
        with mock.patch("skills_auditor.lifecycle.repository.os.fsync", side_effect=OSError("staged file sync failed")):
            with self.assertRaises(LifecycleError) as caught:
                Repository(root)
        self.assertEqual(caught.exception.code, "repository_io_error")
        self.assertFalse((root / "state.sqlite3").exists())
        self.assertEqual(list(root.glob(".state-init-*.sqlite3")), [])
        with Repository(root) as retried:
            self.assertEqual(retried.list("skill"), [])

    def test_directory_sync_failure_after_publication_is_uncertain_but_retryable(self):
        root = self.root / "published-sync-failed"
        root.mkdir()
        with mock.patch("skills_auditor.lifecycle.repository.fsync_directory", side_effect=[OSError("published directory sync failed"), None]):
            with self.assertRaises(LifecycleError) as caught:
                Repository(root)
        self.assertEqual(caught.exception.code, "repository_io_error")
        self.assertTrue((root / "state.sqlite3").exists())
        before = (root / "state.sqlite3").stat().st_ino
        with Repository(root) as retried:
            self.assertEqual(retried.list("skill"), [])
        self.assertEqual((root / "state.sqlite3").stat().st_ino, before)

    def test_staging_cleanup_failure_preserves_initialization_error_and_safe_retry(self):
        root = self.root / "cleanup-init-failed"
        with mock.patch.object(Repository, "_initialize", side_effect=OSError("primary init failure")), mock.patch.object(Path, "unlink", side_effect=OSError("cleanup failed")):
            with self.assertRaises(LifecycleError) as caught:
                Repository(root)
        self.assertIn("primary init failure", caught.exception.details["error"])
        self.assertFalse((root / "state.sqlite3").exists())
        staging = list(root.glob(".state-init-*.sqlite3"))
        self.assertEqual(len(staging), 1)
        with Repository(root) as retried:
            self.assertEqual(retried.list("skill"), [])
        self.assertTrue(staging[0].exists())

    def test_foreign_final_appearing_during_initialization_is_never_overwritten(self):
        root = self.root / "foreign-init"
        original = Repository._initialize

        def foreign_final(repository):
            original(repository)
            (root / "state.sqlite3").write_bytes(b"foreign data")

        with mock.patch.object(Repository, "_initialize", foreign_final):
            with self.assertRaises(LifecycleError) as caught:
                Repository(root)
        self.assertEqual(caught.exception.code, "repository_corrupt")
        self.assertEqual((root / "state.sqlite3").read_bytes(), b"foreign data")

    def test_event_pagination_keeps_legacy_full_stream_and_stable_sequence_cursors(self):
        first = self.repo.append_event("one", "note", {"number": 1}, "reviewer", "test")
        self.repo.append_event("other", "note", {}, "reviewer", "test")
        second = self.repo.append_event("one", "note", {"number": 2}, "reviewer", "test")
        third = self.repo.append_event("one", "note", {"number": 3}, "reviewer", "test")
        self.assertEqual(self.repo.events("one"), [first, second, third])
        self.assertEqual(self.repo.events("one", limit=2), [first, second])
        self.assertEqual(self.repo.events("one", after_sequence=first["sequence"], limit=1), [second])
        self.assertEqual(self.repo.events("one", after_sequence=second["sequence"]), [third])
        self.assertEqual(self.repo.events("one", after_sequence=third["sequence"], limit=2), [])
        self.assertEqual(self.repo.events("unknown", limit=2, after_sequence=0), [])
        self.assertEqual(self.repo.events("one", limit=2, after_sequence=10 ** 100), [])
        # Prove pagination is in SQL, not an unbounded fetch followed by slicing.
        with mock.patch.object(self.repo, "_query", wraps=self.repo._query) as query:
            self.repo.events("one", limit=1, after_sequence=first["sequence"])
        self.assertIn("sequence>?", query.call_args.args[0])
        self.assertIn("LIMIT ?", query.call_args.args[0])
        self.assertEqual(query.call_args.args[1], ["one", first["sequence"], 1])

    def test_event_pagination_rejects_noninteger_limits_and_cursors_before_querying(self):
        for limit in (True, False, 0, -1, 1001, 1.5, "1"):
            with self.subTest(limit=limit), mock.patch.object(self.repo, "_query") as query:
                with self.assertRaises(LifecycleError) as caught:
                    self.repo.events("one", limit=limit)
                self.assertEqual(caught.exception.code, "invalid_record")
                query.assert_not_called()
        for cursor in (True, False, -1, 1.5, "1"):
            with self.subTest(cursor=cursor), mock.patch.object(self.repo, "_query") as query:
                with self.assertRaises(LifecycleError):
                    self.repo.events("one", after_sequence=cursor)
                query.assert_not_called()
