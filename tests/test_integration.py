"""High-level integration contract and CLI tests."""

import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from skills_auditor.cli import main
from skills_auditor.integration import (
    ERROR_SCHEMA,
    PLAN_SCHEMA,
    RECEIPT_SCHEMA,
    SPEC_SCHEMA,
    VERIFICATION_SCHEMA,
    IntegrationError,
    IntegrationSpec,
    IntegrationTarget,
    _plan_id,
    apply_integration_plan,
    build_integration_plan,
    load_integration_spec,
    verify_receipt,
)


class IntegrationFixture(unittest.TestCase):
    def write_skill(self, root: Path, name: str, body: str = "body") -> Path:
        skill = root / name
        skill.mkdir(parents=True, exist_ok=True)
        (skill / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: integration fixture\n---\n\n{body}\n",
            encoding="utf-8",
        )
        return skill

    def spec(self, project: Path, source: Path, target: Path) -> IntegrationSpec:
        return IntegrationSpec(
            project_root=project,
            sources=(source,),
            targets=(IntegrationTarget("test-host", root=target),),
        )

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        stdout = StringIO()
        stderr = StringIO()
        with patch("sys.argv", ["skills-audit", *argv]), redirect_stdout(
            stdout
        ), redirect_stderr(stderr):
            code = main()
        return code, stdout.getvalue(), stderr.getvalue()


