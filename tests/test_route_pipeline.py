"""Tests for the Select-One Routing pipeline (route_pipeline)."""

import tempfile
import unittest
from pathlib import Path

from skills_auditor.cli import (
    apply_route,
    route_pipeline,
)


def _write_skill(path: Path, name: str, extra: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\n---\n{extra}", encoding="utf-8")


class TestRoutePipelineTrueDuplicate(unittest.TestCase):
    """All variants have identical content → relink non-primary."""

    def test_true_duplicate_relinks(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            skills = Path(base) / "skills"
            pack = skills / "pack"
            content = "---\nname: pack\n---\nsame content"
            _write_skill(pack / "SKILL.md", "pack", "same content")
            _write_skill(pack / ".agents" / "skills" / "pack" / "SKILL.md", "pack", "same content")

            trace, actions = route_pipeline(
                skills, active_platform="cursor",
                trace_dir=Path(base) / "traces",
            )
            self.assertEqual(len(trace.identities), 1)
            ident = trace.identities[0]
            self.assertIsNotNone(ident.final_selected)
            self.assertIn("pack/SKILL.md", ident.final_selected)

            relinks = [a for a in actions if a.action == "relink"]
            self.assertEqual(len(relinks), 1)

    def test_true_duplicate_three_way(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            skills = Path(base) / "skills"
            pack = skills / "gstack"
            body = "same"
            _write_skill(pack / "SKILL.md", "gstack", body)
            _write_skill(pack / ".agents" / "skills" / "gstack" / "SKILL.md", "gstack", body)
            _write_skill(pack / ".factory" / "skills" / "gstack" / "SKILL.md", "gstack", body)

            trace, actions = route_pipeline(
                skills, active_platform="cursor",
                trace_dir=Path(base) / "traces",
            )
            relinks = [a for a in actions if a.action == "relink"]
            self.assertEqual(len(relinks), 2)


class TestRoutePipelineVariantDetected(unittest.TestCase):
    """Variants have different content → platform-based routing."""

    def test_cursor_selects_primary_archives_codex(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            skills = Path(base) / "skills"
            pack = skills / "browse"
            _write_skill(pack / "SKILL.md", "browse", "full version for cursor")
            _write_skill(
                pack / ".agents" / "skills" / "browse" / "SKILL.md",
                "browse", "trimmed for codex",
            )

            trace, actions = route_pipeline(
                skills, active_platform="cursor",
                resolve_strategy="archive",
                trace_dir=Path(base) / "traces",
            )
            ident = trace.identities[0]
            self.assertIn("browse/SKILL.md", ident.final_selected)

            archives = [a for a in actions if a.action == "archive"]
            self.assertEqual(len(archives), 1)
            self.assertIn(".agents", archives[0].duplicate_path)
            self.assertEqual(archives[0].inferred_platform, "codex")

    def test_codex_selects_agents_variant(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            skills = Path(base) / "skills"
            pack = skills / "browse"
            _write_skill(pack / "SKILL.md", "browse", "full version")
            _write_skill(
                pack / ".agents" / "skills" / "browse" / "SKILL.md",
                "browse", "codex version",
            )

            trace, actions = route_pipeline(
                skills, active_platform="codex",
                resolve_strategy="archive",
                trace_dir=Path(base) / "traces",
            )
            ident = trace.identities[0]
            self.assertIn(".agents", ident.final_selected)
            self.assertEqual(len(ident.final_superseded), 1)

    def test_delete_strategy(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            skills = Path(base) / "skills"
            pack = skills / "x"
            _write_skill(pack / "SKILL.md", "x", "primary")
            _write_skill(pack / ".factory" / "skills" / "x" / "SKILL.md", "x", "factory")

            trace, actions = route_pipeline(
                skills, active_platform="cursor",
                resolve_strategy="delete",
                trace_dir=Path(base) / "traces",
            )
            deletes = [a for a in actions if a.action == "delete"]
            self.assertEqual(len(deletes), 1)

    def test_keep_strategy(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            skills = Path(base) / "skills"
            pack = skills / "x"
            _write_skill(pack / "SKILL.md", "x", "primary")
            _write_skill(pack / ".agents" / "skills" / "x" / "SKILL.md", "x", "codex")

            trace, actions = route_pipeline(
                skills, active_platform="cursor",
                resolve_strategy="keep",
                trace_dir=Path(base) / "traces",
            )
            keeps = [a for a in actions if a.action == "keep"]
            self.assertEqual(len(keeps), 1)


class TestRoutePipelineFallback(unittest.TestCase):
    """When no variant matches the active platform, primary is selected as fallback."""

    def test_fallback_to_primary(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            skills = Path(base) / "skills"
            pack = skills / "x"
            _write_skill(pack / "SKILL.md", "x", "primary")
            _write_skill(pack / ".agents" / "skills" / "x" / "SKILL.md", "x", "codex only")

            trace, actions = route_pipeline(
                skills, active_platform="factory",
                trace_dir=Path(base) / "traces",
            )
            ident = trace.identities[0]
            self.assertIn("x/SKILL.md", ident.final_selected)


class TestApplyRoute(unittest.TestCase):
    def test_archive_renames_file(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            skills = Path(base) / "skills"
            pack = skills / "x"
            _write_skill(pack / "SKILL.md", "x", "primary")
            dup = pack / ".agents" / "skills" / "x" / "SKILL.md"
            _write_skill(dup, "x", "codex")

            trace, actions = route_pipeline(
                skills, active_platform="cursor",
                resolve_strategy="archive",
                trace_dir=Path(base) / "traces",
            )
            applied = apply_route(actions, skills)
            self.assertEqual(applied, 1)
            self.assertFalse(dup.exists())
            archived = list(dup.parent.glob("SKILL.md.archived-*"))
            self.assertEqual(len(archived), 1)

    def test_delete_removes_file(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            skills = Path(base) / "skills"
            pack = skills / "x"
            _write_skill(pack / "SKILL.md", "x", "primary")
            dup = pack / ".factory" / "skills" / "x" / "SKILL.md"
            _write_skill(dup, "x", "factory")

            trace, actions = route_pipeline(
                skills, active_platform="cursor",
                resolve_strategy="delete",
                trace_dir=Path(base) / "traces",
            )
            applied = apply_route(actions, skills)
            self.assertEqual(applied, 1)
            self.assertFalse(dup.exists())

    def test_relink_creates_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            skills = Path(base) / "skills"
            pack = skills / "x"
            body = "same"
            _write_skill(pack / "SKILL.md", "x", body)
            dup = pack / "copy" / "SKILL.md"
            _write_skill(dup, "x", body)

            trace, actions = route_pipeline(
                skills, active_platform="cursor",
                trace_dir=Path(base) / "traces",
            )
            applied = apply_route(actions, skills)
            self.assertEqual(applied, 1)
            self.assertTrue(dup.is_symlink())


class TestTraceOutput(unittest.TestCase):
    def test_trace_written_to_disk(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            skills = Path(base) / "skills"
            pack = skills / "x"
            _write_skill(pack / "SKILL.md", "x", "a")
            _write_skill(pack / ".agents" / "skills" / "x" / "SKILL.md", "x", "b")

            trace_dir = Path(base) / "traces"
            trace, _ = route_pipeline(
                skills, active_platform="cursor", trace_dir=trace_dir,
            )
            files = list(trace_dir.glob("*.json"))
            self.assertEqual(len(files), 1)

            data = __import__("json").loads(files[0].read_text())
            self.assertEqual(data["active_platform"], "cursor")
            self.assertTrue(len(data["identities"]) > 0)
            self.assertTrue(len(data["identities"][0]["transitions"]) > 0)


if __name__ == "__main__":
    unittest.main()
