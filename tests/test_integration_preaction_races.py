"""Reject target and archive races after the real global apply preflight."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from skills_auditor.integration import (
    IntegrationError,
    IntegrationSpec,
    IntegrationTarget,
    apply_integration_plan,
    build_integration_plan,
    check_plan_preconditions,
    entry_snapshot,
    verify_receipt,
)


class TestIntegrationPreactionRaces(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="skills-auditor-preaction-")
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name).resolve()
        self.source = self.project / "source"
        self.canonical = self.write_skill(self.source, "canonical")
        self.target = self.project / "target"
        self.entry = self.target / "alpha"
        self.spec = IntegrationSpec(
            project_root=self.project,
            sources=(self.source,),
            targets=(IntegrationTarget("test-host", root=self.target),),
        )

    def write_skill(self, root: Path, body: str) -> Path:
        skill = root / "alpha"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\nname: alpha\ndescription: per-action race fixture\n---\n\n" + body + "\n",
            encoding="utf-8",
        )
        (skill / "payload.txt").write_text(body + " payload\n", encoding="utf-8")
        return skill

    def tree_snapshot(self, root: Path) -> dict:
        return {
            str(path.relative_to(root)): (
                path.stat().st_mode,
                path.stat().st_mtime_ns,
                path.read_bytes() if path.is_file() else None,
            )
            for path in [root, *root.rglob("*")]
        }

    def link_snapshot(self) -> tuple:
        stat = self.entry.lstat()
        return (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, os.readlink(self.entry))

    def assert_failed_receipt(
        self, error: IntegrationError, path: Path, plan: dict, expected_detail: dict
    ) -> None:
        self.assertEqual(error.code, "stale_plan")
        self.assertEqual(error.exit_code, 3)
        self.assertEqual(error.details, [expected_detail, {"receipt_path": str(path)}])
        failed = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(failed["plan_id"], plan["plan_id"])
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["results"], [])
        self.assertEqual(failed["error"]["code"], "stale_plan")
        self.assertEqual(failed["error"]["details"], [expected_detail])
        verification = verify_receipt(failed)
        self.assertEqual(verification["status"], "failed")
        self.assertEqual(
            verification["approval"],
            {
                "state": "invalidated",
                "requires_reapproval": True,
                "reason_codes": ["receipt_not_completed"],
            },
        )

    def test_noop_target_retargeted_after_preflight_is_preserved_and_rejected(self) -> None:
        _, original_path = apply_integration_plan(build_integration_plan(self.spec))
        original_receipt_bytes = original_path.read_bytes()
        plan = build_integration_plan(self.spec)
        action = plan["targets"][0]["actions"][0]
        self.assertEqual(action["action"], "noop")
        alternative = self.write_skill(self.project / "alternative", "external update")
        source_before = self.tree_snapshot(self.canonical)
        alternative_before = self.tree_snapshot(alternative)
        changed_link = []

        def retarget_after_preflight(reviewed_plan: dict) -> list:
            issues = check_plan_preconditions(reviewed_plan)
            self.assertEqual(issues, [])
            self.entry.unlink()
            self.entry.symlink_to(alternative, target_is_directory=True)
            changed_link.append(self.link_snapshot())
            return issues

        failed_path = self.project / "target-race-receipt.json"
        self.assertFalse(failed_path.exists())
        with patch(
            "skills_auditor.integration.check_plan_preconditions",
            side_effect=retarget_after_preflight,
        ) as preflight, patch("skills_auditor.integration._apply_exact_action") as apply_action:
            with self.assertRaises(IntegrationError) as caught:
                apply_integration_plan(plan, failed_path)
        preflight.assert_called_once_with(plan)
        apply_action.assert_not_called()

        self.assert_failed_receipt(
            caught.exception,
            failed_path,
            plan,
            {
                "code": "target_changed",
                "target": str(self.target),
                "name": "alpha",
                "expected": action["entry_before"],
                "actual": entry_snapshot(self.entry),
            },
        )
        self.assertTrue(self.entry.is_symlink())
        self.assertEqual(self.entry.resolve(), alternative)
        self.assertEqual(self.link_snapshot(), changed_link[0])
        self.assertEqual(self.tree_snapshot(alternative), alternative_before)
        self.assertEqual(self.tree_snapshot(self.canonical), source_before)
        self.assertEqual(original_path.read_bytes(), original_receipt_bytes)

    def test_reserved_archive_occupied_after_preflight_preserves_both_directories(self) -> None:
        native = self.write_skill(self.target, "native user content")
        plan = build_integration_plan(self.spec)
        action = plan["targets"][0]["actions"][0]
        self.assertEqual(action["action"], "archive_and_link")
        archive = Path(action["archive_path"])
        self.assertFalse(archive.exists())
        native_before = self.tree_snapshot(native)
        source_before = self.tree_snapshot(self.canonical)
        occupied_archive = []

        def occupy_after_preflight(reviewed_plan: dict) -> list:
            issues = check_plan_preconditions(reviewed_plan)
            self.assertEqual(issues, [])
            archive.mkdir()
            (archive / "preserve.txt").write_text("concurrent user content\n", encoding="utf-8")
            occupied_archive.append(self.tree_snapshot(archive))
            return issues

        failed_path = self.project / "archive-race-receipt.json"
        self.assertFalse(failed_path.exists())
        with patch(
            "skills_auditor.integration.check_plan_preconditions",
            side_effect=occupy_after_preflight,
        ) as preflight, patch("skills_auditor.integration._apply_exact_action") as apply_action:
            with self.assertRaises(IntegrationError) as caught:
                apply_integration_plan(plan, failed_path)
        preflight.assert_called_once_with(plan)
        apply_action.assert_not_called()

        self.assert_failed_receipt(
            caught.exception,
            failed_path,
            plan,
            {
                "code": "archive_changed",
                "name": "alpha",
                "path": str(archive),
                "expected": {"kind": "missing"},
                "actual": entry_snapshot(archive),
            },
        )
        self.assertTrue(native.is_dir())
        self.assertFalse(native.is_symlink())
        self.assertEqual(self.tree_snapshot(native), native_before)
        self.assertTrue(archive.is_dir())
        self.assertFalse(archive.is_symlink())
        self.assertEqual(self.tree_snapshot(archive), occupied_archive[0])
        self.assertEqual(self.tree_snapshot(self.canonical), source_before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
