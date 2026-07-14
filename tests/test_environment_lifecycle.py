"""Cross-environment discovery-entry lifecycle contract tests."""

import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from skills_auditor.cli import apply_actions, discover_sync_mapping, main, plan_sync
from skills_auditor.environments import (
    BUILTIN_ENVIRONMENTS,
    NativeEnvironment,
    NativeEnvironmentRegistry,
)


class TestEnvironmentLifecycle(unittest.TestCase):
    def write_skill(self, root: Path, name: str, body: str) -> Path:
        skill = root / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: lifecycle fixture\n---\n{body}\n",
            encoding="utf-8",
        )
        return skill

    def run_cli(self, *argv: str) -> tuple[int, str]:
        stdout = StringIO()
        with patch("sys.argv", ["skills-audit", *argv]), redirect_stdout(stdout):
            exit_code = main()
        return exit_code, stdout.getvalue()

    def test_discover_register_update_archive_lifecycle_in_all_builtin_environments(self) -> None:
        for environment in BUILTIN_ENVIRONMENTS.all():
            with self.subTest(environment=environment.key), tempfile.TemporaryDirectory() as base:
                project = Path(base)
                canonical_root = project / ".agents" / "skills"
                canonical = self.write_skill(canonical_root, "product-research", "canonical-v1")
                install_root = environment.primary_project_root(project)

                mapping = discover_sync_mapping([canonical_root], exclude_target_root=install_root)
                self.assertEqual(mapping, {"product-research": str(canonical.resolve())})

                dry_code, dry_output = self.run_cli(
                    "sync-discover",
                    "--source",
                    str(canonical_root),
                    "--skills-dir",
                    str(install_root),
                )
                self.assertEqual(dry_code, 1)
                self.assertIn("product-research\tcreate_link", dry_output)

                apply_actions(install_root, plan_sync(install_root, mapping))
                entry = install_root / "product-research"
                self.assertEqual(entry.resolve(), canonical.resolve())

                stale = self.write_skill(project / "stale", "product-research", "stale")
                entry.unlink()
                entry.symlink_to(stale, target_is_directory=True)
                update_plan = plan_sync(install_root, mapping)
                self.assertEqual(update_plan[0].action, "replace_link")
                apply_actions(install_root, update_plan)
                self.assertEqual(entry.resolve(), canonical.resolve())

                entry.unlink()
                self.write_skill(install_root, "product-research", "native-local-copy")
                archive_plan = plan_sync(install_root, mapping)
                self.assertEqual(archive_plan[0].action, "archive_and_link")
                apply_actions(install_root, archive_plan)
                archives = list(install_root.glob("product-research.archived-*"))
                self.assertEqual(len(archives), 1)
                self.assertIn("native-local-copy", (archives[0] / "SKILL.md").read_text())
                self.assertEqual(entry.resolve(), canonical.resolve())

                verify_plan = plan_sync(install_root, mapping)
                self.assertEqual([action.action for action in verify_plan], ["noop"])

    def test_third_party_environment_can_be_added_without_lifecycle_changes(self) -> None:
        registry = NativeEnvironmentRegistry(BUILTIN_ENVIRONMENTS.all())
        registry.register(
            NativeEnvironment("acme-agent", (".acme/skills",), (".acme/skills",))
        )

        with tempfile.TemporaryDirectory() as base:
            project = Path(base)
            environment = registry.get("acme-agent")
            install_root = environment.primary_project_root(project)
            source_root = project / ".agents" / "skills"
            canonical = self.write_skill(source_root, "product-research", "canonical")
            mapping = discover_sync_mapping([source_root], exclude_target_root=install_root)
            apply_actions(install_root, plan_sync(install_root, mapping))

            self.assertEqual(install_root, project / ".acme/skills")
            self.assertEqual((install_root / "product-research").resolve(), canonical.resolve())
            self.assertEqual([a.action for a in plan_sync(install_root, mapping)], ["noop"])

        self.assertEqual(len(registry.all()), 4)
