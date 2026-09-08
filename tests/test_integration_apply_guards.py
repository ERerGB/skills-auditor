"""Post-action checks must not certify content changed during an apply."""

from __future__ import annotations

import json
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
    verify_receipt,
)


class TestPostActionGuards(unittest.TestCase):
    def assert_post_action_failure(self, operation: str, disruption: str) -> None:
        with tempfile.TemporaryDirectory() as base:
            project = Path(base).resolve()
            source = project / "source"
            skill = source / "alpha"
            skill.mkdir(parents=True)
            skill_file = skill / "SKILL.md"
            original_source = b"---\nname: alpha\ndescription: guard fixture\n---\nH1\n"
            skill_file.write_bytes(original_source)
            target = project / "target"
            spec = IntegrationSpec(
                project_root=project,
                sources=(source,),
                targets=(IntegrationTarget("test", root=target),),
            )
            plan = build_integration_plan(spec)
            old_receipt_path = project / "old-receipt.json"
            old_receipt_bytes = None
            if operation == "noop":
                old_receipt, _ = apply_integration_plan(plan, old_receipt_path)
                self.assertEqual(verify_receipt(old_receipt)["status"], "passed")
                old_receipt_bytes = old_receipt_path.read_bytes()
                plan = build_integration_plan(spec)
            self.assertEqual(plan["targets"][0]["actions"][0]["action"], operation)
            entry = target / "alpha"

            def apply_then_disrupt(root, action):
                _apply_exact_action(root, action)
                if disruption == "target":
                    entry.unlink()
                else:
                    skill_file.write_bytes(original_source.replace(b"H1", b"H2"))

            receipt_path = project / "failed-receipt.json"
            with patch(
                "skills_auditor.integration._apply_exact_action",
                side_effect=apply_then_disrupt,
            ) as apply_action:
                with self.assertRaises(IntegrationError) as caught:
                    apply_integration_plan(plan, receipt_path)

            apply_action.assert_called_once()
            self.assertEqual(caught.exception.code, "apply_verification_failed")
            self.assertEqual(caught.exception.exit_code, 3)
            self.assertIn({"receipt_path": str(receipt_path)}, caught.exception.details)
            failed = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["results"], [])
            self.assertEqual(failed["error"]["code"], "apply_verification_failed")
            self.assertEqual(
                verify_receipt(failed)["approval"],
                {
                    "state": "invalidated",
                    "requires_reapproval": True,
                    "reason_codes": ["receipt_not_completed"],
                },
            )
            if disruption == "target":
                self.assertFalse(entry.exists())
                self.assertFalse(entry.is_symlink())
                self.assertEqual(skill_file.read_bytes(), original_source)
            else:
                self.assertTrue(entry.is_symlink())
                self.assertEqual(entry.resolve(), skill)
                self.assertEqual(
                    skill_file.read_bytes(), original_source.replace(b"H1", b"H2")
                )
            self.assertEqual(list(skill.iterdir()), [skill_file])
            self.assertEqual(list(target.iterdir()), [] if disruption == "target" else [entry])
            if old_receipt_bytes is not None:
                self.assertEqual(old_receipt_path.read_bytes(), old_receipt_bytes)
                self.assertEqual(verify_receipt(old_receipt)["status"], "failed")
            else:
                self.assertFalse(old_receipt_path.exists())

    def test_target_changed_after_action_does_not_get_a_success_receipt(self) -> None:
        for operation in ("create_link", "noop"):
            with self.subTest(operation=operation):
                self.assert_post_action_failure(operation, "target")

    def test_source_changed_after_action_does_not_get_a_success_receipt(self) -> None:
        for operation in ("create_link", "noop"):
            with self.subTest(operation=operation):
                self.assert_post_action_failure(operation, "source")


if __name__ == "__main__":
    unittest.main()
