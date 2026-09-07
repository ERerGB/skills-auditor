"""Legacy primitives cannot mutate recognized managed data or owned entries."""

from contextlib import redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import tempfile
import unittest
import unicodedata
from unittest.mock import patch

from skills_auditor.cli import DedupAction, SyncAction, apply_actions, apply_dedup, apply_route, collect_metadata_repair_actions, main, repair_skill_metadata
from skills_auditor.integration import IntegrationError, IntegrationSpec, IntegrationTarget, _atomic_write_json, apply_integration_plan, build_integration_plan
from skills_auditor.lifecycle.engine import Manager
from skills_auditor.lifecycle.guards import ManagedBoundaryError, assert_legacy_mutation_allowed


class TestLifecycleGuards(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="skills-auditor-managed-guards-")
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name).resolve()
        self.source = self.project / "candidate"
        self.source.mkdir()
        (self.source / "SKILL.md").write_text("# Example\n")
        self.host = self.project / "host"
        self.host.mkdir()
        self.target = self.host / "managed"
        self.manager = Manager(self.project)
        self.addCleanup(self.manager.repository.close)
        plan = self.manager.plan("install", source=self.source, target=self.target)
        self.receipt = self.manager.apply(plan, approve_plan_id=plan["plan_id"])
        self.original_link = os.readlink(self.target)
        self.original_bytes = (self.target / "SKILL.md").read_bytes()

    def assert_unchanged(self):
        self.assertEqual(os.readlink(self.target), self.original_link)
        self.assertEqual((self.target / "SKILL.md").read_bytes(), self.original_bytes)
        self.assertEqual(self.manager.repository.get("receipt", self.receipt["receipt_id"])["data"], self.receipt)

    def test_direct_paths_aliases_and_registry_owned_drift_are_recognized(self):
        for path in (self.target, self.target / "SKILL.md", self.target.resolve(), self.manager.state_root / "state.sqlite3", self.host):
            with self.subTest(path=path):
                with self.assertRaises(ManagedBoundaryError):
                    assert_legacy_mutation_allowed([path])
        self.target.unlink()
        self.target.write_text("foreign drift")
        with self.assertRaises(ManagedBoundaryError):
            assert_legacy_mutation_allowed([self.target])
        self.assertEqual(self.target.read_text(), "foreign drift")

    def test_metadata_dry_run_is_read_only_but_apply_is_rejected(self):
        planned = repair_skill_metadata(self.target / "SKILL.md", apply=False)
        self.assertEqual(planned[0].action, "repair")
        with self.assertRaises(ManagedBoundaryError):
            repair_skill_metadata(self.target / "SKILL.md", apply=True)
        normal = self.host / "a-normal"
        normal.mkdir()
        (normal / "SKILL.md").write_text("# Needs metadata\n")
        with self.assertRaises(ManagedBoundaryError):
            collect_metadata_repair_actions(self.host, apply=True)
        self.assertEqual((normal / "SKILL.md").read_text(), "# Needs metadata\n")
        self.assert_unchanged()

    def test_case_and_unicode_aliases_of_managed_entries_and_state_are_rejected(self):
        self.target.unlink()
        self.target.write_text("ordinary drift")
        for alias in (self.project / "HOST" / "MANAGED", self.project / ".SKILLS-AUDITOR-LOCAL" / "LIFECYCLE" / "state.sqlite3"):
            with self.subTest(alias=alias):
                with self.assertRaises(ManagedBoundaryError):
                    assert_legacy_mutation_allowed([alias], project_root=self.project)
        accented = self.host / "caf\u00e9"
        plan = self.manager.plan("install", source=self.source, target=accented)
        self.manager.apply(plan, approve_plan_id=plan["plan_id"])
        accented.unlink()
        accented.write_text("unicode drift")
        with self.assertRaises(ManagedBoundaryError):
            assert_legacy_mutation_allowed([self.host / unicodedata.normalize("NFD", accented.name)], project_root=self.project)
        self.assertEqual(self.target.read_text(), "ordinary drift")
        self.assertEqual(accented.read_text(), "unicode drift")
        self.assertEqual(self.manager.repository.get("receipt", self.receipt["receipt_id"])["data"], self.receipt)

    def test_whole_explicit_project_and_its_alias_cannot_be_archived_or_deleted(self):
        alias = self.project.parent / self.project.name.upper()
        for project in ((self.project, alias) if alias.exists() else (self.project,)):
            with self.subTest(project=project):
                with self.assertRaises(ManagedBoundaryError):
                    assert_legacy_mutation_allowed([project], project_root=self.project.parent)
        for action in ("archive", "delete"):
            with self.subTest(action=action):
                with patch("pathlib.Path.rename", side_effect=AssertionError("guard must precede rename")), patch("shutil.rmtree", side_effect=AssertionError("guard must precede delete")):
                    with self.assertRaises(ManagedBoundaryError):
                        apply_route([DedupAction("project", "project", str(self.source), str(self.project), action, "fixture")], self.project.parent)
                self.assert_unchanged()

    def test_sync_dedup_and_route_check_the_entire_batch_before_first_effect(self):
        first = self.host / "first"
        first.symlink_to(self.source, target_is_directory=True)
        first_before = first.lstat()
        sync = [SyncAction("first", str(self.source), "replace_link", "fixture"), SyncAction("managed", str(self.source), "replace_link", "fixture")]
        with self.assertRaises(ManagedBoundaryError):
            apply_actions(self.host, sync)
        normal_file = self.project / "normal.md"
        normal_file.write_text("normal")
        actions = [DedupAction("b", "normal", str(self.source / "SKILL.md"), str(normal_file), "relink", "fixture"),
                   DedupAction("b", "managed", str(self.source / "SKILL.md"), str(self.target / "SKILL.md"), "relink", "fixture")]
        for operation in (lambda: apply_dedup(actions), lambda: apply_route(actions, self.host)):
            with self.assertRaises(ManagedBoundaryError):
                operation()
        self.assertEqual(first.lstat().st_ino, first_before.st_ino)
        self.assertFalse(normal_file.is_symlink())
        self.assertEqual(normal_file.read_text(), "normal")
        self.assert_unchanged()

    def test_route_delete_and_archive_cannot_remove_a_managed_parent(self):
        for action in ("delete", "archive"):
            with self.subTest(action=action):
                with self.assertRaises(ManagedBoundaryError):
                    apply_route([DedupAction("b", "managed", str(self.source), str(self.host), action, "fixture")], self.project)
                self.assert_unchanged()

    def test_archive_destinations_are_guarded_before_any_batch_side_effect(self):
        for primitive in ("sync", "route"):
            with self.subTest(primitive=primitive):
                duplicate = self.host / (primitive + "-legacy")
                duplicate.write_text("legacy bytes")
                archive = self.host / (duplicate.name + ".archived-fixed")
                plan = self.manager.plan("install", source=self.source, target=archive)
                receipt = self.manager.apply(plan, approve_plan_id=plan["plan_id"])
                archive.unlink()  # Registered target is missing, not unowned.
                earlier = self.host / (primitive + "-earlier")
                earlier.symlink_to(self.source, target_is_directory=True)
                inode = earlier.lstat().st_ino
                with patch("skills_auditor.cli.datetime") as clock:
                    clock.now.return_value.strftime.return_value = "fixed"
                    with self.assertRaises(ManagedBoundaryError):
                        if primitive == "sync":
                            apply_actions(self.host, [SyncAction(earlier.name, str(self.source), "replace_link", "fixture"), SyncAction(duplicate.name, str(self.source), "archive_and_link", "fixture")])
                        else:
                            apply_route([DedupAction("first", "first", str(self.source), str(earlier), "delete", "fixture"), DedupAction("last", "last", str(self.source), str(duplicate), "archive", "fixture")], self.host)
                self.assertEqual(earlier.lstat().st_ino, inode)
                self.assertEqual(duplicate.read_text(), "legacy bytes")
                self.assertFalse(archive.is_symlink() or archive.exists())
                self.assertEqual(self.manager.repository.get("receipt", receipt["receipt_id"])["data"], receipt)

    def test_legacy_plan_receipt_output_and_apply_respect_managed_boundary(self):
        with self.assertRaises(IntegrationError) as caught:
            _atomic_write_json(self.target / "SKILL.md", {"unsafe": True})
        self.assertEqual(caught.exception.code, "managed_boundary")
        sources = self.project / "legacy-sources"
        skill = sources / "managed"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: managed\ndescription: legacy fixture\n---\n# Legacy\n")
        spec = IntegrationSpec(project_root=self.project, sources=(sources,), targets=(IntegrationTarget("fixture", root=self.host),))
        plan = build_integration_plan(spec)
        with self.assertRaises(IntegrationError) as caught:
            apply_integration_plan(plan)
        self.assertEqual(caught.exception.code, "managed_boundary")
        self.assert_unchanged()

    def test_nonmanaged_legacy_writes_and_noop_primitives_are_unchanged(self):
        normal_host = self.project / "normal-host"
        normal_host.mkdir()
        apply_actions(normal_host, [SyncAction("normal", str(self.source), "create_link", "fixture")])
        self.assertTrue((normal_host / "normal").is_symlink())
        repaired = repair_skill_metadata(self.source / "SKILL.md", apply=True)
        self.assertEqual(repaired[0].action, "repair")
        apply_actions(self.host, [SyncAction("managed", self.original_link, "noop", "fixture")])
        self.assertEqual(apply_dedup([]), 0)
        self.assertEqual(apply_route([], self.host), 0)
        self.assert_unchanged()

    def test_default_legacy_receipt_location_is_guarded_before_first_effect(self):
        receipts = self.project / ".skills-auditor-local" / "receipts"
        plan = self.manager.plan("install", source=self.source, target=receipts)
        receipt = self.manager.apply(plan, approve_plan_id=plan["plan_id"])
        before = receipts.lstat().st_ino
        sources = self.project / "default-receipt-sources"
        skill = sources / "normal"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: normal\ndescription: fixture\n---\n# Normal\n")
        host = self.project / "default-receipt-host"
        spec = IntegrationSpec(project_root=self.project, sources=(sources,), targets=(IntegrationTarget("fixture", root=host),))
        legacy = build_integration_plan(spec)
        with self.assertRaises(IntegrationError) as caught:
            apply_integration_plan(legacy)
        self.assertEqual(caught.exception.code, "managed_boundary")
        self.assertFalse(host.exists())
        self.assertEqual(receipts.lstat().st_ino, before)
        self.assertEqual(self.manager.repository.get("receipt", receipt["receipt_id"])["data"], receipt)
        self.assert_unchanged()

    def test_legacy_integration_guards_receipt_and_archive_before_writes_and_preserves_input_errors(self):
        sources = self.project / "batch-sources"
        skill = sources / "normal"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: normal\ndescription: fixture\n---\n# Normal\n")
        host = self.project / "batch-host"
        host.mkdir()
        spec = IntegrationSpec(project_root=self.project, sources=(sources,), targets=(IntegrationTarget("fixture", root=host),))
        create = build_integration_plan(spec)
        with self.assertRaises(IntegrationError) as caught:
            apply_integration_plan(create, receipt_output=self.target / "SKILL.md")
        self.assertEqual(caught.exception.code, "managed_boundary")
        self.assertFalse((host / "normal").exists())
        malformed = dict(create, plan_id="bad-checksum")
        with self.assertRaises(IntegrationError) as caught:
            apply_integration_plan(malformed, receipt_output=self.target / "SKILL.md")
        self.assertNotEqual(caught.exception.code, "managed_boundary")
        (host / "normal").mkdir()
        (host / "normal" / "old").write_text("old contents")
        archive_plan = build_integration_plan(spec)
        archive = Path(archive_plan["targets"][0]["actions"][0]["archive_path"])
        managed = self.manager.plan("install", source=self.source, target=archive)
        self.manager.apply(managed, approve_plan_id=managed["plan_id"])
        archive.unlink()
        with self.assertRaises(IntegrationError) as caught:
            apply_integration_plan(archive_plan)
        self.assertEqual(caught.exception.code, "managed_boundary")
        self.assertEqual((host / "normal" / "old").read_text(), "old contents")
        self.assertFalse(archive.exists() or archive.is_symlink())
        self.assert_unchanged()

    def test_late_receipt_guard_failure_does_not_hide_original_operation_failure(self):
        sources = self.project / "error-sources"
        skill = sources / "normal"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: normal\ndescription: fixture\n---\n# Normal\n")
        host = self.project / "error-host"
        spec = IntegrationSpec(project_root=self.project, sources=(sources,), targets=(IntegrationTarget("fixture", root=host),))
        plan = build_integration_plan(spec)
        primary = IntegrationError("fixture_operation_failed", "primary failure must remain available")
        secondary = IntegrationError("managed_boundary", "receipt destination changed after preflight")
        with patch("skills_auditor.integration._apply_exact_action", side_effect=primary), patch("skills_auditor.integration._atomic_write_json", side_effect=secondary):
            with self.assertRaises(IntegrationError) as caught:
                apply_integration_plan(plan)
        self.assertEqual(caught.exception.code, "apply_failed_without_receipt")
        self.assertIn("primary failure must remain available", str(caught.exception.details))
        self.assertIn("managed_boundary", str(caught.exception.details))
        self.assertFalse((host / "normal").is_symlink())
        self.assert_unchanged()

    def test_cli_managed_apply_errors_are_stable_and_no_traceback(self):
        for command in ("metadata-repair", "dedup", "route"):
            with self.subTest(command=command):
                argv = ["skills-audit", command, "--skills-dir", str(self.host), "--apply"]
                if command == "route":
                    argv.extend(["--platform", "codex", "--trace-dir", str(self.project / "traces")])
                out, err = io.StringIO(), io.StringIO()
                with patch("sys.argv", argv), redirect_stdout(out), redirect_stderr(err):
                    result = main()
                self.assertEqual(result, 3)
                self.assertIn("managed_boundary", err.getvalue())
                self.assertNotIn("Traceback", err.getvalue())
                self.assert_unchanged()
