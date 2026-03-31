"""Tests for dedup subcommand logic (plan + apply)."""

import os
import tempfile
import unittest
from pathlib import Path

from skills_auditor.cli import apply_dedup, plan_dedup


def _write_skill(path: Path, name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\n---\n", encoding="utf-8")


class TestDedup(unittest.TestCase):
    def test_no_duplicates_empty_plan(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            skills = Path(base) / "skills"
            skills.mkdir()
            pack = skills / "foo"
            pack.mkdir()
            _write_skill(pack / "SKILL.md", "foo")
            actions, findings = plan_dedup(skills)
            self.assertEqual(findings, [])
            self.assertEqual(actions, [])

    def test_plan_picks_shortest_path_as_canonical(self) -> None:
        """Two distinct files with same name: shorter path wins canonical."""
        with tempfile.TemporaryDirectory() as base:
            skills = Path(base) / "skills"
            pack = skills / "pack"
            _write_skill(pack / "SKILL.md", "pack")
            _write_skill(pack / ".agents" / "skills" / "pack" / "SKILL.md", "pack")

            actions, findings = plan_dedup(skills)
            self.assertEqual(len(findings), 1)
            self.assertEqual(len(actions), 1)
            a = actions[0]
            self.assertEqual(a.action, "relink")
            self.assertIn("SKILL.md", a.canonical_path)
            self.assertIn(".agents", a.duplicate_path)

    def test_apply_creates_symlinks(self) -> None:
        """After apply, duplicate becomes a symlink resolving to canonical."""
        with tempfile.TemporaryDirectory() as base:
            skills = Path(base) / "skills"
            pack = skills / "pack"
            canonical = pack / "SKILL.md"
            _write_skill(canonical, "pack")
            dup = pack / "deep" / "nested" / "SKILL.md"
            _write_skill(dup, "pack")

            actions, _ = plan_dedup(skills)
            self.assertTrue(len(actions) > 0)
            applied = apply_dedup(actions)
            self.assertEqual(applied, 1)

            self.assertTrue(dup.is_symlink())
            self.assertEqual(dup.resolve(), canonical.resolve())
            # Content accessible through symlink
            self.assertIn("name: pack", dup.read_text(encoding="utf-8"))

    def test_symlinks_already_present_no_findings(self) -> None:
        """After dedup, re-running plan_dedup should find zero findings (symlinks resolved)."""
        with tempfile.TemporaryDirectory() as base:
            skills = Path(base) / "skills"
            pack = skills / "pack"
            canonical = pack / "SKILL.md"
            _write_skill(canonical, "pack")
            dup = pack / "mirror" / "SKILL.md"
            _write_skill(dup, "pack")

            actions, findings = plan_dedup(skills)
            self.assertEqual(len(findings), 1)
            apply_dedup(actions)

            # Second run: symlink resolves to canonical → zero findings
            actions2, findings2 = plan_dedup(skills)
            self.assertEqual(findings2, [])
            self.assertEqual(actions2, [])

    def test_multiple_bundles(self) -> None:
        """Dedup operates per-bundle independently."""
        with tempfile.TemporaryDirectory() as base:
            skills = Path(base) / "skills"
            # Bundle A: has duplicates
            a = skills / "alpha"
            _write_skill(a / "SKILL.md", "alpha")
            _write_skill(a / "copy" / "SKILL.md", "alpha")
            # Bundle B: no duplicates
            b = skills / "beta"
            _write_skill(b / "SKILL.md", "beta")

            actions, findings = plan_dedup(skills)
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0].bundle, "alpha")
            self.assertEqual(len(actions), 1)

    def test_three_way_duplicate_two_relinks(self) -> None:
        """Three distinct files with same name → two relinks, one canonical."""
        with tempfile.TemporaryDirectory() as base:
            skills = Path(base) / "skills"
            pack = skills / "gstack"
            _write_skill(pack / "SKILL.md", "gstack")
            _write_skill(pack / ".agents" / "skills" / "gstack" / "SKILL.md", "gstack")
            _write_skill(pack / ".factory" / "skills" / "gstack" / "SKILL.md", "gstack")

            actions, findings = plan_dedup(skills)
            self.assertEqual(len(findings), 1)
            relinks = [a for a in actions if a.action == "relink"]
            self.assertEqual(len(relinks), 2)

            applied = apply_dedup(actions)
            self.assertEqual(applied, 2)

            # All three paths now resolve to the same file
            canon_resolved = (pack / "SKILL.md").resolve()
            for sub in [
                pack / ".agents" / "skills" / "gstack" / "SKILL.md",
                pack / ".factory" / "skills" / "gstack" / "SKILL.md",
            ]:
                self.assertTrue(sub.is_symlink())
                self.assertEqual(sub.resolve(), canon_resolved)


if __name__ == "__main__":
    unittest.main()
