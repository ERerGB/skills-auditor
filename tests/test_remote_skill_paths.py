"""Regressions for registrations copied from a different filesystem."""
import tempfile
from pathlib import Path

from tests.test_integration import IntegrationFixture
from skills_auditor.cli import discover_sync_mapping, default_sync_discover_sources
from skills_auditor.environments import BUILTIN_ENVIRONMENTS
from skills_auditor.integration import IntegrationError, build_integration_plan
from unittest.mock import patch


class TestRemoteSkillPaths(IntegrationFixture):
    def test_partial_source_with_foreign_link_cannot_produce_successful_plan(self):
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            source = root / "source"
            self.write_skill(source, "available")
            (source / "unavailable").symlink_to("/foreign-host/skills/unavailable")
            with self.assertRaises(IntegrationError) as caught:
                build_integration_plan(self.spec(root, source, root / "installed"))
            self.assertEqual(caught.exception.code, "source_link_unavailable")
            self.assertIn("unavailable", str(caught.exception))
            self.assertFalse((root / "installed").exists())

    def test_discovery_rejects_broken_root_nested_link_and_loop(self):
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            for name, target in (("missing", "/foreign-host/skill"), ("loop", "loop")):
                link = root / name
                link.symlink_to(target)
                for source in (link, root):
                    with self.subTest(source=source, name=name), self.assertRaisesRegex(ValueError, "[Ss]ource.*link"):
                        discover_sync_mapping([source])
                link.unlink()

    def test_audit_returns_failure_for_broken_install_link(self):
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            self.write_skill(root, "available")
            (root / "missing").symlink_to("/foreign-host/skill")
            code, output, _ = self.run_cli("audit", "--skills-dir", str(root))
            self.assertEqual(code, 3, output)
            self.assertIn("broken", output)

    def test_codex_uses_native_roots_and_keeps_legacy_discovery(self):
        codex = BUILTIN_ENVIRONMENTS.get("codex")
        self.assertEqual(codex.primary_project_root(Path("/project")), Path("/project/.agents/skills"))
        self.assertEqual(codex.primary_global_root(Path("/user")), Path("/user/.agents/skills"))
        with patch("skills_auditor.cli.Path.cwd", return_value=Path("/project")):
            roots = default_sync_discover_sources(False)
        self.assertIn(Path("/project/.codex/skills"), roots)
        self.assertEqual(roots.count(Path("/project/.agents/skills")), 1)
