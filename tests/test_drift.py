"""Tests for git drift: repo-wide vs skill-scoped dirty counts."""

import subprocess
import tempfile
import unittest
from pathlib import Path

from skills_auditor.cli import check_drift_for_path


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


class TestDriftScopedDirty(unittest.TestCase):
    def test_monorepo_skill_clean_rest_repo_dirty(self) -> None:
        """Skill subtree has no changes; another path in the repo is dirty."""
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            _git(root, "init")
            _git(root, "config", "user.email", "t@example.com")
            _git(root, "config", "user.name", "test")
            skill = root / ".cursor" / "skills" / "myskill"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("skill", encoding="utf-8")
            other = root / "apps" / "other.txt"
            other.parent.mkdir(parents=True)
            other.write_text("app", encoding="utf-8")
            _git(root, "add", ".")
            _git(root, "commit", "-m", "init")
            other.write_text("changed", encoding="utf-8")

            d = check_drift_for_path("myskill", skill)
            self.assertGreater(d.dirty_count, 0)
            self.assertEqual(d.skill_dirty_count, 0)
            self.assertEqual(d.ahead, 0)
            self.assertEqual(d.behind, 0)

    def test_skill_tree_dirty(self) -> None:
        """Uncommitted change under the skill path increments skill_dirty_count."""
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            _git(root, "init")
            _git(root, "config", "user.email", "t@example.com")
            _git(root, "config", "user.name", "test")
            skill = root / "pack" / "myskill"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("v1", encoding="utf-8")
            _git(root, "add", ".")
            _git(root, "commit", "-m", "init")
            (skill / "SKILL.md").write_text("v2", encoding="utf-8")

            d = check_drift_for_path("myskill", skill)
            self.assertGreaterEqual(d.skill_dirty_count, 1)
            self.assertGreaterEqual(d.dirty_count, 1)
            self.assertEqual(d.skill_dirty_count, d.dirty_count)
