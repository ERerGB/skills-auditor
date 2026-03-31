"""Tests for platform-aware discovery profiles and sync filtering."""

import json
import tempfile
import unittest
from pathlib import Path

from skills_auditor.cli import (
    PLATFORM_WILDCARD,
    SourceSpec,
    infer_default_platforms_for_source,
    load_discovery_profile,
    longest_matching_source_platforms,
    parse_profile_source_entries,
    platform_allows_target,
    plan_sync,
)


class TestParseProfileSources(unittest.TestCase):
    def test_plain_string_is_wildcard(self) -> None:
        specs = parse_profile_source_entries(["/tmp/a", "/tmp/b"])
        self.assertEqual(specs[0].platforms, [PLATFORM_WILDCARD])
        self.assertEqual(specs[1].path, Path("/tmp/b"))

    def test_object_entry(self) -> None:
        specs = parse_profile_source_entries(
            [{"path": "/tmp/x", "platform": ["cursor"]}]
        )
        self.assertEqual(specs[0].platforms, ["cursor"])

    def test_mixed(self) -> None:
        specs = parse_profile_source_entries(
            [
                "~/.cursor/skills",
                {"path": "~/.claude/skills", "platform": ["claude-code"]},
            ]
        )
        self.assertEqual(specs[0].platforms, [PLATFORM_WILDCARD])
        self.assertEqual(specs[1].platforms, ["claude-code"])


class TestPlatformHelpers(unittest.TestCase):
    def test_platform_allows_target(self) -> None:
        self.assertTrue(platform_allows_target(["*"], "claude-code"))
        self.assertTrue(platform_allows_target(["cursor", "claude-code"], "cursor"))
        self.assertFalse(platform_allows_target(["cursor"], "claude-code"))

    def test_longest_prefix_wins(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            shared = root / "shared"
            nested = root / "shared" / "nested"
            nested.mkdir(parents=True)
            specs = [
                SourceSpec(shared, ["cursor"]),
                SourceSpec(nested, ["claude-code"]),
            ]
            skill = nested / "my-skill"
            plats = longest_matching_source_platforms(skill, specs)
            self.assertEqual(plats, ["claude-code"])

    def test_no_match_is_wildcard(self) -> None:
        specs = [SourceSpec(Path("/nonexistent-root-xyz"), ["cursor"])]
        plats = longest_matching_source_platforms(Path("/tmp/other/skill"), specs)
        self.assertEqual(plats, [PLATFORM_WILDCARD])


class TestLoadDiscoveryProfileFile(unittest.TestCase):
    def test_roundtrip_strings(self) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            json.dump(
                {
                    "sources": ["/tmp/only-strings"],
                    "exclude_sources": [],
                    "collapse_identical": True,
                },
                f,
            )
            path = Path(f.name)
        try:
            prof = load_discovery_profile(path)
            specs = prof["source_specs"]
            assert isinstance(specs, list)
            self.assertEqual(specs[0].platforms, [PLATFORM_WILDCARD])
        finally:
            path.unlink(missing_ok=True)


class TestPlanSyncSkipPlatform(unittest.TestCase):
    def test_skip_when_platform_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            cursor_only = root / "cursor-only" / "batch"
            cursor_only.mkdir(parents=True)
            (cursor_only / "SKILL.md").write_text("---\nname: batch\n---\n", encoding="utf-8")
            specs = [SourceSpec(cursor_only, ["cursor"])]
            mapping = {"batch": str(cursor_only)}
            actions = plan_sync(
                root / "claude_skills",
                mapping,
                target_platform="claude-code",
                source_specs=specs,
            )
            self.assertEqual(len(actions), 1)
            self.assertEqual(actions[0].action, "skip_platform")

    def test_no_skip_when_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            skill_dir = root / "skill"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text("---\nname: x\n---\n", encoding="utf-8")
            specs = [SourceSpec(skill_dir, ["claude-code"])]
            mapping = {"x": str(skill_dir)}
            dest = root / "dest"
            dest.mkdir()
            actions = plan_sync(
                dest,
                mapping,
                target_platform="claude-code",
                source_specs=specs,
            )
            self.assertTrue(any(a.action == "create_link" for a in actions))


class TestInferDefaultPlatforms(unittest.TestCase):
    def test_claude_path(self) -> None:
        p = Path("~/.claude/skills").expanduser()
        self.assertEqual(infer_default_platforms_for_source(p), ["claude-code"])

    def test_skills_cursor(self) -> None:
        p = Path("~/.cursor/skills-cursor").expanduser()
        self.assertEqual(infer_default_platforms_for_source(p), ["cursor"])


if __name__ == "__main__":
    unittest.main()
