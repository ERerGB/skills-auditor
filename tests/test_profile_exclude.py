"""Tests for profile-based exclude patterns (Feature A) and SourceSpec.exclude_patterns."""

import tempfile
import unittest
from pathlib import Path

from skills_auditor.cli import (
    SourceSpec,
    discover_from_source,
    parse_profile_source_entries,
)


def _write_skill(path: Path, name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\n---\n", encoding="utf-8")


class TestParseProfileExclude(unittest.TestCase):
    def test_exclude_parsed(self) -> None:
        raw = [{"path": "/tmp/skills", "platform": ["cursor"], "exclude": [".agents/*"]}]
        specs = parse_profile_source_entries(raw)
        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0].exclude_patterns, [".agents/*"])

    def test_exclude_defaults_empty(self) -> None:
        raw = [{"path": "/tmp/skills", "platform": ["cursor"]}]
        specs = parse_profile_source_entries(raw)
        self.assertEqual(specs[0].exclude_patterns, [])

    def test_string_source_has_empty_exclude(self) -> None:
        raw = ["/tmp/skills"]
        specs = parse_profile_source_entries(raw)
        self.assertEqual(specs[0].exclude_patterns, [])

    def test_invalid_exclude_type(self) -> None:
        raw = [{"path": "/tmp/skills", "platform": ["cursor"], "exclude": "bad"}]
        with self.assertRaises(ValueError):
            parse_profile_source_entries(raw)

    def test_sourcespec_default(self) -> None:
        spec = SourceSpec(Path("/tmp"), ["*"])
        self.assertEqual(spec.exclude_patterns, [])


class TestDiscoverExcludePatterns(unittest.TestCase):
    def test_exclude_filters_child_dirs(self) -> None:
        """Skills under excluded sub-dirs should not appear in discovery."""
        with tempfile.TemporaryDirectory() as base:
            root = Path(base) / "skills"
            _write_skill(root / "browse" / "SKILL.md", "browse")
            _write_skill(root / ".agents" / "skills" / "browse" / "SKILL.md", "browse-codex")
            _write_skill(root / ".factory" / "skills" / "browse" / "SKILL.md", "browse-factory")

            all_items = discover_from_source(root, 0, [])
            self.assertEqual(len(all_items), 3)

            filtered = discover_from_source(
                root, 0, [],
                exclude_patterns=[".agents/*", ".factory/*"],
            )
            names = {i.skill_name for i in filtered}
            self.assertIn("browse", names)
            self.assertNotIn("browse-codex", names)
            self.assertNotIn("browse-factory", names)

    def test_no_exclude_returns_all(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            root = Path(base) / "skills"
            _write_skill(root / "a" / "SKILL.md", "a")
            _write_skill(root / "b" / "SKILL.md", "b")
            items = discover_from_source(root, 0, [], exclude_patterns=[])
            self.assertEqual(len(items), 2)


if __name__ == "__main__":
    unittest.main()
