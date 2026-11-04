"""A registered skill can live inside a dependency-complete package snapshot."""
import tempfile
import unittest
from pathlib import Path

from skills_auditor.lifecycle.common import LifecycleError
from skills_auditor.lifecycle.engine import Manager


class TestPackageEntrypoints(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.source = self.root / "package"
        (self.source / "skills/alpha").mkdir(parents=True)
        (self.source / "skills/alpha/SKILL.md").write_text("---\nname: alpha\ndescription: fixture\n---\nRead ../../helper.txt\n")
        (self.source / "helper.txt").write_text("approved helper")
        self.target = self.root / "alpha"
        self.manager = Manager(self.root / "owner")
        self.addCleanup(self.manager.repository.close)

    def test_package_layout_update_and_explicit_rollback(self):
        plan = self.manager.plan("install", source=self.source, target=self.target,
                                 entrypoint="skills/alpha")
        receipt = self.manager.apply(plan, approve_plan_id=plan["plan_id"])
        first = receipt["version_id"]
        self.assertTrue(self.manager.verify(receipt["installation_id"])["valid"])
        self.assertEqual((self.target.resolve().parent.parent / "helper.txt").read_text(), "approved helper")
        (self.source / "helper.txt").write_text("candidate helper")
        self.assertEqual((self.target.resolve().parent.parent / "helper.txt").read_text(), "approved helper")
        update = self.manager.plan("update", installation_id=receipt["installation_id"],
                                   source=self.source, entrypoint="skills/alpha")
        self.manager.apply(update, approve_plan_id=update["plan_id"])
        self.assertEqual((self.target.resolve().parent.parent / "helper.txt").read_text(), "candidate helper")
        rollback = self.manager.plan("rollback", installation_id=receipt["installation_id"], version_id=first)
        self.manager.apply(rollback, approve_plan_id=rollback["plan_id"])
        self.assertEqual((self.target.resolve().parent.parent / "helper.txt").read_text(), "approved helper")

    def test_entrypoint_must_be_a_safe_skill_directory(self):
        for value in ("../escape", "/tmp/escape", "skills/../skills/alpha", "missing", "helper.txt", "", "bad\\path", "bad\npath"):
            with self.subTest(value=value), self.assertRaises(LifecycleError):
                self.manager.plan("install", source=self.source, target=self.target, entrypoint=value)
