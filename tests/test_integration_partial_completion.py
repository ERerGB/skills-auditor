"""A failed multi-target apply preserves completed work without claiming success."""

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
    _apply_exact_action,
    apply_integration_plan,
    build_integration_plan,
    entry_snapshot,
    verify_receipt,
)


class TestIntegrationPartialCompletion(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="skills-auditor-partial-")
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name).resolve()
        self.source = self.project / "source"
        self.canonical = self.source / "alpha"
        self.canonical.mkdir(parents=True)
        (self.canonical / "SKILL.md").write_text(
            "---\nname: alpha\ndescription: partial completion fixture\n---\n\nbody\n",
            encoding="utf-8",
        )
        (self.canonical / "payload.txt").write_text("reviewed content\n", encoding="utf-8")
        self.targets = [self.project / name for name in ("first", "second", "third")]
        self.entries = [root / "alpha" for root in self.targets]
        self.plan = build_integration_plan(
            IntegrationSpec(
                project_root=self.project,
                sources=(self.source,),
                targets=tuple(
                    IntegrationTarget("host-" + root.name, root=root)
                    for root in self.targets
                ),
            )
        )
        self.assertEqual(self.plan["summary"]["changes"], 3)
        self.source_before = self.tree_snapshot(self.canonical)
        self.receipt_path = self.project / "failed-receipt.json"

    def tree_snapshot(self, root: Path) -> dict:
        return {
            str(path.relative_to(root)): (
                path.stat().st_mode,
                path.stat().st_mtime_ns,
                path.read_bytes() if path.is_file() else None,
            )
            for path in [root, *root.rglob("*")]
        }

    def assert_partial_failure(self, error: IntegrationError, code: str) -> dict:
        self.assertEqual(error.code, code)
        self.assertEqual(error.exit_code, 3)
        self.assertIn({"receipt_path": str(self.receipt_path)}, error.details)
        failed = json.loads(self.receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["plan_id"], self.plan["plan_id"])
        self.assertEqual(failed["error"]["code"], code)
        self.assertEqual(
            failed["results"],
            [{
                "environment": "host-first",
                "scope": "project",
                "root": str(self.targets[0]),
                "name": "alpha",
                "action": "create_link",
                "expected_target": str(self.canonical),
                "expected_tree_sha256": self.plan["source_skills"][0]["tree_sha256"],
                "archive_path": None,
                "entry_after": entry_snapshot(self.entries[0]),
                "verified": True,
            }],
        )
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
        self.assertTrue(self.entries[0].is_symlink())
        self.assertEqual(self.entries[0].resolve(), self.canonical)
        self.assertEqual(list(self.targets[0].iterdir()), [self.entries[0]])
        self.assertFalse(self.targets[2].exists())
        self.assertFalse(self.entries[2].is_symlink())
        self.assertEqual(self.tree_snapshot(self.canonical), self.source_before)
        self.assertEqual(list(self.project.rglob("*.json")), [self.receipt_path])
        return failed

    def test_second_target_link_failure_keeps_first_completion_and_stops_later_targets(self) -> None:
        real_symlink = os.symlink

        def fail_second_link(source, destination, *args, **kwargs):
            if Path(destination) == self.entries[1]:
                raise OSError("second target link denied")
            return real_symlink(source, destination, *args, **kwargs)

        with patch(
            "skills_auditor.integration.os.symlink", side_effect=fail_second_link
        ) as create_link:
            with self.assertRaises(IntegrationError) as caught:
                apply_integration_plan(self.plan, self.receipt_path)

        self.assertEqual(create_link.call_count, 2)
        failed = self.assert_partial_failure(caught.exception, "apply_failed")
        self.assertIn("second target link denied", str(caught.exception))
        self.assertEqual(failed["error"]["message"], "second target link denied")
        self.assertEqual(failed["error"]["details"], [])
        self.assertTrue(self.targets[1].is_dir())
        self.assertEqual(list(self.targets[1].iterdir()), [])
        self.assertFalse(self.entries[1].exists())
        self.assertFalse(self.entries[1].is_symlink())

    def test_second_target_race_keeps_first_completion_and_concurrent_directory(self) -> None:
        concurrent_before = []

        def change_second_after_first_action(root: Path, action: dict) -> None:
            _apply_exact_action(root, action)
            self.assertEqual(root, self.targets[0])
            self.entries[1].mkdir(parents=True)
            (self.entries[1] / "owner.txt").write_text(
                "concurrent user content\n", encoding="utf-8"
            )
            concurrent_before.append(self.tree_snapshot(self.targets[1]))

        with patch(
            "skills_auditor.integration._apply_exact_action",
            side_effect=change_second_after_first_action,
        ) as apply_action:
            with self.assertRaises(IntegrationError) as caught:
                apply_integration_plan(self.plan, self.receipt_path)

        apply_action.assert_called_once_with(
            self.targets[0], self.plan["targets"][0]["actions"][0]
        )
        failed = self.assert_partial_failure(caught.exception, "stale_plan")
        expected_detail = {
            "code": "target_changed",
            "target": str(self.targets[1]),
            "name": "alpha",
            "expected": {"kind": "missing"},
            "actual": entry_snapshot(self.entries[1]),
        }
        self.assertEqual(failed["error"]["details"], [expected_detail])
        self.assertEqual(
            caught.exception.details,
            [expected_detail, {"receipt_path": str(self.receipt_path)}],
        )
        self.assertTrue(self.entries[1].is_dir())
        self.assertFalse(self.entries[1].is_symlink())
        self.assertEqual(self.tree_snapshot(self.targets[1]), concurrent_before[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
