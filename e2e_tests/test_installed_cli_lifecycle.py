"""End-to-end tests for the CLI installed from a built wheel."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, Optional


def required_path(name: str) -> Path:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} must be provided by the artifact test runner")
    return Path(value).resolve()


CLI = required_path("SKILLS_AUDITOR_CLI")


class InstalledCliFixture(unittest.TestCase):
    def write_skill(self, root: Path, name: str, body: str = "body") -> Path:
        skill = root / name
        skill.mkdir(parents=True, exist_ok=True)
        (skill / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: installed CLI fixture\n---\n\n{body}\n",
            encoding="utf-8",
        )
        return skill

    def write_config(self, project: Path, targets: list[str]) -> None:
        (project / "skills-auditor.json").write_text(
            json.dumps(
                {
                    "schema_version": "skills-auditor-integration/v1",
                    "sources": [".agents/skills"],
                    "targets": targets,
                    "metadata_platform": "codex",
                }
            ),
            encoding="utf-8",
        )

    def link_snapshot(self, entry: Path) -> tuple:
        stat = entry.lstat()
        return (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, os.readlink(entry))

    def source_snapshot(self, canonical: Path) -> dict:
        return {
            str(path.relative_to(canonical)): (
                path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns
            )
            for path in canonical.rglob("*")
            if path.is_file()
        }

    def run_cli(
        self,
        project: Path,
        *arguments: str,
        home: Optional[Path] = None,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        if home is not None:
            environment["HOME"] = str(home)
        return subprocess.run(
            [str(CLI), *arguments],
            cwd=project,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def json_cli(
        self,
        project: Path,
        *arguments: str,
        expected_exit: int = 0,
        home: Optional[Path] = None,
    ) -> Dict[str, Any]:
        result = self.run_cli(project, *arguments, home=home)
        self.assertEqual(result.returncode, expected_exit, result.stderr or result.stdout)
        self.assertEqual(result.stderr, "")
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            self.fail(f"CLI did not emit one JSON document: {exc}: {result.stdout!r}")
        self.assertIsInstance(payload, dict)
        return payload


class TestInstalledCliLifecycle(InstalledCliFixture):
    def test_source_drift_renews_approval_only_after_explicit_noop_apply(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            project = Path(base)
            canonical = self.write_skill(project / ".agents" / "skills", "alpha")
            payload = canonical / "payload.txt"
            payload.write_text("H1\n", encoding="utf-8")
            self.write_config(project, ["codex"])
            original_plan_path = project / "original-plan.json"
            self.json_cli(
                project, "integrate", "--plan-out", str(original_plan_path), "--format", "json"
            )
            original = self.json_cli(project, "apply", str(original_plan_path), "--format", "json")
            original_path = Path(original["receipt_path"])
            original_bytes = original_path.read_bytes()
            verification = self.json_cli(project, "verify", str(original_path), "--format", "json")
            self.assertEqual(verification["approval"]["state"], "valid")

            payload.write_text("H2\n", encoding="utf-8")
            invalidated = self.json_cli(
                project, "verify", str(original_path), "--format", "json", expected_exit=3
            )
            self.assertEqual(
                invalidated["approval"],
                {
                    "state": "invalidated",
                    "requires_reapproval": True,
                    "reason_codes": ["source_tree"],
                },
            )
            human = self.run_cli(project, "verify", str(original_path))
            self.assertEqual(human.returncode, 3, human.stderr or human.stdout)
            self.assertEqual(human.stderr, "")
            for guidance in (
                "approval: invalidated | re-approval required: yes",
                "skills-audit integrate",
                "Review the emitted plan",
                "Explicitly re-approve",
                "skills-audit apply",
            ):
                self.assertIn(guidance, human.stdout)

            entry = project / ".codex" / "skills" / "alpha"
            link_before = self.link_snapshot(entry)
            source_before = self.source_snapshot(canonical)
            receipts_before = {
                path.name: path.read_bytes() for path in original_path.parent.glob("*.json")
            }
            renewal_plan_path = project / "renewal-plan.json"
            renewal_plan = self.json_cli(
                project, "integrate", "--plan-out", str(renewal_plan_path), "--format", "json"
            )
            self.assertEqual(renewal_plan["summary"]["changes"], 0)
            self.assertEqual(renewal_plan["summary"]["actions"], 1)
            self.assertEqual(renewal_plan["targets"][0]["actions"][0]["action"], "noop")
            self.assertEqual(
                {path.name: path.read_bytes() for path in original_path.parent.glob("*.json")},
                receipts_before,
            )
            self.assertEqual(self.link_snapshot(entry), link_before)
            self.assertEqual(self.source_snapshot(canonical), source_before)
            still_invalidated = self.json_cli(
                project, "verify", str(original_path), "--format", "json", expected_exit=3
            )
            self.assertEqual(still_invalidated["approval"], invalidated["approval"])

            renewed = self.json_cli(project, "apply", str(renewal_plan_path), "--format", "json")
            renewed_path = Path(renewed["receipt_path"])
            self.assertNotEqual(renewed_path, original_path)
            self.assertNotEqual(renewed["receipt_id"], original["receipt_id"])
            self.assertEqual(renewed["plan_id"], renewal_plan["plan_id"])
            self.assertEqual(renewed["results"][0]["action"], "noop")
            self.assertEqual(
                renewed["results"][0]["expected_tree_sha256"],
                renewal_plan["source_skills"][0]["tree_sha256"],
            )
            self.assertNotEqual(
                renewed["results"][0]["expected_tree_sha256"],
                original["results"][0]["expected_tree_sha256"],
            )
            verified = self.json_cli(project, "verify", str(renewed_path), "--format", "json")
            self.assertEqual(
                verified["approval"],
                {"state": "valid", "requires_reapproval": False, "reason_codes": []},
            )
            self.assertEqual(self.link_snapshot(entry), link_before)
            self.assertEqual(self.source_snapshot(canonical), source_before)
            self.assertEqual(original_path.read_bytes(), original_bytes)
            old_verified = self.json_cli(
                project, "verify", str(original_path), "--format", "json", expected_exit=3
            )
            self.assertEqual(old_verified["approval"], invalidated["approval"])

    def test_stale_noop_plan_rejects_source_change_without_replacing_link_or_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            project = Path(base)
            canonical = self.write_skill(project / ".agents" / "skills", "alpha")
            payload = canonical / "payload.txt"
            payload.write_text("H1\n", encoding="utf-8")
            self.write_config(project, ["codex"])
            original_plan_path = project / "original-plan.json"
            self.json_cli(
                project, "integrate", "--plan-out", str(original_plan_path), "--format", "json"
            )
            original = self.json_cli(project, "apply", str(original_plan_path), "--format", "json")
            original_path = Path(original["receipt_path"])
            original_bytes = original_path.read_bytes()
            receipts_before = {
                path.name: path.read_bytes() for path in original_path.parent.glob("*.json")
            }
            payload.write_text("H2\n", encoding="utf-8")
            renewal_plan_path = project / "renewal-plan.json"
            renewal_plan = self.json_cli(
                project, "integrate", "--plan-out", str(renewal_plan_path), "--format", "json"
            )
            self.assertEqual(renewal_plan["targets"][0]["actions"][0]["action"], "noop")
            payload.write_text("H3\n", encoding="utf-8")
            entry = project / ".codex" / "skills" / "alpha"
            link_before = self.link_snapshot(entry)
            source_before = self.source_snapshot(canonical)
            rejected_receipt_path = project / "rejected-receipt.json"

            error = self.json_cli(
                project,
                "apply",
                str(renewal_plan_path),
                "--receipt-out",
                str(rejected_receipt_path),
                "--format",
                "json",
                expected_exit=3,
            )
            self.assertEqual(error["schema_version"], "skills-auditor-error/v1")
            self.assertEqual(error["error"]["code"], "stale_plan")
            self.assertIn("source_changed", {detail["code"] for detail in error["error"]["details"]})
            self.assertFalse(rejected_receipt_path.exists())
            self.assertEqual(
                {path.name: path.read_bytes() for path in original_path.parent.glob("*.json")},
                receipts_before,
            )
            self.assertEqual(self.link_snapshot(entry), link_before)
            self.assertEqual(self.source_snapshot(canonical), source_before)
            self.assertEqual(original_path.read_bytes(), original_bytes)
            old_verified = self.json_cli(
                project, "verify", str(original_path), "--format", "json", expected_exit=3
            )
            self.assertEqual(
                old_verified["approval"],
                {
                    "state": "invalidated",
                    "requires_reapproval": True,
                    "reason_codes": ["source_tree"],
                },
            )

    def test_plan_apply_verify_and_noop_across_all_builtin_host_roots(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            project = Path(base) / "project"
            project.mkdir()
            home = Path(base) / "home"
            home.mkdir()
            source = project / ".agents" / "skills"
            canonical = self.write_skill(source, "alpha", "canonical-v1")
            targets = [
                "cursor",
                "claude-code",
                "codex",
                "cursor@global",
                "claude-code@global",
                "codex@global",
            ]
            self.write_config(project, targets)
            plan_path = project / "plan.json"
            receipt_path = project / "receipt.json"

            plan = self.json_cli(
                project,
                "integrate",
                "--plan-out",
                str(plan_path),
                "--format",
                "json",
                home=home,
            )
            self.assertEqual(plan["schema_version"], "skills-auditor-plan/v1")
            self.assertEqual(plan["summary"], {"skills": 1, "targets": 6, "actions": 6, "changes": 6})

            receipt = self.json_cli(
                project,
                "apply",
                str(plan_path),
                "--receipt-out",
                str(receipt_path),
                "--format",
                "json",
                home=home,
            )
            self.assertEqual(receipt["schema_version"], "skills-auditor-receipt/v1")
            self.assertEqual(receipt["status"], "completed")

            expected_roots = (
                project / ".cursor" / "skills",
                project / ".claude" / "skills",
                project / ".codex" / "skills",
                home / ".cursor" / "skills",
                home / ".claude" / "skills",
                home / ".codex" / "skills",
            )
            for root in expected_roots:
                with self.subTest(root=root):
                    entry = root / "alpha"
                    self.assertTrue(entry.is_symlink())
                    self.assertEqual(entry.resolve(), canonical.resolve())

            verification = self.json_cli(
                project,
                "verify",
                str(receipt_path),
                "--format",
                "json",
                home=home,
            )
            self.assertEqual(verification["schema_version"], "skills-auditor-verification/v1")
            self.assertEqual(verification["status"], "passed")
            self.assertTrue(all(check["ok"] for check in verification["checks"]))
            self.assertEqual(
                verification["approval"],
                {
                    "state": "valid",
                    "requires_reapproval": False,
                    "reason_codes": [],
                },
            )

            second_plan = self.json_cli(
                project,
                "integrate",
                "--plan-out",
                str(project / "second-plan.json"),
                "--format",
                "json",
                home=home,
            )
            self.assertEqual(second_plan["summary"]["changes"], 0)
            self.assertEqual(
                {action["action"] for target in second_plan["targets"] for action in target["actions"]},
                {"noop"},
            )

    def test_stale_reviewed_plan_fails_before_any_target_write(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            project = Path(base)
            source = project / ".agents" / "skills"
            canonical = self.write_skill(source, "alpha", "before")
            self.write_config(project, ["codex"])
            plan_path = project / "plan.json"
            self.json_cli(
                project,
                "integrate",
                "--plan-out",
                str(plan_path),
                "--format",
                "json",
            )
            (canonical / "SKILL.md").write_text(
                "---\nname: alpha\ndescription: installed CLI fixture\n---\n\nafter\n",
                encoding="utf-8",
            )

            error = self.json_cli(
                project,
                "apply",
                str(plan_path),
                "--format",
                "json",
                expected_exit=3,
            )
            self.assertEqual(error["schema_version"], "skills-auditor-error/v1")
            self.assertEqual(error["error"]["code"], "stale_plan")
            self.assertFalse((project / ".codex" / "skills" / "alpha").exists())

    def test_native_entry_is_archived_and_source_drift_breaks_verification(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            project = Path(base)
            source = project / ".agents" / "skills"
            canonical = self.write_skill(source, "alpha", "canonical")
            native = self.write_skill(project / ".codex" / "skills", "alpha", "native")
            self.write_config(project, ["codex"])
            plan_path = project / "plan.json"
            receipt_path = project / "receipt.json"

            plan = self.json_cli(
                project,
                "integrate",
                "--plan-out",
                str(plan_path),
                "--format",
                "json",
            )
            action = plan["targets"][0]["actions"][0]
            self.assertEqual(action["action"], "archive_and_link")
            archive_path = Path(action["archive_path"])

            self.json_cli(
                project,
                "apply",
                str(plan_path),
                "--receipt-out",
                str(receipt_path),
                "--format",
                "json",
            )
            self.assertTrue(native.is_symlink())
            self.assertTrue(archive_path.is_dir())
            self.assertIn("native", (archive_path / "SKILL.md").read_text(encoding="utf-8"))
            self.assertEqual(native.resolve(), canonical.resolve())

            (canonical / "payload.txt").write_text("drift\n", encoding="utf-8")
            verification = self.json_cli(
                project,
                "verify",
                str(receipt_path),
                "--format",
                "json",
                expected_exit=3,
            )
            self.assertEqual(verification["status"], "failed")
            self.assertEqual(
                verification["approval"],
                {
                    "state": "invalidated",
                    "requires_reapproval": True,
                    "reason_codes": ["source_tree"],
                },
            )
            self.assertIn(
                "source_tree",
                {check["code"] for check in verification["checks"] if not check["ok"]},
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
