"""Shipped managed contracts validate real records, not only hand-made examples."""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from skills_auditor.lifecycle.common import LifecycleError, digest
from skills_auditor.lifecycle.engine import Manager


class TestLifecycleSchemas(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from jsonschema import Draft202012Validator, FormatChecker
            from referencing import Registry, Resource
        except ImportError:
            raise unittest.SkipTest("install the test extra to validate JSON schemas")
        path = Path(__file__).parents[1] / "skills_auditor/schemas/lifecycle-core-v1.schema.json"
        cls.schema = json.loads(path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(cls.schema)
        cls.validator_type = Draft202012Validator
        cls.format_checker = FormatChecker()
        status = json.loads((path.parent / "lifecycle-status-v1.schema.json").read_text(encoding="utf-8"))
        cls.registry = Registry().with_resource(status["$id"], Resource.from_contents(status))

    def validator(self, kind):
        return self.validator_type(
            {**self.schema, "$ref": "#/$defs/" + kind},
            format_checker=self.format_checker,
            registry=self.registry,
        )

    def check(self, kind, value):
        errors = list(self.validator(kind).iter_errors(value))
        if errors:
            self.fail("{}: {}".format(kind, "; ".join(
                "{}: {}".format(list(error.path), error.message[:200]) for error in errors
            )[:1200]))

    def test_real_core_records_and_every_operation_match_shipped_contracts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "candidate"
            source.mkdir()
            (source / "SKILL.md").write_text("# Candidate H1\n", encoding="utf-8")
            hosts = root / "hosts"
            hosts.mkdir()
            manager = Manager(root)
            installation_id = None
            first_version = None
            for operation, options in (
                ("install", {"source": source, "target": hosts / "alpha"}),
                ("rename", {"name": "renamed"}),
                ("move", {"target": hosts / "beta"}),
                ("update", {"source": source}),
                ("edit", {"source": source}),
                ("disable", {}), ("enable", {}),
                ("archive", {}), ("enable", {}),
                ("renew", {}), ("revoke", {}), ("renew", {}),
                ("rollback", {}), ("uninstall", {}),
            ):
                with self.subTest(operation=operation):
                    if operation in {"update", "edit"}:
                        (source / "SKILL.md").write_text("# " + operation + "\n", encoding="utf-8")
                    if operation == "rollback":
                        options["version_id"] = first_version
                    plan = manager.plan(operation, installation_id=installation_id, **options)
                    self.check("plan", plan)
                    self.check("skill", plan["skill"])
                    self.check("version", plan["version"])
                    receipt = manager.apply(plan, approve_plan_id=plan["plan_id"])
                    installation_id = receipt["installation_id"]
                    first_version = first_version or receipt["version_id"]
                    self.check("receipt", receipt)
                    self.check("transaction", manager.inspect_transaction(receipt["transaction_id"]))
                    installation = manager.get_installation(installation_id)
                    self.check("installation", installation)
                    self.check("grant", manager.repository.get("grant", receipt["grant_id"])["data"])
                    self.check("verification", manager.verify(installation_id))
                    self.check("verification_run", manager.repository.get("verification-run", installation_id)["data"])
                    self.check("installation_list", {
                        "schema_version": "skills-auditor-lifecycle-list/v1",
                        "installations": manager.list_installations(),
                    })
                    from skills_auditor.lifecycle.status import preflight
                    self.check("preflight", preflight(manager, installation_id))
                    for event in manager.repository.events(installation_id):
                        self.check("event", event)
            retained = manager.plan("install-retained", skill_id=plan["skill"]["skill_id"],
                                    version_id=plan["version"]["version_id"], target=hosts / "retained")
            self.check("plan", retained)
            reinstated = manager.apply(retained, approve_plan_id=retained["plan_id"])
            self.check("receipt", reinstated)
            self.check("transaction", manager.inspect_transaction(reinstated["transaction_id"]))
            self.assertNotEqual(reinstated["installation_id"], installation_id)
            self.assertIsNone(retained["source"])
            snapshot = Path(plan["version"]["snapshot"]["path"]).parent
            self.check("snapshot_descriptor", plan["version"]["snapshot"])
            self.check("snapshot_manifest", json.loads((snapshot / "manifest.json").read_text()))
            self.check("snapshot_stage", json.loads((snapshot / "stage.json").read_text()))
            self.check("error", LifecycleError("probe", "bounded failure").to_dict())

    def test_malformed_shapes_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "candidate"
            source.mkdir()
            (source / "SKILL.md").write_text("# H1\n", encoding="utf-8")
            manager = Manager(root)
            plan = manager.plan("install", source=source, target=root / "target")
            receipt = manager.apply(plan, approve_plan_id=plan["plan_id"])
            verification = manager.verify(receipt["installation_id"])
            invalid = []
            from skills_auditor.lifecycle.status import preflight
            flight = preflight(manager, receipt["installation_id"])
            for identifiers in ("incident-id", [None], ["duplicate", "duplicate"]):
                invalid.append(("preflight", {**flight, "status": {**flight["status"], "incident_ids": identifiers}}))
            for key, value in (("plan_id", "not-a-digest"), ("expected_revision", True),
                               ("operation", "trust-everything"), ("steps", []), ("source", None),
                               ("created_at", "yesterday")):
                variant = copy.deepcopy(plan)
                variant[key] = value
                invalid.append(("plan", variant))
            unexpected = copy.deepcopy(plan)
            unexpected["implicitly_approved"] = True
            invalid.append(("plan", unexpected))
            failed_receipt = {**receipt, "status": "completed", "steps": []}
            invalid.append(("receipt", failed_receipt))
            invalid.append(("receipt", {**receipt, "status": "partial"}))
            invalid.append(("verification", {**verification, "valid": "yes"}))
            for checks in (
                [{"code": "arbitrary", "valid": True}],
                verification["integrity"]["checks"][:-1],
                verification["integrity"]["checks"] + [verification["integrity"]["checks"][0]],
            ):
                invalid.append(("verification", {**verification, "integrity": {"valid": True, "checks": checks}}))
            invalid.append(("verification", {**verification, "approval": {
                "state": "valid", "requires_reapproval": True, "reason_codes": []}}))
            invalid.append(("verification", {**verification, "approval": {
                "state": "invalidated", "requires_reapproval": False, "reason_codes": ["snapshot_tree"]}}))
            invalid.append(("verification", {**verification, "valid": False, "integrity": {
                "valid": False, "checks": [{"code": "snapshot_tree", "valid": False}]}}))
            for kind, document in invalid:
                with self.subTest(kind=kind, document=document):
                    self.assertTrue(list(self.validator(kind).iter_errors(document)))

    def test_migration_and_interrupted_recovery_records_match_contracts(self):
        from skills_auditor.integration import (
            IntegrationSpec, IntegrationTarget, apply_integration_plan, build_integration_plan,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source" / "alpha"
            source.mkdir(parents=True)
            (source / "SKILL.md").write_text(
                "---\nname: alpha\ndescription: legacy fixture\n---\n# H1\n", encoding="utf-8"
            )
            host = root / "host"
            spec = IntegrationSpec(project_root=root, sources=(source.parent,),
                                   targets=(IntegrationTarget("fixture", root=host),))
            legacy, _ = apply_integration_plan(build_integration_plan(spec))
            manager = Manager(root)
            plan = manager.plan("migrate", source=source, target=host / "alpha", legacy_receipt=legacy)
            self.check("plan", plan)
            receipt = manager.apply(plan, approve_plan_id=plan["plan_id"])
            self.check("receipt", receipt)
            update = manager.plan("move", installation_id=receipt["installation_id"], target=host / "beta")
            seen = []

            def interrupt(name, transaction):
                self.check("transaction", transaction)
                seen.append(transaction["state"])
                if name == "step:0:effect":
                    raise OSError("controlled schema recovery probe")

            with self.assertRaises(LifecycleError):
                manager.apply(update, approve_plan_id=update["plan_id"], transaction_id="schema-probe", checkpoint=interrupt)
            transaction = manager.inspect_transaction("schema-probe")
            self.assertEqual(transaction["state"], "recovery_needed")
            self.assertIn("prepared", seen)
            self.assertIn("applying", seen)
            self.check("transaction", transaction)
            self.assertIsNone(transaction["receipt_id"])
            compensated = manager.recover("schema-probe", mode="compensate", approve_plan_id=update["plan_id"])
            self.assertEqual(compensated["state"], "compensated")
            self.check("transaction", compensated)

    def test_timezone_qualified_imported_plan_matches_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "candidate"
            source.mkdir()
            (source / "SKILL.md").write_text("# H1\n", encoding="utf-8")
            manager = Manager(root)
            plan = manager.plan("install", source=source, target=root / "target")
            for document in (plan, plan["skill"], plan["version"], plan["after"]):
                document["created_at"] = "2026-09-07T12:00:00.123456+08:00"
            plan["plan_id"] = digest({key: value for key, value in plan.items() if key != "plan_id"})
            self.check("plan", plan)
            receipt = manager.apply(plan, approve_plan_id=plan["plan_id"])
            self.check("receipt", receipt)
            self.check("transaction", manager.inspect_transaction(receipt["transaction_id"]))


if __name__ == "__main__":
    unittest.main()
