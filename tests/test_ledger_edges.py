"""Validation edges for execution ledgers."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from skills_auditor.ledger import (
    SCHEMA_VERSION,
    _parse_metadata,
    audit_ledger,
    default_run_id,
    empty_ledger,
    load_ledger,
    save_ledger,
    upsert_resource,
)


class TestLedgerEdges(unittest.TestCase):
    def test_generated_ids_and_default_save_id_are_nonempty(self) -> None:
        self.assertRegex(default_run_id(), r"^\d{4}-\d{2}-\d{2}T.*-[0-9a-f]{8}$")
        with tempfile.TemporaryDirectory() as base:
            ledger = {"run": {}, "resources": [], "checks": {}}
            path = save_ledger(ledger, Path(base))
            self.assertTrue(path.is_file())
            self.assertTrue(ledger["run"]["id"])

    def test_metadata_and_upsert_reject_invalid_values(self) -> None:
        for raw in (["missing"], ["=value"]):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                _parse_metadata(raw)
        ledger = empty_ledger("run")
        with self.assertRaises(ValueError):
            upsert_resource(
                ledger,
                resource_id="x",
                resource_class="invalid",
                locator="path",
                owner="owner",
                status="completed",
            )
        with self.assertRaises(ValueError):
            upsert_resource(
                ledger,
                resource_id="x",
                resource_class="artifact",
                locator="path",
                owner="owner",
                status="invalid",
            )

    def test_load_rejects_non_object_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            (root / "run.json").write_text("[]", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_ledger("run", root)

    def test_audit_reports_every_structural_resource_error(self) -> None:
        ledger = {
            "schema_version": "wrong",
            "run": {},
            "resources": [
                {},
                {
                    "id": "duplicate",
                    "class": "wrong",
                    "locator": "",
                    "owner": "",
                    "status": "wrong",
                },
                {
                    "id": "duplicate",
                    "class": "artifact",
                    "locator": "path",
                    "owner": "owner",
                    "status": "completed",
                },
                {
                    "id": "active",
                    "class": "artifact",
                    "locator": "path",
                    "owner": "owner",
                    "status": "active",
                },
                {
                    "id": "failed",
                    "class": "artifact",
                    "locator": "path",
                    "owner": "owner",
                    "status": "failed",
                },
            ],
        }
        checks = {finding.check for finding in audit_ledger(ledger)}
        self.assertEqual(
            checks,
            {
                "schema_version",
                "run_id",
                "resource_id",
                "duplicate_resource",
                "resource_class",
                "status",
                "locator",
                "owner",
                "active_resource",
                "failed_resource",
            },
        )
        self.assertNotEqual(ledger.get("schema_version"), SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
