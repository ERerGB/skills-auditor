"""Filesystem recovery guarantees exercised through the public apply entry point."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from skills_auditor.integration import (
    IntegrationError,
    IntegrationSpec,
    IntegrationTarget,
    apply_integration_plan,
    build_integration_plan,
    verify_receipt,
)


class TestIntegrationRecovery(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name).resolve()
        self.source = self.project / "source"
        self.target = self.project / "target"
        self.canonical = self.write_skill(self.source, "canonical payload")
        self.canonical_bytes = (self.canonical / "SKILL.md").read_bytes()
        self.target.mkdir()
        self.entry = self.target / "alpha"
        self.receipt_path = self.project / "failed-receipt.json"

    def write_skill(self, root: Path, body: str) -> Path:
        skill = root / "alpha"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\nname: alpha\ndescription: recovery fixture\n---\n\n" + body + "\n",
            encoding="utf-8",
        )
        return skill

    def build_plan(self, expected_action: str) -> dict:
        plan = build_integration_plan(
            IntegrationSpec(
                project_root=self.project,
                sources=(self.source,),
                targets=(IntegrationTarget("test", root=self.target),),
            )
        )
        self.assertEqual(plan["targets"][0]["actions"][0]["action"], expected_action)
        return plan

    def assert_failed_apply(self, plan: dict, *messages: str) -> None:
        with self.assertRaises(IntegrationError) as caught:
            apply_integration_plan(plan, self.receipt_path)
        self.assertEqual(caught.exception.code, "apply_failed")
        self.assertEqual(caught.exception.exit_code, 3)
        self.assertIn({"receipt_path": str(self.receipt_path)}, caught.exception.details)
        failed = json.loads(self.receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["plan_id"], plan["plan_id"])
        self.assertEqual(failed["results"], [])
        self.assertEqual(failed["error"]["code"], "apply_failed")
        for message in messages:
            self.assertIn(message, str(caught.exception))
            self.assertIn(message, failed["error"]["message"])
        verification = verify_receipt(failed)
        self.assertEqual(verification["status"], "failed")
        self.assertEqual(
            verification["approval"],
            {
                "state": "invalidated",
                "requires_reapproval": True,
                "reason_codes": ["receipt_not_completed"],
            },
        )
        self.assertEqual((self.canonical / "SKILL.md").read_bytes(), self.canonical_bytes)

    def test_replace_failure_restores_original_relative_link(self) -> None:
        previous = self.write_skill(self.project / "previous", "previous payload")
        previous_bytes = (previous / "SKILL.md").read_bytes()
        raw_target = "../previous/./alpha"
        self.entry.symlink_to(raw_target, target_is_directory=True)
        plan = self.build_plan("replace_link")
        real_symlink = os.symlink

        def fail_new_link(source, destination, *args, **kwargs):
            if str(source) == str(self.canonical):
                raise OSError("new link denied")
            return real_symlink(source, destination, *args, **kwargs)

        with patch("skills_auditor.integration.os.symlink", side_effect=fail_new_link):
            self.assert_failed_apply(plan, "new link denied")

        self.assertTrue(self.entry.is_symlink())
        self.assertEqual(os.readlink(self.entry), raw_target)
        self.assertEqual(self.entry.resolve(), previous.resolve())
        self.assertEqual((self.entry / "SKILL.md").read_bytes(), previous_bytes)
        self.assertEqual((previous / "SKILL.md").read_bytes(), previous_bytes)
        self.assertEqual(list(self.target.iterdir()), [self.entry])

    def test_replace_and_restore_failures_preserve_both_errors(self) -> None:
        previous = self.write_skill(self.project / "previous", "previous payload")
        previous_bytes = (previous / "SKILL.md").read_bytes()
        self.entry.symlink_to("../previous/alpha", target_is_directory=True)
        plan = self.build_plan("replace_link")

        with patch(
            "skills_auditor.integration.os.symlink",
            side_effect=[OSError("new link denied"), OSError("previous link restore denied")],
        ):
            self.assert_failed_apply(plan, "new link denied", "previous link restore denied")

        self.assertFalse(self.entry.exists())
        self.assertFalse(self.entry.is_symlink())
        self.assertEqual((previous / "SKILL.md").read_bytes(), previous_bytes)
        self.assertEqual(list(self.target.iterdir()), [])

    def test_archive_restore_failure_preserves_native_contents_in_archive(self) -> None:
        native = self.write_skill(self.target, "native payload")
        native_bytes = (native / "SKILL.md").read_bytes()
        (native / "local-only.txt").write_text("local data", encoding="utf-8")
        plan = self.build_plan("archive_and_link")
        archive = Path(plan["targets"][0]["actions"][0]["archive_path"])
        real_rename = Path.rename

        def fail_restore(path, destination):
            if path == archive:
                raise OSError("native restore denied")
            return real_rename(path, destination)

        with patch(
            "skills_auditor.integration.os.symlink", side_effect=OSError("new link denied")
        ), patch.object(Path, "rename", fail_restore):
            self.assert_failed_apply(plan, "new link denied", "native restore denied")

        self.assertFalse(native.exists())
        self.assertFalse(native.is_symlink())
        self.assertTrue(archive.is_dir())
        self.assertFalse(archive.is_symlink())
        self.assertEqual((archive / "SKILL.md").read_bytes(), native_bytes)
        self.assertEqual((archive / "local-only.txt").read_text(encoding="utf-8"), "local data")
        self.assertEqual(list(self.target.iterdir()), [archive])

    def test_initial_archive_rename_failure_leaves_native_entry_untouched(self) -> None:
        native = self.write_skill(self.target, "native payload")
        native_bytes = (native / "SKILL.md").read_bytes()
        plan = self.build_plan("archive_and_link")
        archive = Path(plan["targets"][0]["actions"][0]["archive_path"])
        real_rename = Path.rename

        def fail_initial_rename(path, destination):
            if path == native and Path(destination) == archive:
                raise OSError("archive rename denied")
            return real_rename(path, destination)

        with patch.object(Path, "rename", fail_initial_rename), patch(
            "skills_auditor.integration.os.symlink"
        ) as create_link:
            self.assert_failed_apply(plan, "archive rename denied")
            create_link.assert_not_called()

        self.assertTrue(native.is_dir())
        self.assertFalse(native.is_symlink())
        self.assertEqual((native / "SKILL.md").read_bytes(), native_bytes)
        self.assertFalse(archive.exists())
        self.assertFalse(archive.is_symlink())
        self.assertEqual(list(self.target.iterdir()), [native])

    def test_replace_failure_preserves_concurrently_created_dangling_link(self) -> None:
        previous = self.write_skill(self.project / "previous", "previous payload")
        previous_bytes = (previous / "SKILL.md").read_bytes()
        self.entry.symlink_to("../previous/alpha", target_is_directory=True)
        plan = self.build_plan("replace_link")
        real_symlink = os.symlink
        concurrent_target = "../concurrent/missing"

        def create_concurrent_link_then_fail(source, destination, *args, **kwargs):
            real_symlink(concurrent_target, destination)
            raise FileExistsError("concurrent link appeared")

        with patch(
            "skills_auditor.integration.os.symlink",
            side_effect=create_concurrent_link_then_fail,
        ) as create_link:
            self.assert_failed_apply(plan, "concurrent link appeared")
            self.assertEqual(create_link.call_count, 1)

        self.assertFalse(self.entry.exists())
        self.assertTrue(self.entry.is_symlink())
        self.assertEqual(os.readlink(self.entry), concurrent_target)
        self.assertEqual((previous / "SKILL.md").read_bytes(), previous_bytes)
        self.assertEqual(list(self.target.iterdir()), [self.entry])

    def test_archive_failure_preserves_concurrent_directory_and_native_archive(self) -> None:
        native = self.write_skill(self.target, "native payload")
        native_bytes = (native / "SKILL.md").read_bytes()
        plan = self.build_plan("archive_and_link")
        archive = Path(plan["targets"][0]["actions"][0]["archive_path"])

        def create_concurrent_directory_then_fail(source, destination, *args, **kwargs):
            concurrent = Path(destination)
            concurrent.mkdir()
            (concurrent / "owner.txt").write_text("concurrent owner", encoding="utf-8")
            raise FileExistsError("concurrent directory appeared")

        with patch(
            "skills_auditor.integration.os.symlink",
            side_effect=create_concurrent_directory_then_fail,
        ):
            self.assert_failed_apply(plan, "concurrent directory appeared")

        self.assertTrue(self.entry.is_dir())
        self.assertFalse(self.entry.is_symlink())
        self.assertEqual(
            (self.entry / "owner.txt").read_text(encoding="utf-8"), "concurrent owner"
        )
        self.assertEqual(list(self.entry.iterdir()), [self.entry / "owner.txt"])
        self.assertTrue(archive.is_dir())
        self.assertFalse(archive.is_symlink())
        self.assertEqual((archive / "SKILL.md").read_bytes(), native_bytes)
        self.assertEqual(set(self.target.iterdir()), {self.entry, archive})


if __name__ == "__main__":
    unittest.main()
