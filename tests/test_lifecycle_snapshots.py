"""Snapshot publication uses only reviewed bytes and leaves no partial object."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from skills_auditor.cli import directory_tree_hash
from skills_auditor.lifecycle import snapshots
from skills_auditor.lifecycle.common import LifecycleError


class SnapshotFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "candidate"
        self.source.mkdir()
        (self.source / "SKILL.md").write_text("# reviewed H1\n", encoding="utf-8")
        (self.source / "scripts").mkdir()
        (self.source / "scripts" / "run").write_bytes(b"#!/bin/sh\nexit 0\n")
        (self.source / "scripts" / "run").chmod(0o751)
        self.store = self.root / "store" / "sha256"

    def materialize(self, **kwargs):
        manifest = snapshots.inspect_source(self.source)
        return snapshots.materialize(
            self.source, self.store, manifest["source_tree_sha256"],
            manifest["snapshot_tree_sha256"], **kwargs,
        )

    def assert_no_object(self):
        if self.store.exists():
            self.assertEqual(list(self.store.iterdir()), [])


class TestSnapshotMaterialization(SnapshotFixture):
    def test_exact_source_and_intentionally_normalized_snapshot_are_distinct(self):
        before = directory_tree_hash(self.source)
        source_mode = (self.source / "scripts" / "run").stat().st_mode
        manifest = snapshots.inspect_source(self.source)
        self.assertEqual(manifest["source_tree_sha256"], before)
        self.assertEqual(manifest["normalization"], "remove-write-bits/v1")
        self.assertNotEqual(manifest["snapshot_tree_sha256"], before)
        descriptor = self.materialize()
        tree = Path(descriptor["path"])
        self.assertEqual(tree, self.store / manifest["snapshot_tree_sha256"] / "tree")
        self.assertEqual(directory_tree_hash(tree), manifest["snapshot_tree_sha256"])
        self.assertEqual(directory_tree_hash(self.source), before)
        self.assertEqual((self.source / "scripts" / "run").stat().st_mode, source_mode)
        self.assertEqual(stat.S_IMODE((tree / "scripts" / "run").stat().st_mode), 0o551)
        self.assertEqual(stat.S_IMODE(tree.stat().st_mode) & 0o222, 0)
        self.assertNotEqual((tree / "SKILL.md").stat().st_ino,
                            (self.source / "SKILL.md").stat().st_ino)
        self.assertEqual(snapshots.verify_snapshot(descriptor), descriptor)
        (self.source / "SKILL.md").write_text("# unapproved H2", encoding="utf-8")
        self.assertEqual((tree / "SKILL.md").read_text(), "# reviewed H1\n")
        self.assertEqual(snapshots.verify_snapshot(descriptor), descriptor)

    def test_valid_existing_object_is_reused_without_replacing_its_inode(self):
        first = self.materialize()
        tree = Path(first["path"])
        before = tree.stat()
        second = self.materialize()
        self.assertEqual(second, first)
        self.assertEqual(tree.stat().st_ino, before.st_ino)
        self.assertFalse(list(self.store.glob(".stage-*")))

    def test_different_source_write_bits_share_normalized_content_identity(self):
        first = self.materialize()
        (self.source / "SKILL.md").chmod(0o666)
        second = self.materialize()
        self.assertNotEqual(first["source_tree_sha256"], second["source_tree_sha256"])
        self.assertEqual(first["snapshot_tree_sha256"], second["snapshot_tree_sha256"])
        self.assertEqual(first["path"], second["path"])
        self.assertEqual(snapshots.verify_snapshot(second), second)

    def test_internal_relative_file_and_directory_links_are_preserved(self):
        (self.source / "run").symlink_to("scripts/run")
        (self.source / "tools").symlink_to("scripts", target_is_directory=True)
        tree = Path(self.materialize()["path"])
        self.assertEqual(os.readlink(tree / "run"), "scripts/run")
        self.assertEqual((tree / "run").read_bytes(), (self.source / "scripts/run").read_bytes())
        self.assertEqual(os.readlink(tree / "tools"), "scripts")

    def test_symlink_modes_survive_nondefault_umask_without_changing_source(self):
        link = self.source / "run"
        previous_umask = os.umask(0o077)
        try:
            link.symlink_to("scripts/run")
        finally:
            os.umask(previous_umask)
        supports_link_modes = os.chmod in os.supports_follow_symlinks
        if supports_link_modes:
            os.chmod(link, 0o700, follow_symlinks=False)
            self.assertEqual(stat.S_IMODE(link.lstat().st_mode), 0o700)
        else:
            # Linux uses fixed 0777 symlink modes and ignores umask for links.
            self.assertEqual(stat.S_IMODE(link.lstat().st_mode), 0o777)
        source_hash = directory_tree_hash(self.source)
        link_before = link.lstat()
        target_before = (self.source / "scripts" / "run").stat()
        previous_umask = os.umask(0o027)
        try:
            descriptor = self.materialize()
        finally:
            os.umask(previous_umask)
        tree = Path(descriptor["path"])
        self.assertEqual(stat.S_IMODE((tree / "run").lstat().st_mode),
                         stat.S_IMODE(link_before.st_mode))
        self.assertEqual(os.readlink(tree / "run"), "scripts/run")
        self.assertEqual(link.lstat().st_ino, link_before.st_ino)
        self.assertEqual(link.lstat().st_mode, link_before.st_mode)
        self.assertEqual((self.source / "scripts" / "run").stat().st_mode,
                         target_before.st_mode)
        self.assertEqual(directory_tree_hash(self.source), source_hash)
        self.assertEqual(directory_tree_hash(tree), descriptor["snapshot_tree_sha256"])
        self.assertEqual(snapshots.verify_snapshot(descriptor), descriptor)

    def test_failed_link_mode_preservation_is_explicit_and_preserves_source(self):
        link = self.source / "run"
        link.symlink_to("scripts/run")
        if os.chmod not in os.supports_follow_symlinks:
            # Fixed-mode Linux links need no chmod; this is the compatible path.
            self.assertEqual(stat.S_IMODE(link.lstat().st_mode), 0o777)
            self.assertEqual(snapshots.verify_snapshot(self.materialize())["normalization"],
                             snapshots.NORMALIZATION)
            return
        os.chmod(link, 0o700, follow_symlinks=False)
        source_hash = directory_tree_hash(self.source)
        actual_chmod = os.chmod
        for failure in ("unsupported", "ignored"):
            with self.subTest(failure=failure):
                calls = []

                def chmod(path, mode, *args, **kwargs):
                    if Path(path).is_symlink():
                        self.assertFalse(kwargs["follow_symlinks"])
                        self.assertNotEqual(Path(path), link)
                        calls.append(Path(path))
                        if failure == "unsupported":
                            raise NotImplementedError("no symlink modes on this filesystem")
                        return None
                    return actual_chmod(path, mode, *args, **kwargs)

                previous_umask = os.umask(0o022)
                try:
                    with patch.object(snapshots.os, "chmod", side_effect=chmod):
                        with self.assertRaises(LifecycleError) as caught:
                            self.materialize()
                finally:
                    os.umask(previous_umask)
                self.assertEqual(caught.exception.code, "snapshot_unsupported")
                self.assertEqual(len(calls), 1)
                self.assertEqual(directory_tree_hash(self.source), source_hash)
                self.assert_no_object()

    def test_corrupt_existing_object_is_never_overwritten(self):
        descriptor = self.materialize()
        tree = Path(descriptor["path"])
        payload = tree / "SKILL.md"
        payload.chmod(0o644)
        payload.write_text("tampered", encoding="utf-8")
        with self.assertRaises(LifecycleError) as caught:
            self.materialize()
        self.assertEqual(caught.exception.code, "snapshot_corrupt")
        self.assertEqual(payload.read_text(), "tampered")
        self.assertFalse(list(self.store.glob(".stage-*")))

    def test_distinct_corrupt_bytes_report_existing_actual_hash_without_rescan(self):
        descriptor = self.materialize()
        tree = Path(descriptor["path"])
        payload = tree / "SKILL.md"
        observed = []
        for content in ("corrupt H2", "different corrupt H3"):
            payload.chmod(0o644)
            payload.write_text(content)
            payload.chmod(0o444)
            actual = directory_tree_hash(tree)
            with patch.object(snapshots, "_scan", wraps=snapshots._scan) as scan:
                with self.assertRaises(LifecycleError) as caught:
                    snapshots.verify_snapshot(descriptor)
            scan.assert_called_once_with(tree)
            self.assertEqual(caught.exception.details["expected"], descriptor["snapshot_tree_sha256"])
            self.assertEqual(caught.exception.details["actual"], actual)
            self.assertEqual(caught.exception.details["path"], str(tree))
            self.assertNotIn(content, str(caught.exception.details))
            observed.append(actual)
        self.assertNotEqual(*observed)

    def test_source_changes_after_review_fail_without_store_publication(self):
        manifest = snapshots.inspect_source(self.source)
        (self.source / "SKILL.md").write_text("H2", encoding="utf-8")
        with self.assertRaises(LifecycleError) as caught:
            snapshots.materialize(self.source, self.store,
                                  manifest["source_tree_sha256"],
                                  manifest["snapshot_tree_sha256"])
        self.assertEqual(caught.exception.code, "snapshot_source_changed")
        self.assert_no_object()

    def test_changed_source_and_staged_clone_are_checked_again_before_publish(self):
        for mutation in ("source", "clone"):
            with self.subTest(mutation=mutation):
                original = (self.source / "SKILL.md").read_bytes()

                def change(name, details):
                    if name == "snapshot_copied":
                        base = self.source if mutation == "source" else Path(details["stage"]) / "tree"
                        (base / "SKILL.md").write_text("concurrent H2", encoding="utf-8")

                with self.assertRaises(LifecycleError) as caught:
                    self.materialize(checkpoint=change)
                self.assertEqual(caught.exception.code, "snapshot_source_changed")
                self.assert_no_object()
                (self.source / "SKILL.md").write_bytes(original)

    def test_io_and_checkpoint_failures_cleanup_only_the_owned_staging_tree(self):
        for failure in ("copy", "fsync", "publish", "checkpoint"):
            with self.subTest(failure=failure):
                self.store.mkdir(parents=True, exist_ok=True)
                foreign = self.store / ".stage-foreign"
                foreign.mkdir(exist_ok=True)
                (foreign / "keep").write_text("other transaction")

                def fail_checkpoint(name, details):
                    if failure == "checkpoint" and name == "snapshot_staged":
                        raise OSError("injected checkpoint failure")

                target = {"copy": "_copy_tree", "fsync": "_sync_tree",
                          "publish": "_publish"}.get(failure)
                if target:
                    with patch.object(snapshots, target, side_effect=OSError("injected " + failure)):
                        with self.assertRaises(LifecycleError) as caught:
                            self.materialize()
                else:
                    with self.assertRaises(LifecycleError) as caught:
                        self.materialize(checkpoint=fail_checkpoint)
                self.assertEqual(caught.exception.code, "snapshot_write_failed")
                self.assertEqual((foreign / "keep").read_text(), "other transaction")
                self.assertEqual(list(self.store.iterdir()), [foreign])

    def test_cleanup_error_preserves_primary_failure_and_reports_stage_path(self):
        with patch.object(snapshots, "_copy_tree", side_effect=OSError("copy denied")), \
                patch.object(snapshots, "_remove_stage", side_effect=OSError("cleanup denied")):
            with self.assertRaises(LifecycleError) as caught:
                self.materialize()
        self.assertEqual(caught.exception.code, "snapshot_write_failed")
        self.assertIn("copy denied", str(caught.exception))
        details = str(caught.exception.details)
        self.assertIn("cleanup denied", details)
        stages = list(self.store.glob(".stage-*"))
        self.assertEqual(len(stages), 1)
        self.assertIn(str(stages[0]), details)
        self.assertFalse([p for p in self.store.iterdir() if not p.name.startswith(".stage-")])

    def test_racing_valid_publication_reuses_winner_without_overwrite(self):
        winners = []

        def publish_first(name, details):
            if name == "snapshot_durable":
                winners.append(self.materialize())

        descriptor = self.materialize(checkpoint=publish_first)
        self.assertEqual(descriptor, winners[0])
        self.assertEqual(len(list(self.store.iterdir())), 1)
        self.assertEqual(snapshots.verify_snapshot(descriptor), descriptor)

    def test_empty_foreign_digest_directory_is_preserved_at_publication_race(self):
        foreign = []

        def occupy(name, details):
            if name == "snapshot_durable":
                destination = Path(details["path"]).parent
                destination.mkdir()
                foreign.append((destination, destination.stat().st_ino))

        with self.assertRaises(LifecycleError) as caught:
            self.materialize(checkpoint=occupy)
        self.assertEqual(caught.exception.code, "snapshot_corrupt")
        self.assertEqual(foreign[0][0].stat().st_ino, foreign[0][1])
        self.assertEqual(list(foreign[0][0].iterdir()), [])
        self.assertFalse(list(self.store.glob(".stage-*")))

    def test_durable_clone_tamper_and_cleanup_failure_are_not_hidden(self):
        def change(name, details):
            if name == "snapshot_durable":
                payload = Path(details["stage"]) / "tree" / "SKILL.md"
                payload.chmod(0o644)
                payload.write_text("unapproved bytes")

        with patch.object(snapshots, "_remove_stage", side_effect=OSError("retain stage")):
            with self.assertRaises(LifecycleError) as caught:
                self.materialize(checkpoint=change)
        self.assertEqual(caught.exception.code, "snapshot_source_changed")
        self.assertIn("retain stage", str(caught.exception.details))
        self.assertFalse([p for p in self.store.iterdir() if not p.name.startswith(".stage-")])

    def test_post_rename_sync_failure_reports_uncertainty_but_preserves_complete_object(self):
        real_rename, real_fsync = os.rename, os.fsync
        renamed = []

        def rename(source, destination):
            result = real_rename(source, destination)
            if Path(source).name.startswith(".stage-"):
                renamed.append(Path(destination))
            return result

        def sync(fd):
            if renamed:
                raise OSError("injected store durability failure")
            return real_fsync(fd)

        with patch.object(snapshots.os, "rename", side_effect=rename), \
                patch.object(snapshots.os, "fsync", side_effect=sync):
            with self.assertRaises(LifecycleError) as caught:
                self.materialize()
        self.assertEqual(caught.exception.code, "snapshot_publish_uncertain")
        self.assertTrue(caught.exception.details["published"])
        self.assertTrue((renamed[0] / "tree").is_dir())
        self.assertFalse(list(self.store.glob(".stage-*")))
        descriptor = self.materialize()
        self.assertEqual(snapshots.verify_snapshot(descriptor), descriptor)

    def test_partial_and_zero_progress_copy_writes_have_exact_outcomes(self):
        actual_write = os.write
        with patch.object(snapshots.os, "write", side_effect=lambda fd, data: actual_write(fd, data[:2])):
            descriptor = self.materialize()
        self.assertEqual(snapshots.verify_snapshot(descriptor), descriptor)
        (self.source / "SKILL.md").write_text("second candidate")
        with patch.object(snapshots.os, "write", return_value=0):
            with self.assertRaises(LifecycleError) as caught:
                self.materialize()
        self.assertEqual(caught.exception.code, "snapshot_write_failed")
        self.assertEqual(snapshots.verify_snapshot(descriptor), descriptor)
        self.assertFalse(list(self.store.glob(".stage-*")))


class TestSnapshotValidation(SnapshotFixture):
    def test_absolute_escaping_dangling_and_looping_links_fail_closed(self):
        cases = (str(self.source / "SKILL.md"), "../candidate/SKILL.md", "missing",
                 "link", ".", "scripts/..")
        for target in cases:
            with self.subTest(target=target):
                link = self.source / "link"
                link.symlink_to(target)
                with self.assertRaises(LifecycleError) as caught:
                    snapshots.inspect_source(self.source)
                self.assertEqual(caught.exception.code, "snapshot_unsafe_source")
                self.assert_no_object()
                link.unlink()

    def test_directory_alias_cycle_and_non_directory_traversal_are_rejected(self):
        (self.source / "second").mkdir()
        (self.source / "scripts" / "next").symlink_to("../second")
        (self.source / "second" / "next").symlink_to("../scripts")
        with self.assertRaises(LifecycleError):
            snapshots.inspect_source(self.source)
        (self.source / "second" / "next").unlink()
        (self.source / "bad").symlink_to("SKILL.md/child")
        with self.assertRaises(LifecycleError):
            snapshots.inspect_source(self.source)

    def test_fifo_and_symlink_source_root_are_not_read(self):
        fifo = self.source / "fifo"
        os.mkfifo(fifo)
        with self.assertRaises(LifecycleError) as caught:
            snapshots.inspect_source(self.source)
        self.assertEqual(caught.exception.code, "snapshot_unsafe_source")
        fifo.unlink()
        alias = self.root / "alias"
        alias.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(LifecycleError):
            snapshots.inspect_source(alias)

    def test_source_store_overlap_is_rejected_in_either_direction(self):
        manifest = snapshots.inspect_source(self.source)
        for store in (self.source, self.source / "nested", self.root):
            with self.subTest(store=store), self.assertRaises(LifecycleError) as caught:
                snapshots.materialize(self.source, store, manifest["source_tree_sha256"],
                                      manifest["snapshot_tree_sha256"])
            self.assertEqual(caught.exception.code, "snapshot_path_overlap")
        self.assertFalse((self.source / "nested").exists())

    def test_malformed_descriptor_and_missing_or_foreign_object_fail_closed(self):
        descriptor = self.materialize()
        for key, value in (("snapshot_tree_sha256", "../escape"),
                           ("source_tree_sha256", "not-a-digest"),
                           ("normalization", "unreviewed"),
                           ("path", str(self.source))):
            with self.subTest(key=key), self.assertRaises(LifecycleError):
                snapshots.verify_snapshot(dict(descriptor, **{key: value}))
        manifest = Path(descriptor["path"]).parent / "manifest.json"
        manifest.chmod(0o644)
        manifest.write_text("{}")
        with self.assertRaises(LifecycleError) as caught:
            snapshots.verify_snapshot(descriptor)
        self.assertEqual(caught.exception.code, "snapshot_corrupt")

    def test_metadata_special_files_oversize_and_invalid_stage_provenance_fail_closed(self):
        descriptor = self.materialize()
        parent = Path(descriptor["path"]).parent
        parent.chmod(0o755)
        manifest = parent / "manifest.json"
        old = manifest.read_bytes()
        manifest.unlink()
        os.mkfifo(manifest)
        with self.assertRaises(LifecycleError) as caught:
            snapshots.verify_snapshot(descriptor)
        self.assertEqual(caught.exception.code, "snapshot_corrupt")
        manifest.unlink()
        manifest.write_bytes(b"x" * 4097)
        with self.assertRaises(LifecycleError):
            snapshots.verify_snapshot(descriptor)
        manifest.write_bytes(old)
        stage = parent / "stage.json"
        stage.chmod(0o644)
        stage.write_text("[]")
        with self.assertRaises(LifecycleError) as caught:
            snapshots.verify_snapshot(descriptor)
        self.assertEqual(caught.exception.code, "snapshot_corrupt")

    def test_writable_root_and_unsafe_link_tampering_fail_verification(self):
        descriptor = self.materialize()
        tree = Path(descriptor["path"])
        tree.chmod(0o755)
        with self.assertRaises(LifecycleError):
            snapshots.verify_snapshot(descriptor)
        (tree / "bad").symlink_to("../outside")
        tree.chmod(0o555)
        with self.assertRaises(LifecycleError) as caught:
            snapshots.verify_snapshot(descriptor)
        self.assertEqual(caught.exception.code, "snapshot_corrupt")

    def test_missing_source_invalid_hash_and_store_file_fail_without_publication(self):
        manifest = snapshots.inspect_source(self.source)
        with self.assertRaises(LifecycleError) as caught:
            snapshots.materialize(self.source / "missing", self.store,
                                  manifest["source_tree_sha256"], manifest["snapshot_tree_sha256"])
        self.assertEqual(caught.exception.code, "snapshot_unsafe_source")
        for invalid in ({}, {"path": "/tmp/foreign"}):
            with self.assertRaises(LifecycleError) as caught:
                snapshots.verify_snapshot(invalid)
            self.assertEqual(caught.exception.code, "snapshot_invalid_descriptor")
        with self.assertRaises(LifecycleError) as caught:
            snapshots.materialize(self.source, self.store, "../invalid", "invalid")
        self.assertEqual(caught.exception.code, "snapshot_invalid_descriptor")
        self.store.parent.mkdir()
        self.store.write_text("foreign file")
        with self.assertRaises(LifecycleError) as caught:
            self.materialize()
        self.assertEqual(caught.exception.code, "snapshot_write_failed")
        self.assertEqual(self.store.read_text(), "foreign file")

    def test_alias_to_absolute_link_and_symlink_store_are_rejected(self):
        (self.source / "a-alias").symlink_to("z-absolute")
        (self.source / "z-absolute").symlink_to(self.source / "SKILL.md")
        with self.assertRaises(LifecycleError):
            snapshots.inspect_source(self.source)
        (self.source / "a-alias").unlink()
        (self.source / "z-absolute").unlink()
        self.store.parent.mkdir()
        actual_store = self.root / "actual-store"
        actual_store.mkdir()
        self.store.symlink_to(actual_store, target_is_directory=True)
        with self.assertRaises(LifecycleError) as caught:
            self.materialize()
        self.assertEqual(caught.exception.code, "snapshot_unsafe_source")
        self.assertEqual(list(actual_store.iterdir()), [])

    def test_source_entry_replacement_and_in_place_mutation_during_read_are_detected(self):
        actual_open, actual_read, actual_readlink = os.open, os.read, os.readlink
        for scenario in ("file_open", "directory_open", "file_read", "link_read", "directory_scan"):
            with self.subTest(scenario=scenario):
                link = self.source / "link"
                link.symlink_to("SKILL.md")
                mutated = []

                def open_entry(path, flags, *args, **kwargs):
                    if not mutated and path == "SKILL.md" and scenario == "file_open":
                        (self.source / "SKILL.md").write_text("different size")
                        mutated.append(True)
                    if not mutated and path == "scripts" and scenario == "directory_open":
                        (self.source / "scripts" / "new").write_text("new entry")
                        mutated.append(True)
                    return actual_open(path, flags, *args, **kwargs)

                def read(fd, size):
                    result = actual_read(fd, size)
                    if not mutated and scenario == "file_read":
                        (self.source / "SKILL.md").write_text("changed during read")
                        mutated.append(True)
                    if not mutated and scenario == "directory_scan":
                        (self.source / "added").write_text("added during scan")
                        mutated.append(True)
                    return result

                def readlink(path, *args, **kwargs):
                    result = actual_readlink(path, *args, **kwargs)
                    if not mutated and scenario == "link_read":
                        link.unlink()
                        link.symlink_to("scripts/run")
                        mutated.append(True)
                    return result

                with patch.object(snapshots.os, "open", side_effect=open_entry), \
                        patch.object(snapshots.os, "read", side_effect=read), \
                        patch.object(snapshots.os, "readlink", side_effect=readlink):
                    with self.assertRaises(LifecycleError) as caught:
                        snapshots.inspect_source(self.source)
                self.assertEqual(caught.exception.code, "snapshot_unsafe_source")
                self.assertTrue(mutated)
                link.unlink()
                (self.source / "SKILL.md").write_text("# reviewed H1\n")
                (self.source / "scripts" / "new").unlink(missing_ok=True)
                (self.source / "added").unlink(missing_ok=True)

    def test_store_lock_contention_and_unsupported_platform_fail_clearly(self):
        self.store.mkdir(parents=True)
        with snapshots.locked_store(self.store):
            started = time.monotonic()
            with self.assertRaises(LifecycleError) as caught:
                with snapshots.locked_store(self.store, timeout=0):
                    self.fail("competing writer acquired the same store")
            self.assertEqual(caught.exception.code, "snapshot_store_busy")
            self.assertLess(time.monotonic() - started, 1)
        with snapshots.locked_store(self.store, timeout=0):
            pass
        with patch.object(snapshots, "fcntl", None):
            with self.assertRaises(LifecycleError) as caught:
                self.materialize()
        self.assertEqual(caught.exception.code, "snapshot_unsupported")
        self.assert_no_object()


class TestSnapshotCrashRecovery(SnapshotFixture):
    def test_process_death_leaves_only_discoverable_stage_or_complete_object(self):
        code = """
