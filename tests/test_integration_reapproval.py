"""Approval renewal through reviewed no-op integration plans."""

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
    save_plan,
    verify_receipt,
)


class TestIntegrationReapproval(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="skills-auditor-reapproval-")
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name)
        self.source = self.project / "source"
        self.canonical = self.source / "alpha"
        self.canonical.mkdir(parents=True)
        (self.canonical / "SKILL.md").write_text(
            "---\nname: alpha\ndescription: approval renewal fixture\n---\n\nbody\n",
            encoding="utf-8",
        )
        self.payload = self.canonical / "payload.txt"
        self.payload.write_text("H1\n", encoding="utf-8")
        self.target = self.project / "target"
        self.entry = self.target / "alpha"
        self.spec = IntegrationSpec(
            project_root=self.project,
            sources=(self.source,),
            targets=(IntegrationTarget("test-host", root=self.target),),
        )
        initial_plan = build_integration_plan(self.spec)
        self.old_receipt, self.old_receipt_path = apply_integration_plan(initial_plan)
        self.old_receipt_bytes = self.old_receipt_path.read_bytes()

    def link_snapshot(self) -> tuple:
        stat = self.entry.lstat()
        return (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, os.readlink(self.entry))

    def source_snapshot(self) -> dict:
        return {
            str(path.relative_to(self.canonical)): (
                path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns
            )
            for path in self.canonical.rglob("*")
            if path.is_file()
        }

    def receipt_snapshot(self) -> dict:
        return {
            path.name: path.read_bytes()
            for path in self.old_receipt_path.parent.glob("*.json")
        }

    def assert_old_approval_invalidated(self) -> None:
        self.assertEqual(self.old_receipt_path.read_bytes(), self.old_receipt_bytes)
        self.assertEqual(
            verify_receipt(self.old_receipt)["approval"],
            {"state": "invalidated", "requires_reapproval": True, "reason_codes": ["source_tree"]},
        )

    def test_noop_plan_requires_explicit_apply_to_renew_changed_source_approval(self) -> None:
        self.assertEqual(verify_receipt(self.old_receipt)["approval"]["state"], "valid")
        self.payload.write_text("H2\n", encoding="utf-8")
        self.assert_old_approval_invalidated()
        link_before = self.link_snapshot()
        source_before = self.source_snapshot()
        receipts_before = self.receipt_snapshot()

        renewal_plan = build_integration_plan(self.spec)
        plan_path = save_plan(renewal_plan)
        self.assertTrue(plan_path.is_file())
        self.assertEqual(renewal_plan["summary"]["changes"], 0)
        self.assertEqual(renewal_plan["summary"]["actions"], 1)
        self.assertEqual(renewal_plan["targets"][0]["actions"][0]["action"], "noop")
        self.assertEqual(self.receipt_snapshot(), receipts_before)
        self.assertEqual(self.link_snapshot(), link_before)
        self.assertEqual(self.source_snapshot(), source_before)
        self.assert_old_approval_invalidated()

        with patch("skills_auditor.integration.os.symlink") as create_link:
            renewed, renewed_path = apply_integration_plan(
                json.loads(plan_path.read_text(encoding="utf-8"))
            )
        create_link.assert_not_called()

        self.assertNotEqual(renewed["receipt_id"], self.old_receipt["receipt_id"])
        self.assertNotEqual(renewed_path, self.old_receipt_path)
        self.assertTrue(renewed_path.is_file())
        self.assertEqual(renewed["plan_id"], renewal_plan["plan_id"])
        self.assertEqual(renewed["results"][0]["action"], "noop")
        self.assertEqual(
            renewed["results"][0]["expected_tree_sha256"],
            renewal_plan["source_skills"][0]["tree_sha256"],
        )
        self.assertNotEqual(
            renewed["results"][0]["expected_tree_sha256"],
            self.old_receipt["results"][0]["expected_tree_sha256"],
        )
        self.assertEqual(
            verify_receipt(renewed)["approval"],
            {"state": "valid", "requires_reapproval": False, "reason_codes": []},
        )
        self.assertEqual(self.link_snapshot(), link_before)
        self.assertEqual(self.source_snapshot(), source_before)
        self.assert_old_approval_invalidated()

    def test_stale_noop_plan_rejects_source_change_before_writes(self) -> None:
        self.payload.write_text("H2\n", encoding="utf-8")
        renewal_plan = build_integration_plan(self.spec)
        self.assertEqual(renewal_plan["targets"][0]["actions"][0]["action"], "noop")
        self.payload.write_text("H3\n", encoding="utf-8")
        link_before = self.link_snapshot()
        source_before = self.source_snapshot()
        receipts_before = self.receipt_snapshot()
        rejected_receipt = self.project / "rejected-receipt.json"

        with patch("skills_auditor.integration.os.symlink") as create_link:
            with self.assertRaises(IntegrationError) as caught:
                apply_integration_plan(renewal_plan, rejected_receipt)
        create_link.assert_not_called()

        self.assertEqual(caught.exception.code, "stale_plan")
        self.assertEqual(caught.exception.exit_code, 3)
        self.assertIn("source_changed", {detail["code"] for detail in caught.exception.details})
        self.assertFalse(rejected_receipt.exists())
        self.assertEqual(self.receipt_snapshot(), receipts_before)
        self.assertEqual(self.link_snapshot(), link_before)
        self.assertEqual(self.source_snapshot(), source_before)
        self.assert_old_approval_invalidated()

    def test_noop_apply_rechecks_source_after_preflight_and_records_failure(self) -> None:
        self.payload.write_text("H2\n", encoding="utf-8")
        renewal_plan = build_integration_plan(self.spec)
        self.assertEqual(renewal_plan["targets"][0]["actions"][0]["action"], "noop")
        link_before = self.link_snapshot()
        failed_path = self.project / "failed-receipt.json"

        def change_after_preflight(plan: dict) -> list:
            issues = check_plan_preconditions(plan)
            self.assertEqual(issues, [])
            self.payload.write_text("H3\n", encoding="utf-8")
            return issues

        with patch(
            "skills_auditor.integration.check_plan_preconditions",
            side_effect=change_after_preflight,
        ), patch("skills_auditor.integration.os.symlink") as create_link:
            with self.assertRaises(IntegrationError) as caught:
                apply_integration_plan(renewal_plan, failed_path)
        create_link.assert_not_called()

        self.assertEqual(caught.exception.code, "stale_plan")
        self.assertEqual(caught.exception.exit_code, 3)
        failed = json.loads(failed_path.read_text(encoding="utf-8"))
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["results"], [])
        self.assertEqual(failed["error"]["code"], "stale_plan")
        self.assertEqual(failed["error"]["details"][0]["code"], "source_changed")
        self.assertEqual(
            verify_receipt(failed)["approval"],
            {
                "state": "invalidated",
                "requires_reapproval": True,
                "reason_codes": ["receipt_not_completed"],
            },
        )
        self.assertEqual(self.payload.read_text(encoding="utf-8"), "H3\n")
        self.assertEqual(self.link_snapshot(), link_before)
        self.assert_old_approval_invalidated()


if __name__ == "__main__":
    unittest.main(verbosity=2)
