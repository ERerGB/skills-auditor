"""Tests for discovery-driven sync mapping."""

import tempfile
import unittest
from pathlib import Path

from skills_auditor.cli import discover_sync_mapping, skill_alias_for_install_root


class TestSyncDiscover(unittest.TestCase):
    def write_skill(self, root: Path, rel: str, name: str, body: str = "body") -> Path:
        skill = root / rel
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(f"---\nname: {name}\n---\n{body}\n", encoding="utf-8")
        return skill

    def test_alias_for_slash_name(self) -> None:
        self.assertEqual(skill_alias_for_install_root("magpie-loom/extract-leads"), "magpie-loom-extract-leads")

    def test_discovers_nested_skills(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            source = root / ".cursor" / "skills"
            skill = self.write_skill(source, "pack/subskills/leaf", "pack/leaf")
            mapping = discover_sync_mapping([source])
            self.assertEqual(mapping["pack-leaf"], str(skill.resolve()))

    def test_excludes_target_canonical_entry(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            target = root / ".agents" / "skills"
            skill = self.write_skill(target, "foo", "foo")
            mapping = discover_sync_mapping([target], exclude_target_root=target)
            self.assertNotIn("foo", mapping)
            self.assertTrue((skill / "SKILL.md").exists())

    def test_keeps_external_source_absolute(self) -> None:
        with tempfile.TemporaryDirectory() as base, tempfile.TemporaryDirectory() as external:
            target = Path(base) / ".codex" / "skills"
            target.mkdir(parents=True)
            source = Path(external) / "skills"
            skill = self.write_skill(source, "skills-auditor", "skills-auditor")
            mapping = discover_sync_mapping([source], exclude_target_root=target)
            self.assertEqual(mapping["skills-auditor"], str(skill.resolve()))

    def test_follows_symlinked_source_entries(self) -> None:
        with tempfile.TemporaryDirectory() as base, tempfile.TemporaryDirectory() as external:
            root = Path(base)
            source = root / "source"
            source.mkdir()
            external_skill = self.write_skill(Path(external), "external-skill", "external-skill")
            link = source / "external-skill"
            try:
                link.symlink_to(external_skill, target_is_directory=True)
            except OSError:
                self.skipTest("directory symlink creation not supported in this environment")
            mapping = discover_sync_mapping([source])
            self.assertEqual(mapping["external-skill"], str(external_skill.resolve()))

if __name__ == "__main__":
    unittest.main()
