"""Tests for skill-run execution ledgers."""

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from skills_auditor.cli import main
from skills_auditor.ledger import (
    SCHEMA_VERSION,
    audit_ledger,
    create_ledger,
    ledger_summary,
    load_ledger,
    save_ledger,
    update_checks,
    upsert_resource,
)


class TestLedgerCore(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger_root = Path(self.tmp.name) / "ledgers"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_create_ledger_writes_schema_and_run_metadata(self) -> None:
        ledger = create_ledger(
            run_id="run-1",
            source="unit-test",
            mode="dry-run",
            ledger_root=self.ledger_root,
        )

        loaded = load_ledger("run-1", self.ledger_root)
        self.assertEqual(ledger["schema_version"], SCHEMA_VERSION)
        self.assertEqual(loaded["run"]["id"], "run-1")
        self.assertEqual(loaded["run"]["source"], "unit-test")
        self.assertEqual(loaded["run"]["mode"], "dry-run")

    def test_upsert_resource_merges_metadata_and_notes(self) -> None:
        ledger = create_ledger(run_id="run-1", ledger_root=self.ledger_root)
        upsert_resource(
            ledger,
            resource_id="route-trace",
            resource_class="trace",
            locator="traces/route.json",
            owner="skills-auditor-route",
            status="active",
            note="trace planned",
            metadata={"platform": "codex"},
        )
        upsert_resource(
            ledger,
            resource_id="route-trace",
            resource_class="trace",
            locator="traces/route.json",
            owner="skills-auditor-route",
            status="completed",
            note="trace checked",
            metadata={"strategy": "archive"},
        )
        save_ledger(ledger, self.ledger_root)

        resource = load_ledger("run-1", self.ledger_root)["resources"][0]
        self.assertEqual(resource["status"], "completed")
        self.assertEqual(resource["notes"], ["trace planned", "trace checked"])
        self.assertEqual(resource["metadata"]["platform"], "codex")
        self.assertEqual(resource["metadata"]["strategy"], "archive")

    def test_check_flags_active_and_failed_resources(self) -> None:
        ledger = create_ledger(run_id="run-1", ledger_root=self.ledger_root)
        upsert_resource(
            ledger,
            resource_id="worker",
            resource_class="subagent-run",
            locator="thread:abc",
            owner="skills-auditor",
            status="active",
        )
        upsert_resource(
            ledger,
            resource_id="artifact",
            resource_class="artifact",
            locator="out.json",
            owner="skills-auditor",
            status="failed",
        )

        findings = audit_ledger(ledger)
        update_checks(ledger, findings)

        self.assertIn("active_resource", [f.check for f in findings])
        self.assertIn("failed_resource", [f.check for f in findings])
        self.assertFalse(ledger["checks"]["no_active_resources"])
        self.assertFalse(ledger["checks"]["no_failed_resources"])

    def test_check_requires_handoff_target_and_blocked_reason(self) -> None:
        ledger = create_ledger(run_id="run-1", ledger_root=self.ledger_root)
        upsert_resource(
            ledger,
            resource_id="handoff-row",
            resource_class="skill-run",
            locator="skills-audit route",
            owner="skills-auditor-route",
            status="handoff",
        )
        upsert_resource(
            ledger,
            resource_id="blocked-row",
            resource_class="subagent-run",
            locator="thread:abc",
            owner="skills-auditor",
            status="blocked",
        )

        findings = audit_ledger(ledger)
        update_checks(ledger, findings)

        self.assertIn("handoff_target", [f.check for f in findings])
        self.assertIn("blocked_reason", [f.check for f in findings])
        self.assertFalse(ledger["checks"]["handoffs_have_target"])
        self.assertFalse(ledger["checks"]["blocked_have_reason"])

    def test_summary_counts_resources_by_status_and_class(self) -> None:
        ledger = create_ledger(run_id="run-1", ledger_root=self.ledger_root)
        upsert_resource(
            ledger,
            resource_id="skill",
            resource_class="skill-run",
            locator="skills/route/SKILL.md",
            owner="skills-auditor-route",
            status="completed",
        )
        upsert_resource(
            ledger,
            resource_id="trace",
            resource_class="trace",
            locator="trace.json",
            owner="skills-auditor-route",
            status="preserved",
        )

        summary = ledger_summary(ledger)

        self.assertEqual(summary["resource_count"], 2)
        self.assertEqual(summary["by_status"]["completed"], 1)
        self.assertEqual(summary["by_status"]["preserved"], 1)
        self.assertEqual(summary["by_class"]["skill-run"], 1)
        self.assertEqual(summary["by_class"]["trace"], 1)


class TestLedgerCli(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger_root = Path(self.tmp.name) / "ledgers"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run_cli(self, argv: list[str]) -> tuple[int, str]:
        stdout = StringIO()
        with patch("sys.argv", ["skills-audit"] + argv), redirect_stdout(stdout):
            exit_code = main()
        return exit_code, stdout.getvalue()

    def test_cli_create_upsert_check_and_summary(self) -> None:
        exit_code, out = self._run_cli(
            [
                "ledger-create",
                "--ledger-dir",
                str(self.ledger_root),
                "--run-id",
                "run-1",
                "--source",
                "unit-test",
                "--mode",
                "apply",
            ]
        )
        self.assertEqual(exit_code, 0)
        self.assertIn("ledger written:", out)

        exit_code, out = self._run_cli(
            [
                "ledger-upsert",
                "--ledger-dir",
                str(self.ledger_root),
                "--run-id",
                "run-1",
                "--id",
                "artifact-1",
                "--class",
                "artifact",
                "--locator",
                "reports/out.json",
                "--owner",
                "skills-auditor-sync",
                "--status",
                "completed",
                "--metadata",
                "mode=apply",
                "--note",
                "wrote artifact",
            ]
        )
        self.assertEqual(exit_code, 0)
        self.assertIn("ledger updated:", out)

        exit_code, out = self._run_cli(
            [
                "ledger-check",
                "--ledger-dir",
                str(self.ledger_root),
                "--run-id",
                "run-1",
            ]
        )
        self.assertEqual(exit_code, 0)
        self.assertIn("findings: 0", out)
        self.assertTrue(load_ledger("run-1", self.ledger_root)["checks"]["no_active_resources"])

        exit_code, out = self._run_cli(
            [
                "ledger-summary",
                "--ledger-dir",
                str(self.ledger_root),
                "--run-id",
                "run-1",
            ]
        )
        self.assertEqual(exit_code, 0)
        self.assertIn("ledgers: 1", out)
        self.assertIn("artifact-1", json.dumps(load_ledger("run-1", self.ledger_root)))

    def test_cli_upsert_can_create_missing_ledger(self) -> None:
        exit_code, out = self._run_cli(
            [
                "ledger-upsert",
                "--ledger-dir",
                str(self.ledger_root),
                "--run-id",
                "run-2",
                "--create-if-missing",
                "--source",
                "unit-test",
                "--id",
                "worker",
                "--class",
                "subagent-run",
                "--locator",
                "thread:abc",
                "--owner",
                "skills-auditor",
                "--status",
                "handoff",
                "--handoff-target",
                "operator",
            ]
        )

        self.assertEqual(exit_code, 0)
        self.assertIn("ledger updated:", out)
        loaded = load_ledger("run-2", self.ledger_root)
        self.assertEqual(loaded["run"]["source"], "unit-test")
        self.assertEqual(loaded["resources"][0]["handoff"]["target"], "operator")

    def test_cli_check_can_fail_on_active_warning(self) -> None:
        create_ledger(run_id="run-3", ledger_root=self.ledger_root)

        exit_code, _ = self._run_cli(
            [
                "ledger-upsert",
                "--ledger-dir",
                str(self.ledger_root),
                "--run-id",
                "run-3",
                "--id",
                "worker",
                "--class",
                "subagent-run",
                "--locator",
                "thread:abc",
                "--owner",
                "skills-auditor",
                "--status",
                "active",
            ]
        )
        self.assertEqual(exit_code, 0)

        exit_code, out = self._run_cli(
            [
                "ledger-check",
                "--ledger-dir",
                str(self.ledger_root),
                "--run-id",
                "run-3",
                "--fail-on-warning",
            ]
        )

        self.assertEqual(exit_code, 1)
        self.assertIn("active_resource", out)


if __name__ == "__main__":
    unittest.main()
