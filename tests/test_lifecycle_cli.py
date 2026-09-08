"""Managed CLI uses one machine document and explicit, version-bound approval."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import sys
import shlex
import unicodedata
import unittest
from unittest.mock import patch

from skills_auditor.cli import main


class TestLifecycleCli(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="skills-auditor-managed-cli-")
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name).resolve()
        self.source = self.project / "candidate"
        self.source.mkdir()
        (self.source / "SKILL.md").write_text("# Example\n")
        (self.source / "payload").write_text("H1")
        self.target = self.project / "installed"

    def run_cli(self, *arguments, expected=0, text=False):
        out, err = io.StringIO(), io.StringIO()
        argv = ["skills-audit", "lifecycle", "--project-root", str(self.project), "--format", "text" if text else "json", *map(str, arguments)]
        with patch("sys.argv", argv), redirect_stdout(out), redirect_stderr(err):
            code = main()
        self.assertEqual(code, expected, out.getvalue() + err.getvalue())
        self.assertEqual(err.getvalue(), "")
        return out.getvalue() if text else json.loads(out.getvalue())

    def install(self):
        path = self.project / "plan.json"
        plan = self.run_cli("plan", "install", "--source", self.source, "--target", self.target, "--plan-out", path)
        receipt = self.run_cli("apply", path, "--approve-plan-id", plan["plan_id"])
        return plan, receipt, path

    def test_incident_journal_pagination_resolution_and_supersession(self):
        _, receipt, path = self.install()
        raw = self.target.readlink()
        self.target.unlink()
        failed = self.run_cli("verify", receipt["installation_id"], expected=3)
        incident = self.run_cli("incidents", "--installation-id", receipt["installation_id"], "--state", "open")["incidents"][0]
        identifier = incident["incident_id"]
        event = self.run_cli("append-note", identifier, "--text", "Investigating missing pointer", "--actor", "tester", "--tool", "fixture", "--event-id", "note-cli", "--evidence-ref", "verification:" + failed["verification_id"])
        self.assertEqual(event["actor"], "tester")
        self.assertEqual(self.run_cli("append-note", identifier, "--text", "Investigating missing pointer", "--actor", "tester", "--tool", "fixture", "--event-id", "note-cli", "--evidence-ref", "verification:" + failed["verification_id"]), event)
        page = self.run_cli("investigate", identifier, "--limit", "1")
        self.assertTrue(page["has_more"])
        following = self.run_cli("investigate", identifier, "--limit", "1", "--after-sequence", page["continuation"]["after_sequence"])
        self.assertEqual(following["events"][0]["event_type"], "note")
        self.run_cli("append-note", identifier, "--text", "bad reference", "--evidence-ref", "/etc/passwd", expected=2)
        self.run_cli("investigate", identifier, "--limit", "51", expected=2)
        self.run_cli("resolve", identifier, "--verification-id", failed["verification_id"], expected=3)
        self.target.symlink_to(raw)
        renew = self.run_cli("plan", "renew", "--installation-id", receipt["installation_id"], "--plan-out", path)
        self.run_cli("apply", path, "--approve-plan-id", renew["plan_id"])
        clean = self.run_cli("verify", receipt["installation_id"])
        self.assertEqual(self.run_cli("resolve", identifier, "--verification-id", clean["verification_id"])["state"], "resolved")
        self.target.unlink()
        self.run_cli("verify", receipt["installation_id"], expected=3)
        other = self.run_cli("incidents", "--state", "open")["incidents"][0]["incident_id"]
        self.target.symlink_to(self.source)
        self.run_cli("verify", receipt["installation_id"], expected=3)
        replacement = next(value["incident_id"] for value in self.run_cli("incidents", "--state", "open")["incidents"] if value["incident_id"] != other)
        self.assertEqual(self.run_cli("supersede", other, replacement, "--explanation", "Related active investigation")["state"], "superseded")
        self.assertEqual(self.run_cli("inspect", "incident", other)["superseded_by"], replacement)

    def test_invocation_cli_preserves_h1_and_requires_audited_stale_override(self):
        _, receipt, _ = self.install()
        identifier = receipt["installation_id"]
        (self.source / "payload").write_text("H2 candidate")
        result = self.run_cli("invocation", "select", identifier)
        self.assertEqual((Path(result["snapshot_path"]) / "payload").read_text(), "H1")
        old = (datetime.now(timezone.utc) - timedelta(seconds=20)).isoformat()
        with patch("skills_auditor.lifecycle.engine.utc_now", return_value=old):
            self.run_cli("verify", identifier)
        self.run_cli("invocation", "select", identifier, "--cached", "--max-age-seconds", "1", expected=3)
        self.run_cli("invocation", "select", identifier, "--cached", "--max-age-seconds", "1", "--policy", "last-known-good", expected=4)
        path = self.project / "override.json"
        plan = self.run_cli("invocation", "override-plan", identifier, "--reason", "Temporary stale observation exception", "--max-age-seconds", "1", "--plan-out", path)
        self.run_cli("invocation", "override-apply", path, expected=3)
        approved = self.run_cli("invocation", "override-apply", path, "--approve-plan-id", plan["plan_id"])
        override = approved["override_id"]
        self.assertEqual(self.run_cli("invocation", "override-get", override), approved)
        selected = self.run_cli("invocation", "select", identifier, "--cached", "--max-age-seconds", "1", "--override-id", override)
        self.assertTrue(selected["override"]["used"])
        self.assertEqual(selected["status"]["freshness"]["state"], "stale")
        rendered = self.run_cli("invocation", "select", identifier, "--cached", "--max-age-seconds", "1", "--override-id", override, text=True)
        self.assertIn("[OVERRIDE]", rendered)
        self.assertIn("not executed", rendered)
        self.run_cli("invocation", "override-revoke", override, "--reason", "Exception ended")
        self.run_cli("invocation", "select", identifier, "--cached", "--max-age-seconds", "1", "--override-id", override, expected=3)
        blocked = self.run_cli("invocation", "select", identifier, "--cached", "--max-age-seconds", "1", "--override-id", override, expected=3, text=True)
        self.assertIn("[BLOCK] Decision: block", blocked)
        self.assertIn("override_inapplicable", blocked)

    def test_retention_cli_policy_expiry_collection_restore_and_purge_second_gate(self):
        _, first, core_path = self.install()
        uninstall = self.run_cli("plan", "uninstall", "--installation-id", first["installation_id"], "--plan-out", core_path)
        last = self.run_cli("apply", core_path, "--approve-plan-id", uninstall["plan_id"])
        path = self.project / "retention.json"

        def plan(operation, *options):
            return self.run_cli("retention", "plan", operation, *options, "--plan-out", path)

        def apply(value, *options):
            return self.run_cli("retention", "apply", path, "--approve-plan-id", value["plan_id"], *options)

        policy = plan("policy", "--keep-recent", "0", "--clear-pins")
        self.run_cli("retention", "apply", path, expected=3)
        apply(policy)
        apply(plan("expire", "--receipt-id", first["receipt_id"], "--receipt-id", last["receipt_id"]))
        collection = apply(plan("collect"))
        obj = collection["objects"][0]
        self.assertTrue(Path(obj["quarantine_path"]).is_dir())
        self.assertEqual(self.run_cli("retention", "recover", collection["transaction_id"])["state"], "completed")
        apply(plan("restore", "--object-id", obj["quarantine_id"]))
        collection = apply(plan("collect"))
        obj = collection["objects"][0]
        purge = plan("purge", "--object-id", obj["quarantine_id"], "--grace-seconds", "0")
        self.run_cli("retention", "apply", path, "--approve-plan-id", purge["plan_id"], expected=3)
        self.assertTrue(Path(obj["quarantine_path"]).is_dir())
        result = apply(purge, "--permanent-delete")
        self.assertTrue(result["permanently_deleted"])
        self.assertFalse(Path(obj["quarantine_path"]).exists())
        self.assertEqual(self.run_cli("inspect", "receipt", first["receipt_id"]), first)

    def test_batch_cli_saved_children_explicit_apply_and_compensation_plan(self):
        paths = []
        for index in range(2):
            path = self.project / ("child-{}.json".format(index))
            self.run_cli("plan", "install", "--source", self.source, "--target", self.project / ("target-" + str(index)), "--plan-out", path)
            paths.append(path)
        output = self.project / "batch.json"
        plan = self.run_cli("batch", "plan", *paths, "--plan-out", output)
        self.run_cli("batch", "apply", output, expected=3)
        receipt = self.run_cli("batch", "apply", output, "--approve-plan-id", plan["plan_id"], "--batch-id", "cli-batch")
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(self.run_cli("batch", "inspect", "cli-batch")["state"], "completed")
        self.assertEqual(self.run_cli("batch", "resume", "cli-batch", "--approve-plan-id", plan["plan_id"]), receipt)
        inverse = self.run_cli("batch", "compensate-plan", "cli-batch", "--plan-out", output)
        self.run_cli("batch", "apply", output, expected=3)
        self.run_cli("batch", "apply", output, "--approve-plan-id", inverse["plan_id"])
        self.assertFalse(any((self.project / ("target-" + str(index))).is_symlink() for index in range(2)))

    def test_new_plan_outputs_are_bounded_and_protect_unregistered_child_candidates(self):
        from skills_auditor.lifecycle.cli import _save_plan
        from skills_auditor.lifecycle.common import LifecycleError
        from skills_auditor.lifecycle.engine import Manager
        manager = Manager(self.project)
        self.addCleanup(manager.repository.close)
        output = self.project / "oversize.json"
        with self.assertRaises(LifecycleError) as caught:
            _save_plan(manager, {"schema_version": "skills-auditor-lifecycle-retention-plan/v1", "objects": [], "diagnostic": "x" * (2 * 1024 * 1024)}, output)
        self.assertEqual(caught.exception.code, "plan_output_too_large")
        self.assertFalse(output.exists())
        child = self.project / "child.json"
        self.run_cli("plan", "install", "--source", self.source, "--target", self.target, "--plan-out", child)
        self.run_cli("batch", "plan", child, "--plan-out", self.source / "payload", expected=3)
        self.assertEqual((self.source / "payload").read_text(), "H1")

    def test_all_consumer_apply_inputs_reject_unsafe_json_and_unknown_types(self):
        self.install()
        path = self.project / "unsafe.json"
        depth = sys.getrecursionlimit() + 100
        for data in ('{"x":1,"x":2}', '{"x":Infinity}', "[]", "x" * (2 * 1024 * 1024 + 1), '{"x":' + '[' * depth + '0' + ']' * depth + '}'):
            path.write_text(data)
            for command in (("invocation", "override-apply"), ("retention", "apply"), ("batch", "apply"), ("batch", "plan")):
                with self.subTest(command=command, prefix=data[:30]):
                    self.assertEqual(self.run_cli(*command, path, expected=2)["code"], "invalid_input")
        path.write_text("{}")
        self.run_cli("retention", "apply", path, "--approve-plan-id", "fake", expected=2)
        self.run_cli("batch", "apply", path, "--approve-plan-id", "fake", expected=2)
        self.run_cli("invocation", "override-apply", path, "--approve-plan-id", "fake", expected=2)
        self.run_cli("retention", "plan", "policy", "--keep-recent", "101", expected=2)
        self.run_cli("invocation", "override-plan", "unknown", "--reason", "test", "--ttl-seconds", "0", expected=2)

    def test_retention_recovery_gate_and_batch_partial_resume_are_wired(self):
        from skills_auditor.lifecycle.batch import BatchManager
        from skills_auditor.lifecycle.common import LifecycleError
        from skills_auditor.lifecycle.engine import Manager
        from skills_auditor.lifecycle.retention import apply_retention, plan_retention
        from skills_auditor.lifecycle.snapshots import inspect_source, materialize
        manager = Manager(self.project)
        self.addCleanup(manager.repository.close)
        inspected = inspect_source(self.source)
        materialize(self.source, manager.store_root, inspected["source_tree_sha256"], inspected["snapshot_tree_sha256"])
        collect = plan_retention(manager, "collect")

        def interrupt(name, value):
            if name == "retention:0:effect":
                raise OSError("fixture interruption")

        with self.assertRaises(LifecycleError):
            apply_retention(manager, collect, approve_plan_id=collect["plan_id"], transaction_id="retention-cli-crash", checkpoint=interrupt)
        self.assertEqual(self.run_cli("retention", "recover", "retention-cli-crash")["state"], "recovery_needed")
        self.run_cli("retention", "recover", "retention-cli-crash", "--mode", "compensate", expected=3)
        result = self.run_cli("retention", "recover", "retention-cli-crash", "--mode", "compensate", "--approve-plan-id", collect["plan_id"])
        self.assertEqual(result["state"], "compensated")
        batch = BatchManager(manager)
        plan = batch.plan([manager.plan("install", source=self.source, target=self.target)])

        def pause(name, value):
            if name == "batch:child:0:step:0:effect":
                raise OSError("fixture batch interruption")

        with self.assertRaises(LifecycleError):
            batch.apply(plan, approve_plan_id=plan["plan_id"], batch_id="batch-cli-crash", checkpoint=pause)
        inode = self.target.lstat().st_ino
        self.assertEqual(self.run_cli("batch", "inspect", "batch-cli-crash")["state"], "recovery_needed")
        self.run_cli("batch", "resume", "batch-cli-crash", expected=3)
        self.assertEqual(self.run_cli("batch", "resume", "batch-cli-crash", "--approve-plan-id", plan["plan_id"])["status"], "completed")
        self.assertEqual(self.target.lstat().st_ino, inode)

    def test_incident_list_schema_validates_real_output_and_rejects_bad_items(self):
        try:
            from jsonschema import Draft202012Validator, FormatChecker
            from referencing import Registry, Resource
        except ImportError:
            self.skipTest("install the test extra to validate JSON schemas")
        _, receipt, _ = self.install()
        self.target.unlink()
        self.run_cli("verify", receipt["installation_id"], expected=3)
        value = self.run_cli("incidents")
        directory = Path(__file__).parents[1] / "skills_auditor/schemas"
        schema = json.loads((directory / "lifecycle-incident-list-v1.schema.json").read_text())
        incident = json.loads((directory / "lifecycle-incident-v1.schema.json").read_text())
        registry = Registry().with_resource(incident["$id"], Resource.from_contents(incident))
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema, registry=registry, format_checker=FormatChecker())
        validator.validate(value)
        self.assertFalse(validator.is_valid({**value, "incidents": [{"state": "resolved"}]}))

    def test_plan_apply_verify_and_restart_inspection(self):
        plan, receipt, path = self.install()
        self.assertEqual(json.loads(path.read_text()), plan)
        verified = self.run_cli("verify", receipt["installation_id"])
        self.assertTrue(verified["valid"])
        self.assertEqual(self.run_cli("preflight", receipt["installation_id"])["decision"], "proceed")
        self.assertEqual(self.run_cli("inspect", "receipt", receipt["receipt_id"]), receipt)
        self.assertEqual(self.run_cli("inspect", "transaction", receipt["transaction_id"])["state"], "completed")
        self.assertEqual(len(self.run_cli("list")["installations"]), 1)
        self.assertIn("[OK]", self.run_cli("status", receipt["installation_id"], text=True))

    def test_missing_approval_bad_checksum_and_stale_plan_exit_codes(self):
        path = self.project / "plan.json"
        plan = self.run_cli("plan", "install", "--source", self.source, "--target", self.target, "--plan-out", path)
        self.assertEqual(self.run_cli("apply", path, expected=3)["code"], "approval_required")
        altered = dict(plan, operation="uninstall")
        path.write_text(json.dumps(altered))
        self.assertEqual(self.run_cli("apply", path, "--approve-plan-id", plan["plan_id"], expected=2)["code"], "invalid_plan")
        path.write_text(json.dumps(plan))
        (self.source / "payload").write_text("H2")
        self.assertEqual(self.run_cli("apply", path, "--approve-plan-id", plan["plan_id"], expected=4)["code"], "stale_plan")
        self.assertFalse(self.target.is_symlink())

    def test_text_execution_error_exposes_generated_transaction_and_scoped_inspection(self):
        from skills_auditor.lifecycle.engine import Manager
        path = self.project / "failure-plan.json"
        plan = self.run_cli("plan", "install", "--source", self.source, "--target", self.target, "--plan-out", path)
        with patch.object(Manager, "_effect", side_effect=OSError("fixture failure with PRIVATE CONTENT")):
            output = self.run_cli("apply", path, "--approve-plan-id", plan["plan_id"], expected=3, text=True)
        manager = Manager(self.project, create=False)
        self.addCleanup(manager.repository.close)
        transaction = manager.repository.list("transaction")[0]
        self.assertEqual(transaction["data"]["state"], "recovery_needed")
        self.assertIsNone(transaction["data"]["receipt_id"])
        self.assertIn(transaction["id"], output)
        self.assertNotIn("PRIVATE CONTENT", output)
        line = next(line for line in output.splitlines() if line.startswith("Next (read-only): "))
        tokens = shlex.split(line.partition(": ")[2])
        self.assertEqual(tokens, ["skills-audit", "lifecycle", "--project-root", str(self.project), "recover", transaction["id"], "--mode", "inspect"])
        inspected = self.run_cli(*tokens[4:])
        self.assertEqual(inspected["transaction_id"], transaction["id"])

    def test_text_recovery_hints_are_typed_bounded_and_never_dump_error_details(self):
        from skills_auditor.lifecycle.cli import _render
        from skills_auditor.lifecycle.common import LifecycleError
        for code, details, route in (
            ("transaction_failed", {"transaction_id": "core-123"}, "recover core-123 --mode inspect"),
            ("journal_write_failed", {"transaction_id": "core-456"}, "recover core-456 --mode inspect"),
            ("committed_response_failed", {"transaction_id": "core-789"}, "recover core-789 --mode inspect"),
            ("batch_journal_failed", {"batch_id": "batch-123", "transaction_id": "not-the-parent"}, "batch inspect batch-123"),
            ("retention_journal_failed", {"transaction_id": "retention-123"}, "retention recover retention-123 --mode inspect"),
            ("retention_committed_response_failed", {"transaction_id": "retention-456"}, "retention recover retention-456 --mode inspect"),
            ("retention_compensation_failed", {"transaction_id": "retention-789"}, "retention recover retention-789 --mode inspect"),
        ):
            with self.subTest(code=code):
                payload = LifecycleError(code, "Safe summary", details={**details, "private_extra": "SECRET" * 200000}).to_dict()
                rendered = _render(payload)
                self.assertIn(route, rendered)
                self.assertNotIn("SECRET", rendered)
                self.assertNotIn("not-the-parent", rendered)
                self.assertLess(len(rendered), 1500)
                self.assertEqual(payload["details"]["private_extra"], "SECRET" * 200000)
        for invalid in (None, [], {}, "x" * 201, "bad\nCOMMAND", "../foreign"):
            with self.subTest(invalid=invalid):
                rendered = _render(LifecycleError("transaction_failed", "Safe summary", details={"transaction_id": invalid}).to_dict())
                self.assertNotIn("Next (read-only):", rendered)
        identifier = "id with 'quotes'; not-a-command"
        project = "/tmp/project with 'quotes'"
        rendered = _render(LifecycleError("retention_incomplete", "Safe summary", details={"transaction_id": identifier}).to_dict(), project_root=project)
        line = next(line for line in rendered.splitlines() if line.startswith("Next (read-only): "))
        self.assertEqual(shlex.split(line.partition(": ")[2]), ["skills-audit", "lifecycle", "--project-root", project, "retention", "recover", identifier, "--mode", "inspect"])

    def test_text_retention_recovery_hint_accepts_existing_leading_dash_transaction_ids(self):
        self.run_cli("plan", "install", "--source", self.source, "--target", self.target)
        path = self.project / "retention-failure.json"
        plan = self.run_cli("retention", "plan", "policy", "--keep-recent", "0", "--plan-out", path)
        with patch("skills_auditor.lifecycle.retention._complete", side_effect=OSError("fixture failure")):
            rendered = self.run_cli("retention", "apply", path, "--approve-plan-id", plan["plan_id"], "--transaction-id=-operator", expected=3, text=True)
        line = next(line for line in rendered.splitlines() if line.startswith("Next (read-only): "))
        command = shlex.split(line.partition(": ")[2])
        self.assertEqual(command[:4], ["skills-audit", "lifecycle", "--project-root", str(self.project)])
        record = self.run_cli(*command[4:])
        self.assertEqual(record["transaction_id"], "-operator")
        self.assertEqual(record["state"], "recovery_needed")
        self.assertIsNone(record["receipt_id"])
        self.assertEqual(command[4:], ["retention", "recover", "--mode", "inspect", "--", "-operator"])

    def test_unknown_readers_do_not_initialize_a_repository(self):
        self.assertEqual(self.run_cli("status", "unknown", expected=3)["approval"]["state"], "unknown")
        self.assertEqual(self.run_cli("preflight", "unknown", expected=3)["decision"], "block")
        self.assertEqual(self.run_cli("inspect", "installation", "unknown", expected=3)["code"], "repository_missing")
        for command in (("incidents",), ("investigate", "unknown"), ("invocation", "select", "unknown"), ("retention", "recover", "unknown"), ("batch", "inspect", "unknown")):
            with self.subTest(command=command):
                self.assertEqual(self.run_cli(*command, expected=3)["code"], "repository_missing")
        self.assertFalse((self.project / ".skills-auditor-local").exists())

    def test_plan_output_cannot_overwrite_candidate_managed_state_or_installed_bytes(self):
        payload = self.source / "payload"
        self.assertEqual(self.run_cli("plan", "install", "--source", self.source, "--target", self.target, "--plan-out", payload, expected=3)["code"], "unsafe_plan_output")
        self.assertEqual(payload.read_text(), "H1")
        _, receipt, _ = self.install()
        for output in (self.target / "payload", self.project / ".skills-auditor-local" / "lifecycle" / "plan.json", self.target):
            with self.subTest(output=output):
                self.run_cli("plan", "renew", "--installation-id", receipt["installation_id"], "--plan-out", output, expected=3)
        self.assertEqual((self.target / "payload").read_text(), "H1")

    def test_syntax_and_malformed_json_are_single_error_documents(self):
        self.assertEqual(self.run_cli("apply", expected=2)["code"], "invalid_arguments")
        path = self.project / "invalid.json"
        depth = sys.getrecursionlimit() + 100
        for data in ("not json", '{"x":1,"x":2}', "[]", '{"x":NaN}', '{"deep":' + '[' * depth + '0' + ']' * depth + '}'):
            with self.subTest(data=data):
                path.write_text(data)
                self.run_cli("apply", path, expected=2)
        self.run_cli("apply", self.project / "absent.json", expected=2)

    def test_plan_output_rejects_case_and_unicode_aliases_without_changing_source(self):
        sources = [(self.source, self.project / "CANDIDATE")]
        accented = self.project / "caf\u00e9"
        accented.mkdir()
        (accented / "SKILL.md").write_text("# Unicode source\n")
        sources.append((accented, self.project / unicodedata.normalize("NFD", accented.name)))
        for source, alias in sources:
            with self.subTest(alias=alias):
                original = (source / "SKILL.md").read_bytes()
                result = self.run_cli("plan", "install", "--source", source, "--target", self.target, "--plan-out", alias / "SKILL.md", expected=3)
                self.assertEqual(result["code"], "unsafe_plan_output")
                self.assertEqual((source / "SKILL.md").read_bytes(), original)
                self.assertFalse(self.target.is_symlink())

    def test_recovery_is_inspection_by_default_and_requires_exact_approval_to_resume(self):
        from skills_auditor.lifecycle.common import LifecycleError
        from skills_auditor.lifecycle.engine import Manager
        manager = Manager(self.project)
        self.addCleanup(manager.repository.close)
        plan = manager.plan("install", source=self.source, target=self.target)

        def interrupt(name, transaction):
            if name == "step:0:effect":
                raise OSError("fixture interruption")

        with self.assertRaises(LifecycleError):
            manager.apply(plan, approve_plan_id=plan["plan_id"], transaction_id="interrupted", checkpoint=interrupt)
        self.assertEqual(self.run_cli("recover", "interrupted")["state"], "recovery_needed")
        self.run_cli("recover", "interrupted", "--mode", "resume", expected=3)
        final = self.run_cli("recover", "interrupted", "--mode", "resume", "--approve-plan-id", plan["plan_id"])
        self.assertEqual(final["status"], "completed")
        self.assertEqual((self.target / "payload").read_text(), "H1")

    def test_explicit_freshness_integer_is_accepted_and_invalid_policies_rejected(self):
        _, receipt, _ = self.install()
        self.run_cli("verify", receipt["installation_id"])
        self.assertEqual(self.run_cli("status", receipt["installation_id"], "--max-age-seconds", "300")["severity"], "ok")
        for value in ("1.5", "true", "0", "nan", "-1", "86401"):
            with self.subTest(value=value):
                self.run_cli("preflight", receipt["installation_id"], "--cached", "--max-age-seconds", value, expected=2)

    def test_failed_verification_exposes_a_durable_inspectable_incident_entry(self):
        _, receipt, _ = self.install()
        self.target.unlink()
        verification = self.run_cli("verify", receipt["installation_id"], expected=3)
        status = self.run_cli("status", receipt["installation_id"], expected=3)
        self.assertEqual(len(status["incident_ids"]), 1)
        incident = self.run_cli("inspect", "incident", status["incident_ids"][0])
        self.assertEqual(incident["opening_verification_id"], verification["verification_id"])
        self.assertEqual(incident["grant_id"], receipt["grant_id"])
        self.assertEqual(incident["state"], "open")
        self.assertIn("inspect incident " + incident["incident_id"], self.run_cli("status", receipt["installation_id"], expected=3, text=True))