class TestIntegrationCore(IntegrationFixture):
    def test_plan_apply_verify_and_replan_noop(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            project = Path(base)
            source = project / ".agents" / "skills"
            target = project / ".codex" / "skills"
            canonical = self.write_skill(source, "product-research", "v1")

            plan = build_integration_plan(self.spec(project, source, target))
            self.assertEqual(plan["schema_version"], PLAN_SCHEMA)
            self.assertEqual(plan["summary"]["changes"], 1)
            self.assertEqual(plan["targets"][0]["actions"][0]["action"], "create_link")

            receipt, receipt_path = apply_integration_plan(plan)
            self.assertTrue(receipt_path.is_file())
            self.assertEqual(receipt["schema_version"], RECEIPT_SCHEMA)
            self.assertEqual((target / "product-research").resolve(), canonical.resolve())

            verification = verify_receipt(receipt)
            self.assertEqual(verification["schema_version"], VERIFICATION_SCHEMA)
            self.assertEqual(verification["status"], "passed")

            second = build_integration_plan(self.spec(project, source, target))
            self.assertEqual(second["summary"]["changes"], 0)
            self.assertEqual(second["targets"][0]["actions"][0]["action"], "noop")

    def test_source_change_rejects_reviewed_plan_without_writing_target(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            project = Path(base)
            source = project / "source"
            target = project / "target"
            canonical = self.write_skill(source, "alpha", "before")
            plan = build_integration_plan(self.spec(project, source, target))
            (canonical / "SKILL.md").write_text(
                "---\nname: alpha\ndescription: integration fixture\n---\n\nafter\n",
                encoding="utf-8",
            )

            with self.assertRaises(IntegrationError) as caught:
                apply_integration_plan(plan)
            self.assertEqual(caught.exception.code, "stale_plan")
            self.assertFalse((target / "alpha").exists())

    def test_payload_change_rejects_plan_and_fails_later_verification(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            project = Path(base)
            source = project / "source"
            target = project / "target"
            canonical = self.write_skill(source, "alpha")
            scripts = canonical / "scripts"
            scripts.mkdir()
            payload = scripts / "run.py"
            payload.write_text("print('v1')\n", encoding="utf-8")

            stale_plan = build_integration_plan(self.spec(project, source, target))
            payload.write_text("print('v2')\n", encoding="utf-8")
            with self.assertRaises(IntegrationError) as caught:
                apply_integration_plan(stale_plan)
            self.assertEqual(caught.exception.code, "stale_plan")
            self.assertFalse((target / "alpha").exists())

            fresh_plan = build_integration_plan(self.spec(project, source, target))
            receipt, _ = apply_integration_plan(fresh_plan)
            payload.write_text("print('v3')\n", encoding="utf-8")
            verification = verify_receipt(receipt)
            self.assertEqual(verification["status"], "failed")
            self.assertIn(
                "source_tree",
                {check["code"] for check in verification["checks"] if not check["ok"]},
            )

    def test_target_change_rejects_reviewed_plan(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            project = Path(base)
            source = project / "source"
            target = project / "target"
            self.write_skill(source, "alpha")
            plan = build_integration_plan(self.spec(project, source, target))
            self.write_skill(target, "alpha", "local")

            with self.assertRaises(IntegrationError) as caught:
                apply_integration_plan(plan)
            self.assertEqual(caught.exception.code, "stale_plan")
            self.assertFalse((target / "alpha").is_symlink())

    def test_existing_native_entry_is_archived(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            project = Path(base)
            source = project / "source"
            target = project / "target"
            canonical = self.write_skill(source, "alpha", "canonical")
            self.write_skill(target, "alpha", "local")
            plan = build_integration_plan(self.spec(project, source, target))
            self.assertEqual(plan["targets"][0]["actions"][0]["action"], "archive_and_link")

            receipt, _ = apply_integration_plan(plan)
            archives = list(target.glob("alpha.archived-*"))
            self.assertEqual(len(archives), 1)
            self.assertIn("local", (archives[0] / "SKILL.md").read_text(encoding="utf-8"))
            self.assertEqual((target / "alpha").resolve(), canonical.resolve())
            self.assertEqual(receipt["status"], "completed")
            self.assertEqual(
                Path(receipt["results"][0]["archive_path"]).resolve(strict=False),
                archives[0].resolve(strict=False),
            )

    def test_archive_action_restores_native_entry_when_link_creation_fails(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            project = Path(base)
            source = project / "source"
            target = project / "target"
            receipt_path = project / "failed-receipt.json"
            self.write_skill(source, "alpha", "canonical")
            native = self.write_skill(target, "alpha", "local")
            plan = build_integration_plan(self.spec(project, source, target))

            with patch(
                "skills_auditor.integration.os.symlink",
                side_effect=OSError("injected link failure"),
            ):
                with self.assertRaises(IntegrationError) as caught:
                    apply_integration_plan(plan, receipt_path)

            self.assertEqual(caught.exception.code, "apply_failed")
            self.assertTrue(native.is_dir())
            self.assertIn("local", (native / "SKILL.md").read_text(encoding="utf-8"))
            self.assertFalse(Path(plan["targets"][0]["actions"][0]["archive_path"]).exists())
            failed = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["results"], [])

    def test_archive_destination_never_collides_with_another_planned_entry(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            project = Path(base)
            source = project / "source"
            target = project / "target"
            canonical = self.write_skill(source, "alpha", "canonical")
            colliding_name = "alpha.archived-20260719T000000Z"
            second = self.write_skill(source, colliding_name, "second")
            self.write_skill(target, "alpha", "local")

            with patch(
                "skills_auditor.integration._utc_now",
                return_value="2026-07-19T00:00:00Z",
            ):
                plan = build_integration_plan(self.spec(project, source, target))
            archive_action = plan["targets"][0]["actions"][0]
            self.assertTrue(archive_action["archive_path"].endswith(".1"))

            apply_integration_plan(plan)
            self.assertEqual((target / "alpha").resolve(), canonical.resolve())
            self.assertEqual((target / colliding_name).resolve(), second.resolve())

    def test_all_targets_are_preflighted_before_first_write(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            project = Path(base)
            source = project / "source"
            first = project / "first"
            second = project / "second"
            self.write_skill(source, "alpha")
            spec = IntegrationSpec(
                project_root=project,
                sources=(source,),
                targets=(
                    IntegrationTarget("first", root=first),
                    IntegrationTarget("second", root=second),
                ),
            )
            plan = build_integration_plan(spec)
            self.write_skill(second, "alpha", "late local entry")

            with self.assertRaises(IntegrationError) as caught:
                apply_integration_plan(plan)
            self.assertEqual(caught.exception.code, "stale_plan")
            self.assertFalse((first / "alpha").exists())

    def test_same_definition_with_different_payload_is_a_source_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            project = Path(base)
            first_source = project / "source-a"
            second_source = project / "source-b"
            first = self.write_skill(first_source, "alpha")
            second = self.write_skill(second_source, "alpha")
            (first / "tool.txt").write_text("one\n", encoding="utf-8")
            (second / "tool.txt").write_text("two\n", encoding="utf-8")
            spec = IntegrationSpec(
                project_root=project,
                sources=(first_source, second_source),
                targets=(IntegrationTarget("test", root=project / "target"),),
            )

            with self.assertRaises(IntegrationError) as caught:
                build_integration_plan(spec)
            self.assertEqual(caught.exception.code, "source_conflict")

    def test_source_symlink_cannot_escape_the_skill_tree(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            project = Path(base)
            source = project / "source"
            target = project / "target"
            canonical = self.write_skill(source, "alpha")
            external = project / "shared-secret.txt"
            external.write_text("outside\n", encoding="utf-8")
            (canonical / "payload.txt").symlink_to(external)

            with self.assertRaises(IntegrationError) as caught:
                build_integration_plan(self.spec(project, source, target))
            self.assertEqual(caught.exception.code, "source_symlink_escape")

    def test_named_environment_resolves_project_root(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            project = Path(base)
            source = project / ".agents" / "skills"
            self.write_skill(source, "alpha")
            spec = IntegrationSpec(
                project_root=project,
                sources=(source,),
                targets=(
                    IntegrationTarget("cursor"),
                    IntegrationTarget("claude-code"),
                    IntegrationTarget("codex"),
                ),
            )
            plan = build_integration_plan(spec)
            self.assertEqual(
                [target["root"] for target in plan["targets"]],
                [
                    str((project / ".cursor" / "skills").resolve(strict=False)),
                    str((project / ".claude" / "skills").resolve(strict=False)),
                    str((project / ".codex" / "skills").resolve(strict=False)),
                ],
            )

    def test_named_environment_resolves_global_root(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            project = Path(base)
            source = project / "source"
            fake_home = project / "home"
            self.write_skill(source, "alpha")
            spec = IntegrationSpec(
                project_root=project,
                sources=(source,),
                targets=(IntegrationTarget("codex", scope="global"),),
            )
            with patch("skills_auditor.integration.Path.home", return_value=fake_home):
                plan = build_integration_plan(spec)
            self.assertEqual(
                plan["targets"][0]["root"],
                str((fake_home / ".codex" / "skills").resolve(strict=False)),
            )

    def test_target_inside_source_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            project = Path(base)
            source = project / "source"
            self.write_skill(source, "alpha")
            with self.assertRaises(IntegrationError) as caught:
                build_integration_plan(self.spec(project, source, source / "install"))
            self.assertEqual(caught.exception.code, "target_inside_source")

    def test_source_inside_target_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            project = Path(base)
            target = project / "target"
            source = target / "canonical"
            self.write_skill(source, "alpha")

            with self.assertRaises(IntegrationError) as caught:
                build_integration_plan(self.spec(project, source, target))
            self.assertEqual(caught.exception.code, "source_inside_target")

    def test_target_roots_cannot_contain_each_other(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            project = Path(base)
            source = project / "source"
            target = project / "target"
            self.write_skill(source, "alpha")
            spec = IntegrationSpec(
                project_root=project,
                sources=(source,),
                targets=(
                    IntegrationTarget("outer", root=target),
                    IntegrationTarget("inner", root=target / "nested"),
                ),
            )

            with self.assertRaises(IntegrationError) as caught:
                build_integration_plan(spec)
            self.assertEqual(caught.exception.code, "overlapping_targets")

    def test_rehashed_plan_cannot_escape_target_with_a_traversal_name(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            project = Path(base)
            source = project / "source"
            target = project / "target"
            self.write_skill(source, "alpha")
            plan = build_integration_plan(self.spec(project, source, target))
            plan["source_skills"][0]["name"] = "../escape"
            plan["targets"][0]["actions"][0]["name"] = "../escape"
            plan["plan_id"] = _plan_id(plan)

            with self.assertRaises(IntegrationError) as caught:
                apply_integration_plan(plan)
            self.assertEqual(caught.exception.code, "invalid_plan")
            self.assertFalse((project / "escape").exists())


class TestIntegrationConfig(IntegrationFixture):
    def test_config_paths_are_relative_to_project_root(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            project = Path(base)
            config = project / "skills-auditor.json"
            config.write_text(
                json.dumps(
                    {
                        "schema_version": SPEC_SCHEMA,
                        "sources": [".agents/skills"],
                        "targets": ["codex"],
                        "metadata_platform": "codex",
                    }
                ),
                encoding="utf-8",
            )
            spec = load_integration_spec(config_path=config, cwd=project)
            self.assertEqual(spec.project_root, project.resolve(strict=False))
            self.assertEqual(
                spec.sources,
                ((project / ".agents" / "skills").resolve(strict=False),),
            )
            self.assertEqual(spec.targets[0].environment, "codex")


class TestIntegrationCli(IntegrationFixture):
    def test_json_cli_is_parseable_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            project = Path(base)
            source = project / "source"
            target = project / "target"
            plan_path = project / "plan.json"
            receipt_path = project / "receipt.json"
            self.write_skill(source, "alpha")

            code, stdout, stderr = self.run_cli(
                "integrate",
                "--source",
                str(source),
                "--target-root",
                f"test={target}",
                "--plan-out",
                str(plan_path),
                "--format",
                "json",
            )
            self.assertEqual(code, 0, stderr)
            plan_output = json.loads(stdout)
            self.assertEqual(plan_output["schema_version"], PLAN_SCHEMA)
            self.assertEqual(plan_output["plan_path"], str(plan_path.resolve(strict=False)))

            code, stdout, stderr = self.run_cli(
                "apply",
                str(plan_path),
                "--receipt-out",
                str(receipt_path),
                "--format",
                "json",
            )
            self.assertEqual(code, 0, stderr)
            receipt_output = json.loads(stdout)
            self.assertEqual(receipt_output["schema_version"], RECEIPT_SCHEMA)
            self.assertEqual(
                receipt_output["receipt_path"], str(receipt_path.resolve(strict=False))
            )

            code, stdout, stderr = self.run_cli(
                "verify", str(receipt_path), "--format", "json"
            )
            self.assertEqual(code, 0, stderr)
            self.assertEqual(json.loads(stdout)["status"], "passed")

    def test_json_error_has_stable_schema_and_exit_code(self) -> None:
        code, stdout, stderr = self.run_cli("integrate", "--format", "json")
        self.assertEqual(code, 2)
        self.assertEqual(stderr, "")
        error = json.loads(stdout)
        self.assertEqual(error["schema_version"], ERROR_SCHEMA)
        self.assertEqual(error["error"]["code"], "missing_sources")

    def test_stale_plan_cli_returns_contract_exit(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            project = Path(base)
            source = project / "source"
            target = project / "target"
            plan_path = project / "plan.json"
            skill = self.write_skill(source, "alpha", "before")
            code, _, _ = self.run_cli(
                "integrate",
                "--source",
                str(source),
                "--target-root",
                f"test={target}",
                "--plan-out",
                str(plan_path),
            )
            self.assertEqual(code, 0)
            (skill / "SKILL.md").write_text(
                "---\nname: alpha\ndescription: integration fixture\n---\n\nafter\n",
                encoding="utf-8",
            )
            code, stdout, _ = self.run_cli(
                "apply", str(plan_path), "--format", "json"
            )
            self.assertEqual(code, 3)
            self.assertEqual(json.loads(stdout)["error"]["code"], "stale_plan")

    def test_shipped_json_schemas_parse(self) -> None:
        try:
            from jsonschema import Draft202012Validator, validate
        except ImportError:
            self.skipTest("install the test extra to validate public JSON schemas")

        schema_root = Path(__file__).parents[1] / "skills_auditor" / "schemas"
        schemas = {
            path.name: json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(schema_root.glob("*.schema.json"))
        }
        self.assertEqual(len(schemas), 5)
        for name, schema in schemas.items():
            with self.subTest(schema=name):
                Draft202012Validator.check_schema(schema)

        with tempfile.TemporaryDirectory() as base:
            project = Path(base)
            source = project / "source"
            target = project / "target"
            self.write_skill(source, "alpha")
            plan = build_integration_plan(self.spec(project, source, target))
            receipt, _ = apply_integration_plan(plan)
            verification = verify_receipt(receipt)
            documents = {
                "integration-spec-v1.schema.json": plan["spec"],
                "integration-plan-v1.schema.json": plan,
                "integration-receipt-v1.schema.json": receipt,
                "integration-verification-v1.schema.json": verification,
                "error-v1.schema.json": IntegrationError("probe", "probe").to_dict(),
            }
            for name, document in documents.items():
                with self.subTest(document=name):
                    validate(instance=document, schema=schemas[name])
