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

    def test_symlinks_to_same_file_not_duplicate(self) -> None:
        """Symlinked SKILL.md pointing at canonical file — one logical skill (issue #2)."""
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            skills = root / "skills"
            skills.mkdir()
            pack = skills / "pkg"
            pack.mkdir()
            canon = pack / "SKILL.md"
            canon.write_text("---\nname: pkg\n---\n", encoding="utf-8")
            mirror = pack / "mirror"
            mirror.mkdir()
            link = mirror / "SKILL.md"
            try:
                link.symlink_to(canon)
            except OSError:
                self.skipTest("symlink creation not supported in this environment")
            found = collect_duplicate_skill_names(skills)
            self.assertEqual(found, [], msg=str(found))


    def test_cross_top_level_dup_same_name(self) -> None:
        """Hosts that rglob the install root see both top-level and nested pack exports."""
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            skills = root / "skills"
            skills.mkdir()

            flat = skills / "browse"
            flat.mkdir()
            (flat / "SKILL.md").write_text("---\nname: browse\n---\nbody\n", encoding="utf-8")

            nested = skills / "gstack" / "browse"
            nested.mkdir(parents=True)
            (nested / "SKILL.md").write_text("---\nname: browse\n---\nbody\n", encoding="utf-8")

            found = collect_duplicate_skill_names(skills)
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0].skill_name, "browse")
            self.assertEqual(found[0].bundle, "browse")
            self.assertEqual(len(found[0].skill_md_paths), 2)


if __name__ == "__main__":
    unittest.main()
