"""Tests for hash-aware dedup (Feature C) and convention platform inference (Feature B)."""

import tempfile
import unittest
from pathlib import Path

from skills_auditor.cli import (
    CONVENTION_PLATFORM_MAP,
    apply_dedup,
    infer_platform_from_path,
    plan_dedup,
)


def _write_skill(path: Path, name: str, extra: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\n---\n{extra}", encoding="utf-8")


class TestHashAwareDedup(unittest.TestCase):
    def test_same_hash_relinks(self) -> None:
        """Identical content → action=relink."""
        with tempfile.TemporaryDirectory() as base:
            skills = Path(base) / "skills"
            pack = skills / "pack"
            _write_skill(pack / "SKILL.md", "pack", "body")
            _write_skill(pack / "copy" / "SKILL.md", "pack", "body")
            actions, findings = plan_dedup(skills)
            self.assertEqual(len(findings), 1)
            relinks = [a for a in actions if a.action == "relink"]
            self.assertEqual(len(relinks), 1)
            self.assertEqual(relinks[0].content_hash_canonical, relinks[0].content_hash_duplicate)

    def test_different_hash_skips(self) -> None:
        """Different content → action=skip_multi_version."""
        with tempfile.TemporaryDirectory() as base:
            skills = Path(base) / "skills"
            pack = skills / "pack"
            _write_skill(pack / "SKILL.md", "pack", "primary version")
            _write_skill(pack / ".agents" / "skills" / "pack" / "SKILL.md", "pack", "codex version")
            actions, findings = plan_dedup(skills)
            self.assertEqual(len(findings), 1)
            skips = [a for a in actions if a.action == "skip_multi_version"]
            self.assertEqual(len(skips), 1)
            self.assertNotEqual(
                skips[0].content_hash_canonical,
                skips[0].content_hash_duplicate,
            )

    def test_mixed_same_and_different_hash(self) -> None:
        """Three copies: one identical, one different → 1 relink + 1 skip."""
        with tempfile.TemporaryDirectory() as base:
            skills = Path(base) / "skills"
            pack = skills / "gstack"
            content = "---\nname: gstack\n---\nsame"
            (pack).mkdir(parents=True)
            (pack / "SKILL.md").write_text(content, encoding="utf-8")
            agents = pack / ".agents" / "skills" / "gstack"
            agents.mkdir(parents=True)
            (agents / "SKILL.md").write_text(content, encoding="utf-8")
            factory = pack / ".factory" / "skills" / "gstack"
            factory.mkdir(parents=True)
            (factory / "SKILL.md").write_text("---\nname: gstack\n---\ndifferent", encoding="utf-8")

            actions, _ = plan_dedup(skills)
            relinks = [a for a in actions if a.action == "relink"]
            skips = [a for a in actions if a.action == "skip_multi_version"]
            self.assertEqual(len(relinks), 1)
            self.assertEqual(len(skips), 1)

    def test_apply_only_relinks(self) -> None:
        """apply_dedup should only process relink actions, not skip_multi_version."""
        with tempfile.TemporaryDirectory() as base:
            skills = Path(base) / "skills"
            pack = skills / "pack"
            _write_skill(pack / "SKILL.md", "pack", "primary")
            _write_skill(pack / "variant" / "SKILL.md", "pack", "variant")
            actions, _ = plan_dedup(skills)
            applied = apply_dedup(actions)
            self.assertEqual(applied, 0, "should not symlink different-hash files")
            self.assertFalse((pack / "variant" / "SKILL.md").is_symlink())


class TestConventionPlatformInference(unittest.TestCase):
    def test_agents_infers_codex(self) -> None:
        p = Path("/bundle/.agents/skills/gstack/SKILL.md")
        self.assertEqual(infer_platform_from_path(p, Path("/bundle")), "codex")

    def test_factory_infers_factory(self) -> None:
        p = Path("/bundle/.factory/skills/gstack/SKILL.md")
        self.assertEqual(infer_platform_from_path(p, Path("/bundle")), "factory")

    def test_primary_returns_empty(self) -> None:
        p = Path("/bundle/browse/SKILL.md")
        self.assertEqual(infer_platform_from_path(p, Path("/bundle")), "")

    def test_codex_dir_infers_codex(self) -> None:
        p = Path("/bundle/.codex/skills/gstack/SKILL.md")
        self.assertEqual(infer_platform_from_path(p, Path("/bundle")), "codex")

    def test_dedup_reports_inferred_platform(self) -> None:
        """skip_multi_version actions carry the inferred platform label."""
        with tempfile.TemporaryDirectory() as base:
            skills = Path(base) / "skills"
            pack = skills / "pack"
            _write_skill(pack / "SKILL.md", "pack", "primary")
            _write_skill(pack / ".agents" / "skills" / "pack" / "SKILL.md", "pack", "codex")
            actions, _ = plan_dedup(skills)
            skips = [a for a in actions if a.action == "skip_multi_version"]
            self.assertEqual(len(skips), 1)
            self.assertEqual(skips[0].inferred_platform, "codex")

    def test_convention_map_has_known_dirs(self) -> None:
        self.assertIn(".agents", CONVENTION_PLATFORM_MAP)
        self.assertIn(".factory", CONVENTION_PLATFORM_MAP)
        self.assertIn(".codex", CONVENTION_PLATFORM_MAP)


if __name__ == "__main__":
    unittest.main()
