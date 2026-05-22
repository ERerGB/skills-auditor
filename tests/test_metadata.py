"""Tests for SKILL.md frontmatter metadata validation."""

import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from skills_auditor.cli import collect_metadata_findings, main, validate_skill_metadata


class TestMetadataValidation(unittest.TestCase):
    def write_skill(self, text: str) -> Path:
        root = Path(self.tmp.name)
        skill = root / "skill" / "SKILL.md"
        skill.parent.mkdir(parents=True, exist_ok=True)
        skill.write_text(text, encoding="utf-8")
        return skill

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_codex_metadata_accepts_name_and_description(self) -> None:
        skill_md = self.write_skill(
            "---\n"
            "name: sample-skill\n"
            "description: >\n"
            "  Validate Codex-ready metadata.\n"
            "---\n"
            "# Sample\n"
        )
        self.assertEqual(validate_skill_metadata(skill_md, platform="codex"), [])

    def test_codex_metadata_requires_description(self) -> None:
        skill_md = self.write_skill("---\nname: sample-skill\n---\n# Sample\n")
        findings = validate_skill_metadata(skill_md, platform="codex")
        self.assertEqual([f.code for f in findings], ["missing_description"])

    def test_missing_frontmatter_is_invalid(self) -> None:
        skill_md = self.write_skill("# Sample\n")
        findings = validate_skill_metadata(skill_md, platform="codex")
        self.assertEqual([f.code for f in findings], ["missing_frontmatter"])

    def test_duplicate_keys_are_invalid(self) -> None:
        skill_md = self.write_skill(
            "---\n"
            "name: sample-skill\n"
            "name: other-skill\n"
            "description: ok\n"
            "---\n"
        )
        findings = validate_skill_metadata(skill_md, platform="codex")
        self.assertIn("duplicate_frontmatter_key", [f.code for f in findings])

    def test_collect_metadata_ignores_top_level_hidden_install_dirs(self) -> None:
        root = Path(self.tmp.name)
        visible = root / "skills" / "visible"
        hidden = root / "skills" / ".hidden"
        visible.mkdir(parents=True)
        hidden.mkdir(parents=True)
        (visible / "SKILL.md").write_text("---\nname: visible\n---\n", encoding="utf-8")
        (hidden / "SKILL.md").write_text("# no frontmatter\n", encoding="utf-8")

        findings = collect_metadata_findings(root / "skills", platform="codex")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].code, "missing_description")

    def test_audit_checks_metadata_by_default_and_fails_invalid(self) -> None:
        root = Path(self.tmp.name)
        skill = root / "skills" / "pretest"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("# no frontmatter\n", encoding="utf-8")

        stdout = StringIO()
        with patch(
            "sys.argv",
            [
                "skills-audit",
                "audit",
                "--skills-dir",
                str(root / "skills"),
            ],
        ), redirect_stdout(stdout):
            exit_code = main()

        self.assertEqual(exit_code, 5)
        self.assertIn("metadata: check", stdout.getvalue())
        self.assertIn("missing_frontmatter", stdout.getvalue())

    def test_audit_can_skip_metadata_check(self) -> None:
        root = Path(self.tmp.name)
        skill = root / "skills" / "pretest"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("# no frontmatter\n", encoding="utf-8")

        stdout = StringIO()
        with patch(
            "sys.argv",
            [
                "skills-audit",
                "audit",
                "--skills-dir",
                str(root / "skills"),
                "--skip-metadata-check",
            ],
        ), redirect_stdout(stdout):
            exit_code = main()

        self.assertEqual(exit_code, 0)
        self.assertNotIn("metadata: check", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