import json, os, sys
from pathlib import Path
from skills_auditor.lifecycle.snapshots import inspect_source, materialize
source, store, boundary = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
manifest = inspect_source(source)
def die(name, details):
    if name == boundary:
        os._exit(73)
materialize(source, store, manifest['source_tree_sha256'],
            manifest['snapshot_tree_sha256'], checkpoint=die)
"""
        for boundary in ("snapshot_staged", "snapshot_copied", "snapshot_durable",
                         "snapshot_published"):
            with self.subTest(boundary=boundary):
                store = self.root / boundary
                result = subprocess.run([sys.executable, "-c", code, str(self.source),
                                         str(store), boundary], capture_output=True, text=True)
                self.assertEqual(result.returncode, 73, result.stderr)
                manifest = snapshots.inspect_source(self.source)
                final = store / manifest["snapshot_tree_sha256"] / "tree"
                if boundary == "snapshot_published":
                    self.assertTrue(final.is_dir())
                    snapshots.verify_snapshot(dict(manifest, path=str(final)))
                else:
                    self.assertFalse(final.exists())
                    stages = list(store.glob(".stage-*"))
                    self.assertEqual(len(stages), 1)
                    intent = json.loads((stages[0] / "stage.json").read_text())
                    self.assertEqual(intent["snapshot_tree_sha256"], manifest["snapshot_tree_sha256"])
                # Retry never activates an incomplete stage and returns complete bytes.
                descriptor = snapshots.materialize(self.source, store,
                    manifest["source_tree_sha256"], manifest["snapshot_tree_sha256"])
                self.assertEqual(snapshots.verify_snapshot(descriptor), descriptor)

    def test_two_processes_publish_one_complete_object_without_overwriting(self):
        code = """
