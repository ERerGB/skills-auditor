"""Unreadable preconditions must reject apply before any filesystem mutation."""

from __future__ import annotations

import errno
import json
import os
import tempfile
import unittest
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from skills_auditor import integration
from skills_auditor.cli import main


class TestIntegrationPreflightFailures(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="skills-auditor-preflight-")
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name).resolve()
        self.source = self.project / "source"
        self.skill = self.source / "alpha"
        self.skill.mkdir(parents=True)
        (self.skill / "SKILL.md").write_text(
            "---\nname: alpha\ndescription: preflight fixture\n---\n\ncanonical\n",
            encoding="utf-8",
        )
        self.live = self.project / "live"
        initial = integration.build_integration_plan(
            integration.IntegrationSpec(
                project_root=self.project,
                sources=(self.source,),
                targets=(integration.IntegrationTarget("live", root=self.live),),
            )
        )
        self.old_receipt, self.old_receipt_path = integration.apply_integration_plan(
            initial, self.project / "old-receipt.json"
        )
        self.old_receipt_bytes = self.old_receipt_path.read_bytes()
        self.native = self.project / "native"
        native_skill = self.native / "alpha"
        native_skill.mkdir(parents=True)
        (native_skill / "SKILL.md").write_text("native content\n", encoding="utf-8")
        (native_skill / "local-only.txt").write_text("keep native data\n", encoding="utf-8")
        prior_archive = self.native / "alpha.archived-previous"
        prior_archive.mkdir()
        (prior_archive / "local-only.txt").write_text("keep archive data\n", encoding="utf-8")
        self.plan = integration.build_integration_plan(
            integration.IntegrationSpec(
                project_root=self.project,
                sources=(self.source,),
                targets=(
                    integration.IntegrationTarget("live", root=self.live),
                    integration.IntegrationTarget("native", root=self.native),
                ),
            )
        )
        self.assertEqual(self.plan["targets"][0]["actions"][0]["action"], "noop")
        archive_action = self.plan["targets"][1]["actions"][0]
        self.assertEqual(archive_action["action"], "archive_and_link")
        self.archive = Path(archive_action["archive_path"])
        self.plan_path = integration.save_plan(self.plan, self.project / "candidate-plan.json")
        self.rejected_receipt_path = self.project / "rejected-receipt.json"

    def tree_snapshot(self) -> dict:
        snapshot = {}
        for path in [self.project, *self.project.rglob("*")]:
            stat = path.lstat()
            if path.is_symlink():
                contents = ("symlink", os.readlink(path))
            elif path.is_file():
                contents = ("file", path.read_bytes())
            else:
                contents = ("directory",)
            snapshot[str(path.relative_to(self.project))] = (
                stat.st_mode, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, contents
            )
        return snapshot

    @contextmanager
    def assert_no_mutations(self):
        before = self.tree_snapshot()
        with patch(
            "skills_auditor.integration._apply_exact_action",
            wraps=integration._apply_exact_action,
        ) as action, patch(
            "skills_auditor.integration._atomic_write_json",
            wraps=integration._atomic_write_json,
        ) as receipt_writer:
            yield
            action.assert_not_called()
            receipt_writer.assert_not_called()
        self.assertEqual(self.tree_snapshot(), before)
        self.assertFalse(self.rejected_receipt_path.exists())
        self.assertFalse(self.archive.exists())
        self.assertEqual(self.old_receipt_path.read_bytes(), self.old_receipt_bytes)
        self.assertEqual(integration.verify_receipt(self.old_receipt)["status"], "passed")

    @contextmanager
    def filesystem_fault(self, operation: str):
        if operation.startswith("hash_"):
            owner, attribute, selected = integration, "directory_tree_hash", self.skill
        elif operation.startswith("is_dir_"):
            owner, attribute, selected = Path, "is_dir", self.skill
        elif operation == "target_snapshot":
            owner, attribute, selected = os, "readlink", self.live / "alpha"
        else:
            owner, attribute, selected = Path, "is_symlink", self.archive
        original = getattr(owner, attribute)

        def fail_selected(path, *args, **kwargs):
            if Path(path) == selected:
                if operation.endswith("value_error"):
                    raise ValueError("preflight probe rejected source tree")
                raise PermissionError(errno.EACCES, "preflight probe denied", str(path))
            return original(path, *args, **kwargs)

        with patch.object(owner, attribute, autospec=True, side_effect=fail_selected):
            yield

    def fault_cases(self):
        return (
            ("hash_os_error", "source_changed"),
            ("hash_value_error", "source_changed"),
            ("is_dir_os_error", "source_changed"),
            ("is_dir_value_error", "source_changed"),
            ("target_snapshot", "target_changed"),
            ("archive_snapshot", "archive_changed"),
        )

    def assert_issue(self, issue: dict, code: str) -> None:
        self.assertEqual(issue["code"], code)
        self.assertEqual(issue["name"], "alpha")
        if code == "source_changed":
            self.assertEqual(issue["path"], str(self.skill))
            self.assertEqual(issue["expected"], self.plan["source_skills"][0]["tree_sha256"])
            self.assertIsNone(issue["actual"])
        else:
            self.assertEqual(set(issue["actual"]), {"kind", "error"})
            self.assertEqual(issue["actual"]["kind"], "unreadable")
            self.assertIn("preflight probe denied", issue["actual"]["error"])
            if code == "target_changed":
                self.assertEqual(issue["target"], str(self.live))
                self.assertEqual(
                    issue["expected"], self.plan["targets"][0]["actions"][0]["entry_before"]
                )
            else:
                self.assertEqual(issue["target"], str(self.native))
                self.assertEqual(issue["path"], str(self.archive))
                self.assertEqual(issue["expected"], {"kind": "missing"})

    def assert_preflight_rejection(self, operation: str, code: str) -> None:
        with self.assert_no_mutations(), self.filesystem_fault(operation):
            issues = integration.check_plan_preconditions(self.plan)
            self.assertEqual(len(issues), 1)
            self.assert_issue(issues[0], code)
            with self.assertRaises(integration.IntegrationError) as caught:
                integration.apply_integration_plan(self.plan, self.rejected_receipt_path)
            self.assertEqual(caught.exception.code, "stale_plan")
            self.assertEqual(caught.exception.exit_code, 3)
            self.assertEqual(caught.exception.details, issues)

    def test_readable_preconditions_are_the_success_control(self) -> None:
        with self.assert_no_mutations():
            self.assertEqual(integration.check_plan_preconditions(self.plan), [])

    def test_source_read_errors_reject_before_apply(self) -> None:
        for operation, code in self.fault_cases()[:4]:
            with self.subTest(operation=operation):
                self.assert_preflight_rejection(operation, code)

    def test_target_snapshot_error_rejects_before_apply(self) -> None:
        self.assert_preflight_rejection("target_snapshot", "target_changed")

    def test_archive_snapshot_error_rejects_before_apply(self) -> None:
        self.assert_preflight_rejection("archive_snapshot", "archive_changed")

    def test_combined_read_errors_keep_all_precondition_details(self) -> None:
        with self.assert_no_mutations(), ExitStack() as faults:
            for operation in ("hash_os_error", "target_snapshot", "archive_snapshot"):
                faults.enter_context(self.filesystem_fault(operation))
            issues = integration.check_plan_preconditions(self.plan)
            codes = ["source_changed", "target_changed", "archive_changed"]
            self.assertEqual([issue["code"] for issue in issues], codes)
            for issue, code in zip(issues, codes):
                self.assert_issue(issue, code)
            with self.assertRaises(integration.IntegrationError) as caught:
                integration.apply_integration_plan(self.plan, self.rejected_receipt_path)
            self.assertEqual(caught.exception.code, "stale_plan")
            self.assertEqual(caught.exception.exit_code, 3)
            self.assertEqual(caught.exception.details, issues)

    def test_cli_json_preserves_preflight_errors_without_writes(self) -> None:
        for operation, code in self.fault_cases():
            with self.subTest(operation=operation):
                stdout, stderr = StringIO(), StringIO()
                argv = [
                    "skills-audit", "apply", str(self.plan_path),
                    "--receipt-out", str(self.rejected_receipt_path), "--format", "json",
                ]
                with self.assert_no_mutations(), self.filesystem_fault(operation), patch(
                    "sys.argv", argv
                ), redirect_stdout(stdout), redirect_stderr(stderr):
                    exit_code = main()
                self.assertEqual(exit_code, 3)
                self.assertEqual(stderr.getvalue(), "")
                error = json.loads(stdout.getvalue())
                self.assertEqual(error["schema_version"], integration.ERROR_SCHEMA)
                self.assertEqual(error["error"]["code"], "stale_plan")
                self.assertEqual(len(error["error"]["details"]), 1)
                self.assert_issue(error["error"]["details"][0], code)


if __name__ == "__main__":
    unittest.main()
