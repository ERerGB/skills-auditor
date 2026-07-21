"""Adversarial validation and failure-path tests for the integration contract."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from skills_auditor.integration import (
    PLAN_SCHEMA,
    RECEIPT_SCHEMA,
    SPEC_SCHEMA,
    IntegrationError,
    IntegrationSpec,
    IntegrationTarget,
    _apply_exact_action,
    _build_receipt,
    _plan_id,
    _receipt_id,
    _snapshot_for_plan,
    _source_tree_hash,
    apply_integration_plan,
    build_integration_plan,
    check_plan_preconditions,
    entry_snapshot,
    load_integration_spec,
    load_json_object,
    save_plan,
    validate_plan,
    validate_receipt,
    verify_receipt,
)


class IntegrationValidationFixture(unittest.TestCase):
    def write_skill(self, root: Path, name: str = "alpha", body: str = "body") -> Path:
        skill = root / name
        skill.mkdir(parents=True, exist_ok=True)
        (skill / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: validation fixture\n---\n\n{body}\n",
            encoding="utf-8",
        )
        return skill

    def valid_plan(self, project: Path, *, native: bool = False) -> dict:
        source = project / "source"
        target = project / "target"
        self.write_skill(source)
        if native:
            self.write_skill(target, body="native")
        return build_integration_plan(
            IntegrationSpec(
                project_root=project,
                sources=(source,),
                targets=(IntegrationTarget("test", root=target),),
            )
        )

    def rehash_plan(self, plan: dict) -> dict:
        plan["plan_id"] = _plan_id(plan)
        return plan

    def rehash_receipt(self, receipt: dict) -> dict:
        receipt["receipt_id"] = _receipt_id(receipt)
        return receipt

    def assert_integration_error(self, code: str, operation) -> IntegrationError:
        with self.assertRaises(IntegrationError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)
        return caught.exception


class TestIntegrationConfigValidation(IntegrationValidationFixture):
    def test_config_shape_and_path_errors_are_stable(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            project = Path(base)
            config = project / "config.json"

            self.assert_integration_error(
                "config_not_found",
                lambda: load_integration_spec(config_path=project / "missing.json", cwd=project),
            )
            config.write_text("{", encoding="utf-8")
            self.assert_integration_error(
                "invalid_config", lambda: load_integration_spec(config_path=config, cwd=project)
            )
            for label, payload, code in (
                ("non-object", [], "invalid_config"),
                ("unknown", {"schema_version": SPEC_SCHEMA, "extra": True}, "invalid_config"),
                ("schema", {"schema_version": "wrong"}, "invalid_config_schema"),
                (
                    "project-type",
                    {"schema_version": SPEC_SCHEMA, "project_root": 7},
                    "invalid_project_root",
                ),
                (
                    "project-missing",
                    {"schema_version": SPEC_SCHEMA, "project_root": "missing"},
                    "invalid_project_root",
                ),
                (
                    "sources-type",
                    {"schema_version": SPEC_SCHEMA, "sources": "source", "targets": ["codex"]},
                    "missing_sources",
                ),
                (
                    "sources-entry",
                    {"schema_version": SPEC_SCHEMA, "sources": [7], "targets": ["codex"]},
                    "invalid_sources",
                ),
                (
                    "targets-type",
                    {"schema_version": SPEC_SCHEMA, "sources": ["source"], "targets": "codex"},
                    "invalid_targets",
                ),
                (
                    "targets-empty",
                    {"schema_version": SPEC_SCHEMA, "sources": ["source"], "targets": []},
                    "missing_targets",
                ),
                (
                    "metadata",
                    {
                        "schema_version": SPEC_SCHEMA,
                        "sources": ["source"],
                        "targets": ["codex"],
                        "metadata_platform": 7,
                    },
                    "invalid_metadata_platform",
                ),
            ):
                with self.subTest(label=label):
                    config.write_text(json.dumps(payload), encoding="utf-8")
                    self.assert_integration_error(
                        code, lambda: load_integration_spec(config_path=config, cwd=project)
                    )

    def test_target_parser_rejects_every_invalid_form(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            project = Path(base)
            source = project / "source"
            source.mkdir()
            config = project / "config.json"
            invalid_targets = (
                ("empty", [""], "invalid_target"),
                ("scope", ["codex@team"], "invalid_target_scope"),
                ("scalar", [7], "invalid_target"),
                ("unknown", [{"environment": "codex", "extra": 1}], "invalid_target"),
                ("environment", [{"environment": ""}], "invalid_target"),
                ("object-scope", [{"environment": "codex", "scope": "team"}], "invalid_target_scope"),
                ("root", [{"environment": "codex", "root": 7}], "invalid_target_root"),
            )
            for label, targets, code in invalid_targets:
                with self.subTest(label=label):
                    config.write_text(
                        json.dumps(
                            {
                                "schema_version": SPEC_SCHEMA,
                                "sources": ["source"],
                                "targets": targets,
                            }
                        ),
                        encoding="utf-8",
                    )
                    self.assert_integration_error(
                        code, lambda: load_integration_spec(config_path=config, cwd=project)
                    )

            self.assert_integration_error(
                "invalid_target_root",
                lambda: load_integration_spec(
                    cli_sources=[str(source)],
                    cli_target_roots=["broken"],
                    cwd=project,
                ),
            )

    def test_cli_values_override_config_and_deduplicate_sources(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            project = Path(base)
            configured = project / "configured"
            override = project / "override"
            configured.mkdir()
            override.mkdir()
            config = project / "config.json"
            config.write_text(
                json.dumps(
                    {
                        "schema_version": SPEC_SCHEMA,
                        "sources": ["configured"],
                        "targets": ["cursor"],
                    }
                ),
                encoding="utf-8",
            )
            spec = load_integration_spec(
                config_path=config,
                cli_sources=[str(override), str(override)],
                cli_target_roots=[f"custom={project / 'target'}"],
                metadata_platform="claude-code",
                cwd=project,
            )
            self.assertEqual(spec.sources, (override.resolve(),))
            self.assertEqual(spec.targets[0].environment, "custom")
            self.assertEqual(spec.metadata_platform, "claude-code")


class TestPlanValidation(IntegrationValidationFixture):
    def test_top_level_plan_invariants_reject_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            original = self.valid_plan(Path(base))

            broken = copy.deepcopy(original)
            broken["schema_version"] = "wrong"
            self.assert_integration_error("invalid_plan_schema", lambda: validate_plan(broken))

            broken = copy.deepcopy(original)
            broken["summary"]["changes"] += 1
            self.assert_integration_error("invalid_plan_id", lambda: validate_plan(broken))

            mutations = []

            def add(label, expected, mutate):
                mutations.append((label, expected, mutate))

            add("unknown-field", "invalid_plan", lambda p: p.__setitem__("extra", True))
            add("spec-type", "invalid_plan", lambda p: p.__setitem__("spec", []))
            add("project-root", "invalid_plan", lambda p: p["spec"].__setitem__("project_root", "."))
            add("sources-empty", "invalid_plan", lambda p: p["spec"].__setitem__("sources", []))
            add("source-records-empty", "invalid_plan", lambda p: p.__setitem__("source_skills", []))
            add("targets-empty", "invalid_plan", lambda p: p.__setitem__("targets", []))
            add("bad-source-name", "invalid_plan", lambda p: p["source_skills"][0].__setitem__("name", "../bad"))
            add(
                "duplicate-source",
                "invalid_plan",
                lambda p: p["source_skills"].append(copy.deepcopy(p["source_skills"][0])),
            )
            add("spec-target-count", "invalid_plan", lambda p: p["spec"].__setitem__("targets", []))
            add("bad-target", "invalid_plan", lambda p: p["targets"][0].__setitem__("scope", "team"))
            add(
                "target-spec-mismatch",
                "invalid_plan",
                lambda p: p["spec"]["targets"][0].__setitem__("environment", "other"),
            )
            add(
                "overlap-source-target",
                "invalid_plan",
                lambda p: (
                    p["targets"][0].__setitem__("root", p["spec"]["sources"][0]),
                    p["spec"]["targets"][0].__setitem__("root", p["spec"]["sources"][0]),
                ),
            )
            add(
                "bad-action",
                "invalid_plan_action",
                lambda p: p["targets"][0]["actions"][0].__setitem__("action", "delete"),
            )
            add(
                "missing-source-action",
                "invalid_plan_action",
                lambda p: p["targets"][0]["actions"][0].__setitem__("name", "other"),
            )
            add(
                "source-mismatch",
                "invalid_plan_action",
                lambda p: p["targets"][0]["actions"][0].__setitem__("expected_tree_sha256", "0" * 64),
            )
            add(
                "snapshot-contradiction",
                "invalid_plan_action",
                lambda p: p["targets"][0]["actions"][0].__setitem__(
                    "entry_before", {"kind": "symlink", "target": "x", "resolved": "x"}
                ),
            )
            add(
                "nonarchive-destination",
                "invalid_plan_action",
                lambda p: p["targets"][0]["actions"][0].__setitem__(
                    "archive_path", p["targets"][0]["root"] + "/archive"
                ),
            )
            add(
                "missing-action",
                "invalid_plan_action",
                lambda p: p["targets"][0].__setitem__("actions", []),
            )
            add("summary", "invalid_plan", lambda p: p["summary"].__setitem__("changes", 99))

            for label, expected, mutate in mutations:
                with self.subTest(label=label):
                    broken = copy.deepcopy(original)
                    mutate(broken)
                    self.rehash_plan(broken)
                    self.assert_integration_error(expected, lambda: validate_plan(broken))

    def test_archive_plan_path_invariants_reject_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            original = self.valid_plan(Path(base), native=True)
            for label, mutate in (
                (
                    "missing-reservation",
                    lambda a: a.__setitem__("archive_before", {"kind": "file", "sha256": "0" * 64}),
                ),
                ("outside-root", lambda a: a.__setitem__("archive_path", str(Path(base) / "outside"))),
            ):
                with self.subTest(label=label):
                    broken = copy.deepcopy(original)
                    mutate(broken["targets"][0]["actions"][0])
                    self.rehash_plan(broken)
                    self.assert_integration_error(
                        "invalid_plan_action", lambda: validate_plan(broken)
                    )

    def test_plan_io_and_snapshot_failures_have_contract_codes(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            project = Path(base)
            plan = self.valid_plan(project)
            default_path = save_plan(plan)
            self.assertTrue(default_path.is_file())

            with patch("skills_auditor.integration._atomic_write_json", side_effect=OSError("disk")):
                self.assert_integration_error("plan_write_failed", lambda: save_plan(plan))
            with patch("skills_auditor.integration.entry_snapshot", side_effect=OSError("read")):
                self.assert_integration_error(
                    "target_unreadable", lambda: _snapshot_for_plan(project / "target")
                )
            with patch("skills_auditor.integration.directory_tree_hash", side_effect=OSError("read")):
                self.assert_integration_error(
                    "source_unreadable", lambda: _source_tree_hash(project / "source")
                )

            non_object = project / "array.json"
            non_object.write_text("[]", encoding="utf-8")
            self.assert_integration_error(
                "invalid_probe", lambda: load_json_object(non_object, "probe")
            )

    def test_preconditions_report_source_target_and_archive_changes(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            project = Path(base)
            plan = self.valid_plan(project, native=True)
            source = Path(plan["source_skills"][0]["skill_root"])
            target = Path(plan["targets"][0]["root"])
            archive = Path(plan["targets"][0]["actions"][0]["archive_path"])
            (source / "payload.txt").write_text("changed\n", encoding="utf-8")
            (target / "alpha" / "local.txt").write_text("changed\n", encoding="utf-8")
            archive.write_text("occupied\n", encoding="utf-8")
            codes = {issue["code"] for issue in check_plan_preconditions(plan)}
            self.assertEqual(codes, {"source_changed", "target_changed", "archive_changed"})


class TestReceiptValidation(IntegrationValidationFixture):
    def completed_receipt(self, project: Path) -> tuple[dict, dict]:
        plan = self.valid_plan(project)
        receipt, _ = apply_integration_plan(plan, project / "receipt.json")
        return plan, receipt

    def test_receipt_invariants_reject_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            _, original = self.completed_receipt(Path(base))

            broken = copy.deepcopy(original)
            broken["schema_version"] = "wrong"
            self.assert_integration_error("invalid_receipt_schema", lambda: validate_receipt(broken))
            broken = copy.deepcopy(original)
            broken["plan_id"] = "other"
            self.assert_integration_error("invalid_receipt_id", lambda: validate_receipt(broken))

            mutations = (
                ("unknown", lambda r: r.__setitem__("extra", True)),
                ("plan-id", lambda r: r.__setitem__("plan_id", "")),
                ("status", lambda r: r.__setitem__("status", "partial")),
                ("completed-error", lambda r: r.__setitem__("error", {})),
                ("bad-result", lambda r: r["results"].__setitem__(0, [])),
                ("bad-environment", lambda r: r["results"][0].__setitem__("environment", "")),
                ("bad-archive", lambda r: r["results"][0].__setitem__("archive_path", "relative")),
            )
            for label, mutate in mutations:
                with self.subTest(label=label):
                    broken = copy.deepcopy(original)
                    mutate(broken)
                    self.rehash_receipt(broken)
                    self.assert_integration_error(
                        "invalid_receipt", lambda: validate_receipt(broken)
                    )

    def test_failed_receipt_verification_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            plan, completed = self.completed_receipt(Path(base))
            failed = _build_receipt(
                plan,
                "failed",
                copy.deepcopy(completed["results"]),
                {"code": "probe", "message": "probe", "details": []},
            )
            validation = verify_receipt(failed)
            self.assertEqual(validation["status"], "failed")
            self.assertEqual(validation["checks"][0]["code"], "receipt_not_completed")


class TestApplyFailureSemantics(IntegrationValidationFixture):
    def test_replace_link_success_and_unknown_action_guard(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            project = Path(base)
            source = project / "source"
            canonical = self.write_skill(source)
            stale = self.write_skill(project / "stale")
            target = project / "target"
            target.mkdir()
            (target / "alpha").symlink_to(stale, target_is_directory=True)
            plan = build_integration_plan(
                IntegrationSpec(
                    project_root=project,
                    sources=(source,),
                    targets=(IntegrationTarget("test", root=target),),
                )
            )
            self.assertEqual(plan["targets"][0]["actions"][0]["action"], "replace_link")
            apply_integration_plan(plan, project / "receipt.json")
            self.assertEqual((target / "alpha").resolve(), canonical.resolve())

            action = copy.deepcopy(plan["targets"][0]["actions"][0])
            action["action"] = "unknown"
            self.assert_integration_error(
                "invalid_plan_action", lambda: _apply_exact_action(target, action)
            )

    def test_failure_receipt_paths_cover_contract_and_io_errors(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            project = Path(base)
            plan = self.valid_plan(project)
            receipt_path = project / "failed.json"
            with patch(
                "skills_auditor.integration.check_plan_preconditions", return_value=[]
            ), patch(
                "skills_auditor.integration._apply_exact_action",
                side_effect=IntegrationError("injected", "injected", exit_code=3),
            ):
                error = self.assert_integration_error(
                    "injected", lambda: apply_integration_plan(plan, receipt_path)
                )
            self.assertTrue(receipt_path.is_file())
            self.assertTrue(any("receipt_path" in detail for detail in error.details))

            plan = self.valid_plan(project / "second")
            with patch(
                "skills_auditor.integration.check_plan_preconditions", return_value=[]
            ), patch(
                "skills_auditor.integration._apply_exact_action", side_effect=OSError("apply")
            ), patch(
                "skills_auditor.integration._atomic_write_json", side_effect=OSError("receipt")
            ):
                self.assert_integration_error(
                    "apply_failed_without_receipt", lambda: apply_integration_plan(plan)
                )

    def test_completed_apply_reports_receipt_write_failure(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            project = Path(base)
            plan = self.valid_plan(project)
            real_apply = _apply_exact_action

            writes = 0

            def fail_only_write(path, value):
                nonlocal writes
                writes += 1
                raise OSError("receipt")

            with patch("skills_auditor.integration._atomic_write_json", side_effect=fail_only_write):
                self.assert_integration_error(
                    "receipt_write_failed", lambda: apply_integration_plan(plan)
                )
            self.assertEqual(writes, 1)
            self.assertTrue((project / "target" / "alpha").is_symlink())


if __name__ == "__main__":
    unittest.main()
