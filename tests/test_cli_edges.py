"""Low-level CLI helper and reporting edge coverage."""

from __future__ import annotations

import json
import runpy
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from skills_auditor.cli import (
    DedupAction,
    DriftStatus,
    DuplicateSkillNameFinding,
    EntryStatus,
    MetadataRepairAction,
    SourceSpec,
    _clean_frontmatter_value,
    _frontmatter_end_index,
    _frontmatter_scalar_value,
    _git,
    default_discovery_sources,
    default_sync_discover_sources,
    infer_default_platforms_for_source,
    is_path_excluded,
    load_discovery_profile,
    load_mapping,
    parse_profile_source_entries,
    print_audit,
    print_dedup_plan,
    print_duplicate_name_check,
    print_metadata_repair,
    resolve_skills_dirs,
    scan_skills,
)


class TestCliHelperEdges(unittest.TestCase):
    def test_module_entrypoint_uses_stable_program_name(self) -> None:
        output = StringIO()
        with patch("sys.argv", ["ignored", "--version"]), redirect_stdout(output):
            with self.assertRaises(SystemExit) as caught:
                runpy.run_module("skills_auditor", run_name="__main__")
        self.assertEqual(caught.exception.code, 0)
        self.assertRegex(output.getvalue(), r"^skills-audit \d")

    def test_git_helper_fails_closed_for_missing_and_timed_out_git(self) -> None:
        with patch("skills_auditor.cli.subprocess.run", side_effect=FileNotFoundError):
            self.assertIsNone(_git(["status"], Path.cwd()))
        with patch(
            "skills_auditor.cli.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["git"], 15),
        ):
            self.assertIsNone(_git(["status"], Path.cwd()))

    def test_scan_skills_classifies_files_directories_and_links(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            directory = root / "directory"
            directory.mkdir()
            (directory / "SKILL.md").write_text("body", encoding="utf-8")
            (root / "file").write_text("body", encoding="utf-8")
            (root / "good").symlink_to(directory, target_is_directory=True)
            (root / "broken").symlink_to(root / "missing", target_is_directory=True)
            statuses = {status.name: status for status in scan_skills(root)}
            self.assertEqual(statuses["directory"].entry_type, "directory")
            self.assertEqual(statuses["file"].entry_type, "file")
            self.assertEqual(statuses["good"].link_status, "ok")
            self.assertEqual(statuses["broken"].link_status, "broken")
            self.assertEqual(scan_skills(root / "missing"), [])

    def test_mapping_and_profile_validation_errors(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            mapping = root / "mapping.json"
            for payload in ([], {"a": 7}):
                mapping.write_text(json.dumps(payload), encoding="utf-8")
                with self.subTest(payload=payload), self.assertRaises(ValueError):
                    load_mapping(mapping)
            mapping.write_text(json.dumps({"a": "path"}), encoding="utf-8")
            self.assertEqual(load_mapping(mapping), {"a": "path"})

            invalid_sources = (
                "not-list",
                [{"path": 7, "platform": ["codex"]}],
                [{"path": "x", "platform": []}],
                [{"path": "x", "platform": [7]}],
                [{"path": "x", "platform": ["codex"], "exclude": "bad"}],
                [7],
            )
            for raw in invalid_sources:
                with self.subTest(raw=raw), self.assertRaises(ValueError):
                    parse_profile_source_entries(raw)

            profile = root / "profile.json"
            for payload in (
                [],
                {"sources": [], "exclude_sources": "bad"},
                {"sources": [], "collapse_identical": "yes"},
            ):
                profile.write_text(json.dumps(payload), encoding="utf-8")
                with self.subTest(profile=payload), self.assertRaises(ValueError):
                    load_discovery_profile(profile)

    def test_path_defaults_and_platform_inference(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            project = Path(base)
            with patch("skills_auditor.cli.Path.cwd", return_value=project):
                defaults = default_discovery_sources()
                project_sources = default_sync_discover_sources(False)
                global_sources = default_sync_discover_sources(True)
            self.assertEqual(len(defaults), len(set(defaults)))
            self.assertIn(project / ".cursor" / "skills", defaults)
            self.assertGreater(len(global_sources), len(project_sources))
            self.assertEqual(
                resolve_skills_dirs([str(project), str(project)]),
                [project],
            )
            self.assertEqual(infer_default_platforms_for_source(Path("/x/.claude/skills")), ["claude-code"])
            self.assertEqual(infer_default_platforms_for_source(Path("/x/skills-cursor")), ["cursor"])
            self.assertEqual(
                infer_default_platforms_for_source(Path("/x/cursor/plugins/skills")),
                ["cursor"],
            )
            self.assertEqual(
                infer_default_platforms_for_source(Path("/x/shared/skills")),
                ["cursor", "claude-code"],
            )
            self.assertTrue(is_path_excluded(project / "child", [project]))
            self.assertFalse(is_path_excluded(project, [project / "child"]))

    def test_frontmatter_scalar_edges(self) -> None:
        self.assertIsNone(_frontmatter_end_index([]))
        self.assertIsNone(_frontmatter_end_index(["body"]))
        self.assertEqual(_frontmatter_end_index(["---", "name: x", "---"]), 2)
        self.assertEqual(_clean_frontmatter_value(" ' value ' "), "value")
        self.assertEqual(_clean_frontmatter_value("plain"), "plain")
        with tempfile.TemporaryDirectory() as base:
            path = Path(base) / "SKILL.md"
            path.write_text("body\n", encoding="utf-8")
            self.assertIsNone(_frontmatter_scalar_value(path.read_text(), "name"))
            self.assertIsNone(_frontmatter_scalar_value("---\nname: >\n  alpha\n---\n", "name"))
            self.assertEqual(_frontmatter_scalar_value("---\nname: 'alpha'\n---\n", "name"), "alpha")


class TestCliReportingEdges(unittest.TestCase):
    def capture(self, operation) -> str:
        output = StringIO()
        with redirect_stdout(output):
            operation()
        return output.getvalue()

    def test_audit_report_covers_every_drift_label(self) -> None:
        statuses = [
            EntryStatus(name, "symlink", "target", "ok", True, f"/tmp/{name}")
            for name in ("synced", "error", "clean", "drift", "unknown")
        ]

        def drift(name: str, **changes) -> DriftStatus:
            values = dict(
                name=name,
                local_path=f"/tmp/{name}",
                remote_url=None,
                branch="main",
                ahead=0,
                behind=0,
                dirty_count=0,
                skill_dirty_count=0,
                synced=False,
                display_target=f"/tmp/{name}",
                error=None,
            )
            values.update(changes)
            return DriftStatus(**values)

        drifts = {
            "synced": drift(
                "synced",
                synced=True,
                remote_url="https://example.test/repo",
                display_target="https://example.test/repo",
            ),
            "error": drift("error", error="not a repository"),
            "clean": drift("clean", dirty_count=2),
            "drift": drift("drift", ahead=1, behind=2, dirty_count=3, skill_dirty_count=1),
        }
        output = self.capture(lambda: print_audit(statuses, drifts))
        for label in ("synced", "not a repository", "skill_clean", "ahead=1", "behind=2", "skill_dirty=1"):
            self.assertIn(label, output)

    def test_duplicate_dedup_and_repair_reports_cover_empty_and_action_rows(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            empty = self.capture(lambda: print_duplicate_name_check(root, []))
            self.assertIn("status: ok", empty)
            finding = DuplicateSkillNameFinding("bundle", "alpha", ["/a", "/b"])
            present = self.capture(lambda: print_duplicate_name_check(root, [finding]))
            self.assertIn("findings present", present)

            actions = [
                DedupAction("b", "alpha", "/a", "/b", "relink", "same"),
                DedupAction("b", "alpha", "/a", "/c", "skip_not_file", "not file"),
                DedupAction("b", "alpha", "/a", "/d", "skip_multi_version", "variant"),
            ]
            dedup = self.capture(lambda: print_dedup_plan(actions, [finding], False))
            self.assertIn("planned: 1 relink(s), 1 multi-version skip(s)", dedup)

            repair_empty = self.capture(lambda: print_metadata_repair(root, [], "codex", False))
            self.assertIn("no metadata repairs", repair_empty)
            repair = MetadataRepairAction("/skill", "repair", "reason", ["missing_name"])
            skip = MetadataRepairAction("/bad", "skip", "manual", ["unclosed_frontmatter"])
            repair_output = self.capture(
                lambda: print_metadata_repair(root, [repair, skip], "codex", True)
            )
            self.assertIn("1 repair(s), 1 skip(s)", repair_output)


if __name__ == "__main__":
    unittest.main()
