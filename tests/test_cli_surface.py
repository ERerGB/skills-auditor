"""Command-level contracts for every public CLI family."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Optional
from unittest.mock import patch

from skills_auditor.cli import main


class CliSurfaceFixture(unittest.TestCase):
    def write_skill(self, root: Path, folder: str, name: str = "alpha", body: str = "body") -> Path:
        skill = root / folder
        skill.mkdir(parents=True, exist_ok=True)
        (skill / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: CLI surface fixture\n---\n\n{body}\n",
            encoding="utf-8",
        )
        return skill

    @contextmanager
    def working_directory(self, path: Path):
        previous = Path.cwd()
        os.chdir(path)
        try:
            yield
        finally:
            os.chdir(previous)

    def run_cli(
        self,
        *arguments: str,
        cwd: Optional[Path] = None,
        stdin: str = "",
    ) -> tuple[int, str, str]:
        stdout = StringIO()
        stderr = StringIO()
        directory = cwd or Path.cwd()
        with self.working_directory(directory), patch(
            "sys.argv", ["skills-audit", *arguments]
        ), patch("sys.stdin", StringIO(stdin)), redirect_stdout(stdout), redirect_stderr(stderr):
            code = main()
        return code, stdout.getvalue(), stderr.getvalue()


class TestCliTransactionalSurface(CliSurfaceFixture):
    def test_human_plan_apply_and_failed_verify(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            project = Path(base)
            source = project / "source"
            target = project / "target"
            canonical = self.write_skill(source, "alpha", body="before")
            plan = project / "plan.json"
            receipt = project / "receipt.json"

            code, output, error = self.run_cli(
                "integrate",
                "--source",
                str(source),
                "--target-root",
                f"test={target}",
                "--plan-out",
                str(plan),
                cwd=project,
            )
            self.assertEqual(code, 0, error)
            self.assertIn("Review the plan", output)
            self.assertIn("test@project", output)

            code, output, error = self.run_cli(
                "apply",
                str(plan),
                "--receipt-out",
                str(receipt),
                cwd=project,
            )
            self.assertEqual(code, 0, error)
            self.assertIn("Verify the installed state", output)

            (canonical / "payload.txt").write_text("drift\n", encoding="utf-8")
            code, output, error = self.run_cli("verify", str(receipt), cwd=project)
            self.assertEqual(code, 3, error)
            self.assertIn("status: failed", output)
            self.assertIn("FAIL source_tree", output)

    def test_human_integration_errors_include_structured_details(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            project = Path(base)
            source = project / "source"
            skill = source / "bad"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("missing frontmatter\n", encoding="utf-8")
            code, output, error = self.run_cli(
                "integrate",
                "--source",
                str(source),
                "--target-root",
                f"test={project / 'target'}",
                cwd=project,
            )
            self.assertEqual(code, 3)
            self.assertEqual(output, "")
            self.assertIn("error [invalid_metadata]", error)
            self.assertIn("missing_frontmatter", error)

            code, output, error = self.run_cli("apply", str(project / "missing.json"), cwd=project)
            self.assertEqual(code, 2)
            self.assertEqual(output, "")
            self.assertIn("error [invalid_plan]", error)

            code, output, error = self.run_cli("verify", str(project / "missing.json"), cwd=project)
            self.assertEqual(code, 2)
            self.assertEqual(output, "")
            self.assertIn("error [invalid_receipt]", error)


class TestCliMaintenanceSurface(CliSurfaceFixture):
    def test_audit_metadata_repair_and_drift_commands(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            project = Path(base)
            valid_root = project / "valid"
            invalid_root = project / "invalid"
            canonical = self.write_skill(project / "source", "alpha")
            valid_root.mkdir()
            (valid_root / "alpha").symlink_to(canonical, target_is_directory=True)
            invalid = invalid_root / "broken"
            invalid.mkdir(parents=True)
            (invalid / "SKILL.md").write_text("---\nname: broken\n", encoding="utf-8")

            code, output, _ = self.run_cli(
                "audit",
                "--skills-dir",
                str(valid_root),
                "--skills-dir",
                str(invalid_root),
                "--allow-invalid-metadata",
                "--with-drift",
            )
            self.assertEqual(code, 0)
            self.assertIn("skills-dir:", output)
            self.assertIn("not a git repository", output)

            code, _, _ = self.run_cli("audit", "--skills-dir", str(invalid_root))
            self.assertEqual(code, 5)
            code, _, _ = self.run_cli(
                "metadata", "--skills-dir", str(invalid_root), "--fail-on-invalid"
            )
            self.assertEqual(code, 5)
            code, output, _ = self.run_cli(
                "metadata-repair", "--skills-dir", str(invalid_root)
            )
            self.assertEqual(code, 5)
            self.assertIn("skip", output)

            code, output, _ = self.run_cli("drift-check", "--skills-dir", str(valid_root))
            self.assertEqual(code, 0)
            self.assertIn("summary:", output)

    def test_sync_and_sync_discover_apply_real_actions(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            project = Path(base)
            source = project / "source"
            canonical = self.write_skill(source, "alpha")
            first = project / "first"
            second = project / "second"
            mapping = project / "mapping.json"
            mapping.write_text(json.dumps({"alpha": str(canonical)}), encoding="utf-8")

            code, _, error = self.run_cli(
                "sync",
                "--map-file",
                str(mapping),
                "--target-platform",
                "codex",
            )
            self.assertEqual(code, 2)
            self.assertIn("requires --discovery-profile", error)

            code, output, error = self.run_cli(
                "sync",
                "--map-file",
                str(mapping),
                "--skills-dir",
                str(first),
                "--skills-dir",
                str(second),
                "--apply",
            )
            self.assertEqual(code, 0, error)
            self.assertIn("Applied actions", output)
            self.assertEqual((first / "alpha").resolve(), canonical.resolve())
            self.assertEqual((second / "alpha").resolve(), canonical.resolve())

            discovered_target = project / "discovered"
            code, output, _ = self.run_cli(
                "sync-discover",
                "--source",
                str(source),
                "--skills-dir",
                str(discovered_target),
            )
            self.assertEqual(code, 1)
            self.assertIn("discovered: 1 sync entry", output)
            code, output, _ = self.run_cli(
                "sync-discover",
                "--source",
                str(source),
                "--skills-dir",
                str(discovered_target),
                "--apply",
            )
            self.assertEqual(code, 0)
            self.assertIn("Applied actions", output)
            self.assertEqual((discovered_target / "alpha").resolve(), canonical.resolve())

    def test_dedup_route_and_state_machine_commands(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            project = Path(base)
            dedup_root = project / "dedup"
            first = self.write_skill(dedup_root, "a", body="same")
            second = self.write_skill(dedup_root, "longer-name", body="same")
            code, output, _ = self.run_cli("dedup", "--skills-dir", str(dedup_root))
            self.assertEqual(code, 0)
            self.assertIn("DRY-RUN", output)
            code, output, _ = self.run_cli(
                "dedup", "--skills-dir", str(dedup_root), "--apply"
            )
            self.assertEqual(code, 0)
            self.assertIn("Applied: 1 symlink", output)
            self.assertTrue((second / "SKILL.md").is_symlink())
            self.assertEqual((second / "SKILL.md").resolve(), (first / "SKILL.md").resolve())

            route_root = project / "route"
            self.write_skill(route_root, "pack", body="primary")
            self.write_skill(
                route_root / "pack" / ".agents" / "skills",
                "codex-variant",
                body="codex",
            )
            nested = route_root / "pack" / ".agents" / "skills" / "codex-variant" / "SKILL.md"
            nested.write_text(
                "---\nname: alpha\ndescription: CLI surface fixture\n---\n\ncodex\n",
                encoding="utf-8",
            )
            trace_dir = project / "traces"
            code, output, _ = self.run_cli(
                "route",
                "--skills-dir",
                str(route_root),
                "--platform",
                "codex",
                "--trace-dir",
                str(trace_dir),
            )
            self.assertEqual(code, 0)
            self.assertIn("route mode: DRY-RUN", output)
            self.assertIn("archive: 1", output)
            code, output, _ = self.run_cli(
                "audit-state-machine", "--trace-dir", str(trace_dir)
            )
            self.assertIn(code, {0, 1})
            self.assertIn("traces analyzed:", output)

            empty_traces = project / "empty-traces"
            code, output, _ = self.run_cli(
                "audit-state-machine", "--trace-dir", str(empty_traces)
            )
            self.assertEqual(code, 0)
            self.assertIn("No traces found", output)


class TestCliEvidenceSurface(CliSurfaceFixture):
    def test_observability_command_family(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            project = Path(base)
            log_root = project / "evidence"
            trace_root = project / "traces"
            skill = self.write_skill(project / "skills", "alpha") / "SKILL.md"

            code, output, _ = self.run_cli(
                "record-trigger-log",
                "--log-dir",
                str(log_root),
                "--expected-skill",
                "alpha",
                "--actual-skill",
                "alpha",
                "--verdict",
                "correct",
            )
            self.assertEqual(code, 0)
            self.assertIn("log written:", output)
            code, output, _ = self.run_cli(
                "audit-trigger-logs", "--log-dir", str(log_root), "--fail-on-error"
            )
            self.assertEqual(code, 0)
            self.assertIn("labeled accuracy: 1.000", output)

            payload = project / "sensor.json"
            payload.write_text(
                json.dumps({"tool_name": "Read", "tool_input": {"file_path": str(skill)}}),
                encoding="utf-8",
            )
            code, output, _ = self.run_cli(
                "record-sensor-event",
                "--provider",
                "codex",
                "--log-dir",
                str(log_root),
                "--input-file",
                str(payload),
                "--resolve-path",
                "--hash-path",
            )
            self.assertEqual(code, 0)
            self.assertIn("sensor log written:", output)
            code, _, error = self.run_cli(
                "record-sensor-event",
                "--provider",
                "codex",
                "--log-dir",
                str(log_root),
                stdin="[]",
            )
            self.assertEqual(code, 2)
            self.assertIn("must be a JSON object", error)

            code, output, _ = self.run_cli(
                "audit-sensor-logs", "--log-dir", str(log_root), "--provider", "codex"
            )
            self.assertEqual(code, 0)
            self.assertIn("skill file accesses: 1", output)
            code, output, _ = self.run_cli(
                "aggregate-sensor-claims", "--log-dir", str(log_root)
            )
            self.assertEqual(code, 0)
            self.assertIn("claims: 1", output)
            code, output, _ = self.run_cli(
                "log-stats",
                "--log-dir",
                str(log_root),
                "--trace-dir",
                str(trace_root),
                "--events-per-day",
                "10",
            )
            self.assertEqual(code, 0)
            self.assertIn("estimate:", output)

    def test_ledger_error_and_multi_summary_paths(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            ledger_root = Path(base)
            code, _, error = self.run_cli(
                "ledger-upsert",
                "--ledger-dir",
                str(ledger_root),
                "--run-id",
                "missing",
                "--id",
                "resource",
                "--class",
                "artifact",
                "--locator",
                "path",
                "--owner",
                "owner",
                "--status",
                "completed",
            )
            self.assertEqual(code, 2)
            self.assertIn("error:", error)
            code, _, error = self.run_cli(
                "ledger-check",
                "--ledger-dir",
                str(ledger_root),
                "--run-id",
                "missing",
            )
            self.assertEqual(code, 2)
            self.assertIn("error:", error)
            code, _, error = self.run_cli(
                "ledger-summary",
                "--ledger-dir",
                str(ledger_root),
                "--run-id",
                "missing",
            )
            self.assertEqual(code, 2)
            self.assertIn("error:", error)

            for run_id in ("one", "two"):
                code, _, _ = self.run_cli(
                    "ledger-create",
                    "--ledger-dir",
                    str(ledger_root),
                    "--run-id",
                    run_id,
                )
                self.assertEqual(code, 0)
            code, output, _ = self.run_cli(
                "ledger-summary", "--ledger-dir", str(ledger_root)
            )
            self.assertEqual(code, 0)
            self.assertIn("ledgers: 2", output)

    def test_discovery_profile_and_conflict_exit_codes(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            project = Path(base)
            first = project / "first"
            second = project / "second"
            self.write_skill(first, "alpha", body="one")
            self.write_skill(second, "alpha", body="two")

            code, output, _ = self.run_cli(
                "audit-discovery",
                "--source",
                str(first),
                "--source",
                str(second),
                "--fail-on-conflict",
            )
            self.assertEqual(code, 2)
            self.assertIn("FAIL: duplicate skills", output)
            code, output, _ = self.run_cli(
                "audit-discovery",
                "--source",
                str(first),
                "--source",
                str(second),
                "--fail-on-hash-conflict",
                "--summary-only",
            )
            self.assertEqual(code, 3)
            self.assertIn("FAIL: hash conflicts", output)

            profile = project / "profile.json"
            profile.write_text(
                json.dumps(
                    {
                        "sources": [
                            {"path": str(first), "platform": ["codex"], "exclude": ["none/**"]}
                        ],
                        "exclude_sources": [str(second)],
                        "collapse_identical": False,
                    }
                ),
                encoding="utf-8",
            )
            code, output, _ = self.run_cli(
                "audit-discovery",
                "--profile-file",
                str(profile),
                "--no-collapse-identical",
            )
            self.assertEqual(code, 0)
            self.assertIn("canonical injection preview:", output)


if __name__ == "__main__":
    unittest.main()
