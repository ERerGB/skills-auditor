"""Tests for duplicate frontmatter name check (audit tail)."""

import tempfile
import unittest
from pathlib import Path

from skills_auditor.cli import collect_duplicate_skill_names


class TestDuplicateNames(unittest.TestCase):
    def test_no_dup_in_flat_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            skills = root / "skills"
            skills.mkdir()
            b = skills / "foo"
            b.mkdir()
            (b / "SKILL.md").write_text("---\nname: foo\n---\n", encoding="utf-8")
            self.assertEqual(collect_duplicate_skill_names(skills), [])

    def test_dup_nested_same_name(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            skills = root / "skills"
            skills.mkdir()
            pack = skills / "pack"
            pack.mkdir()
            (pack / "SKILL.md").write_text("---\nname: pack\n---\n", encoding="utf-8")
            nested = pack / "vendor" / "nested"
            nested.mkdir(parents=True)
            (nested / "SKILL.md").write_text("---\nname: pack\n---\n", encoding="utf-8")
            found = collect_duplicate_skill_names(skills)
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0].bundle, "pack")
            self.assertEqual(found[0].skill_name, "pack")
            self.assertEqual(len(found[0].skill_md_paths), 2)


if __name__ == "__main__":
    unittest.main()