import json, sys, time
from pathlib import Path
from skills_auditor.lifecycle.snapshots import inspect_source, materialize
source, store, barrier, name = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]
manifest = inspect_source(source)
def rendezvous(event, details):
    if event == 'snapshot_durable':
        (barrier / name).write_text('ready')
        deadline = time.monotonic() + 10
        while len(list(barrier.iterdir())) < 2:
            if time.monotonic() > deadline:
                raise RuntimeError('barrier timed out')
            time.sleep(0.01)
print(json.dumps(materialize(source, store, manifest['source_tree_sha256'],
                 manifest['snapshot_tree_sha256'], checkpoint=rendezvous)))
"""
        barrier = self.root / "barrier"
        barrier.mkdir()
        processes = [subprocess.Popen([sys.executable, "-c", code, str(self.source),
                                      str(self.store), str(barrier), str(index)],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                     for index in range(2)]
        try:
            outputs = [process.communicate(timeout=15) for process in processes]
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.wait()
        for process, (_, error) in zip(processes, outputs):
            self.assertEqual(process.returncode, 0, error)
        first, second = (json.loads(output) for output, _ in outputs)
        self.assertEqual(first, second)
        self.assertEqual(snapshots.verify_snapshot(first), first)
        self.assertEqual(len(list(self.store.iterdir())), 1)
