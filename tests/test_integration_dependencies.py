"""Required files extend the reviewed deployment, not the upstream package lock."""
import copy
import json
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tests.test_integration import IntegrationFixture
from skills_auditor.integration import (
    IntegrationError, _apply_exact_action, _plan_id, _receipt_id,
    apply_integration_plan, build_integration_plan, load_integration_spec,
    validate_plan, validate_receipt, verify_receipt,
)


class TestRequiredFiles(IntegrationFixture):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.source = self.root / "payload/skills"
        self.target = self.root / "host/skills"
        self.write_skill(self.source, "alpha")
        self.dep = self.root / "payload/helper.py"
        self.dep.write_text("print('ready')\n")
        self.specification = replace(self.spec(self.root, self.source, self.target), required_files=(self.dep,))

    def test_missing_external_dependency_blocks_plan_and_apply_without_writes(self):
        plan = build_integration_plan(self.specification)
        self.dep.unlink()
        with self.assertRaises(IntegrationError) as missing:
            build_integration_plan(self.specification)
        self.assertEqual(missing.exception.code, "dependency_unavailable")
        with self.assertRaises(IntegrationError) as stale:
            apply_integration_plan(plan)
        self.assertEqual(stale.exception.code, "stale_plan")
        self.assertEqual(stale.exception.details[0]["code"], "required_file")
        self.assertFalse(self.target.exists())

    def test_receipt_detects_bytes_modes_missing_and_replaced_symlink(self):
        receipt, _ = apply_integration_plan(build_integration_plan(self.specification))
        original, mode = self.dep.read_bytes(), self.dep.stat().st_mode & 0o7777
        for change in ("bytes", "mode", "missing", "symlink"):
            with self.subTest(change=change):
                if change == "bytes":
                    self.dep.write_bytes(b"changed")
                elif change == "mode":
                    self.dep.chmod(mode ^ 0o100)
                elif change == "missing":
                    self.dep.unlink()
                else:
                    other = self.root / "other.py"
                    other.write_bytes(original)
                    self.dep.unlink()
                    self.dep.symlink_to(other)
                result = verify_receipt(receipt)
                self.assertEqual(result["status"], "failed")
                failed = [c["code"] for c in result["checks"] if not c["ok"]]
                self.assertEqual(failed, ["required_file"])
                if self.dep.is_symlink():
                    self.dep.unlink()
                self.dep.write_bytes(original)
                self.dep.chmod(mode)
                self.assertEqual(verify_receipt(receipt)["status"], "passed")

    def test_mid_apply_change_produces_failed_receipt(self):
        plan = build_integration_plan(self.specification)
        output = self.root / "failed.json"
        def change_after_link(root, action):
            _apply_exact_action(root, action)
            self.dep.write_text("changed during apply")
        with patch("skills_auditor.integration._apply_exact_action", side_effect=change_after_link):
            with self.assertRaises(IntegrationError) as caught:
                apply_integration_plan(plan, output)
        self.assertEqual(caught.exception.code, "apply_verification_failed")
        receipt = json.loads(output.read_text())
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(verify_receipt(receipt)["status"], "failed")

    def test_change_between_actions_stops_remaining_writes(self):
        self.write_skill(self.source, "beta")
        plan = build_integration_plan(self.specification)
        def change_after_link(root, action):
            _apply_exact_action(root, action)
            self.dep.unlink()
        with patch("skills_auditor.integration._apply_exact_action", side_effect=change_after_link):
            with self.assertRaises(IntegrationError) as caught:
                apply_integration_plan(plan, self.root / "failed.json")
        self.assertEqual(caught.exception.code, "stale_plan")
        self.assertTrue((self.target / "alpha").is_symlink())
        self.assertFalse((self.target / "beta").exists())

    def test_cli_config_paths_override_and_schema_roundtrip(self):
        config = self.root / "skills-auditor.json"
        config.write_text(json.dumps({
            "schema_version": "skills-auditor-integration/v1", "project_root": ".",
            "sources": ["payload/skills"], "targets": [{"environment": "test", "root": "host/skills"}],
            "required_files": ["payload/helper.py", "payload/helper.py"],
        }))
        spec = load_integration_spec(config_path=config, cwd=self.root)
        self.assertEqual(spec.required_files, (self.dep,))
        override = self.root / "other.txt"
        override.write_text("other")
        self.assertEqual(load_integration_spec(config_path=config, cwd=self.root,
                         cli_required_files=["other.txt"]).required_files, (override,))
        plan_path, receipt_path = self.root / "plan.json", self.root / "receipt.json"
        code, output, _ = self.run_cli("integrate", "--config", str(config),
            "--require-file", str(self.dep), "--plan-out", str(plan_path), "--format", "json")
        self.assertEqual(code, 0, output)
        code, output, _ = self.run_cli("apply", str(plan_path), "--receipt-out", str(receipt_path), "--format", "json")
        self.assertEqual(code, 0, output)
        from jsonschema import validate
        schemas = Path(__file__).parents[1] / "skills_auditor/schemas"
        plan, receipt = json.loads(plan_path.read_text()), json.loads(receipt_path.read_text())
        for name, value in (("spec", plan["spec"]), ("plan", plan), ("receipt", receipt)):
            validate(value, json.loads((schemas / f"integration-{name}-v1.schema.json").read_text()))
        self.dep.unlink()
        code, output, _ = self.run_cli("verify", str(receipt_path), "--format", "json")
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(output)["checks"][0]["code"], "required_file")

    def test_malformed_and_mismatched_dependencies_are_rejected(self):
        plan = build_integration_plan(self.specification)
        for records in (None, "bad", [{}], [dict(plan["dependencies"][0], mode=True)],
                        plan["dependencies"] * 2, []):
            invalid = copy.deepcopy(plan)
            invalid["dependencies"] = records
            invalid["plan_id"] = _plan_id(invalid)
            with self.subTest(records=records), self.assertRaises(IntegrationError):
                validate_plan(invalid)
        receipt, _ = apply_integration_plan(plan)
        receipt["dependencies"][0]["sha256"] = "invalid"
        receipt["receipt_id"] = _receipt_id(receipt)
        with self.assertRaises(IntegrationError):
            validate_receipt(receipt)

    def test_config_rejects_invalid_required_files(self):
        for values in (None, "helper.py", [3], [""]):
            config = self.root / "config.json"
            config.write_text(json.dumps({"schema_version": "skills-auditor-integration/v1",
                "sources": [str(self.source)], "targets": ["codex"], "required_files": values}))
            with self.subTest(values=values), self.assertRaises(IntegrationError) as caught:
                load_integration_spec(config_path=config)
            self.assertEqual(caught.exception.code, "invalid_required_files")

    def test_target_dependency_and_non_file_are_rejected(self):
        self.target.mkdir(parents=True)
        inside = self.target / "helper.py"
        inside.write_text("would be inside registration root")
        for path, code in ((inside, "dependency_inside_target"), (self.dep.parent, "dependency_unavailable")):
            with self.subTest(path=path), self.assertRaises(IntegrationError) as caught:
                build_integration_plan(replace(self.specification, required_files=(path,)))
            self.assertEqual(caught.exception.code, code)
