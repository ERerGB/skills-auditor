import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from skills_auditor.lifecycle.common import LifecycleError
from skills_auditor.workspace.common import PREFIX
from skills_auditor.workspace.node import Node
from skills_auditor.workspace.service import WorkspaceService


class WorkspaceFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        home_patch = patch("pathlib.Path.home", return_value=self.home)
        home_patch.start()
        self.addCleanup(home_patch.stop)
        self.service = WorkspaceService(self.root / "controller")
        self.registry = self.service.registry
        self.environment = {"provider": "local", "generation": "one", "state_dir": str(self.root / "node")}
        self.registry.put("environments", "local", self.environment)
        self.source = self.root / "source"
        self.write_package(self.source, "H1")
        self.binding = {"root": str(self.source), "kind": "local", "include": ["skills", "helper"],
                        "entries": {"alpha": "skills/alpha"}, "platforms": ["codex", "cursor", "claude-code"]}
        self.registry.put("sources", "shared", self.binding)
        self.one = self.root / "one"
        self.two = self.root / "two"
        for name, path in (("one", self.one), ("two", self.two)):
            path.mkdir()
            self.write_declaration(path)
            self.service.register(name, "local", path, path / "skills-auditor.workspace.json")

    def write_package(self, root, body):
        (root / "skills/alpha").mkdir(parents=True)
        (root / "skills/alpha/SKILL.md").write_text("---\nname: alpha\ndescription: fixture\n---\nRead ../../helper\n")
        (root / "helper").write_text(body)

    def write_declaration(self, path, **extra):
        value = {"schema_version": PREFIX + "declaration/v1", "baseline": ["shared"], **extra}
        (path / "skills-auditor.workspace.json").write_text(json.dumps(value))

    def apply(self, plan):
        return self.service.apply(plan, approve_plan_id=plan["plan_id"])


class TestWorkspaceService(WorkspaceFixture):
    def test_two_workspaces_three_platforms_immutable_updates_and_rollback(self):
        plan = self.service.plan()
        self.assertEqual(plan["status"], "planned", plan)
        self.assertEqual(plan["summary"]["changes"], 6)
        self.assertFalse((self.one / ".agents/skills/alpha").exists())
        self.assertEqual(self.apply(plan)["status"], "completed")
        first = (self.one / ".agents/skills/alpha").resolve()
        self.assertEqual((first.parent.parent / "helper").read_text(), "H1")
        self.assertEqual((self.two / ".claude/skills/alpha").resolve(), first)
        self.assertTrue(self.service.verify()["valid"])
        self.assertEqual(self.service.plan()["summary"]["changes"], 0)
        (self.source / "helper").write_text("H2")
        self.assertEqual((first.parent.parent / "helper").read_text(), "H1")
        update = self.service.plan()
        self.assertEqual(self.apply(update)["status"], "completed")
        self.assertEqual(((self.one / ".agents/skills/alpha").resolve().parent.parent / "helper").read_text(), "H2")
        inverse = self.service.rollback(update)
        self.assertEqual(self.apply(inverse)["status"], "completed")
        self.assertEqual((self.one / ".agents/skills/alpha").resolve(), first)

    def test_explicit_project_override_isolated_from_shared_baseline(self):
        other = self.root / "other"
        self.write_package(other, "project exception")
        self.registry.put("sources", "project", dict(self.binding, root=str(other)))
        self.write_declaration(self.two, overrides={"alpha": {"source": "project", "entry": "skills/alpha"}})
        plan = self.service.plan()
        self.assertEqual(self.apply(plan)["status"], "completed")
        self.assertNotEqual((self.one / ".agents/skills/alpha").resolve(), (self.two / ".agents/skills/alpha").resolve())
        self.assertEqual(((self.two / ".agents/skills/alpha").resolve().parent.parent / "helper").read_text(), "project exception")
        self.assertEqual(self.service.plan()["summary"]["changes"], 0)

    def test_source_declaration_and_environment_drift_stop_before_effects(self):
        plan = self.service.plan()
        with self.assertRaises(LifecycleError):
            self.service.apply(plan, approve_plan_id="wrong")
        (self.source / "helper").write_text("H2")
        with self.assertRaises(LifecycleError) as caught:
            self.apply(plan)
        self.assertEqual(caught.exception.code, "workspace_stale")
        self.assertFalse((self.one / ".agents/skills/alpha").exists())
        plan = self.service.plan()
        self.write_declaration(self.one, overrides={"alpha": None})
        with self.assertRaises(LifecycleError):
            self.apply(plan)
        plan = self.service.plan()
        self.registry.put("environments", "local", dict(self.environment, generation="two"))
        with self.assertRaises(LifecycleError):
            self.apply(plan)
        self.assertEqual(self.service.status()["environments"]["local"]["status"], "unknown")

    def test_native_conflicts_and_foreign_entries_block_without_overwrite(self):
        native = self.one / ".agents/skills/alpha"
        native.mkdir()
        (native / "SKILL.md").write_text("---\nname: alpha\ndescription: native\n---\n")
        plan = self.service.plan()
        self.assertEqual(plan["status"], "blocked")
        with self.assertRaises(LifecycleError):
            self.apply(plan)
        self.assertTrue(native.is_dir() and not native.is_symlink())

    def test_partial_failure_requires_explicit_recovery_without_repeating_effects(self):
        plan = self.service.plan()
        original = Node._execute
        calls = []
        def fail_one(node, node_plan, journal, path, manager):
            calls.append(node_plan["workspace"])
            if node_plan["workspace"] == "two":
                raise LifecycleError("fixture_failure", "injected failure before execution")
            return original(node, node_plan, journal, path, manager)
        with patch.object(Node, "_execute", fail_one):
            result = self.apply(plan)
        self.assertEqual(result["status"], "partial")
        first = (self.one / ".agents/skills/alpha").lstat().st_ino
        with self.assertRaises(LifecycleError) as caught:
            self.apply(plan)
        self.assertEqual(caught.exception.code, "workspace_recovery_required")
        recovered = self.service.recover(plan, mode="resume", approve_plan_id=plan["plan_id"])
        self.assertEqual(recovered["status"], "completed", recovered)
        self.assertEqual((self.one / ".agents/skills/alpha").lstat().st_ino, first)
        self.assertTrue(self.service.verify()["valid"])
