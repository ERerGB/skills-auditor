"""S06/S07 sequence checks: payload roots, durable history and recovery entry.

The oracle observes independent SQLite facts; expected payload placement and
allowed recovery direction come from the test actions, not retention validators.
All process deaths and filesystem operations are confined to temporary fixtures.
"""

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

from lifecycle_model import ModelOracle, expected_transition, normalized_tree, tree_state
from skills_auditor.lifecycle.batch import BatchManager
from skills_auditor.lifecycle.common import LifecycleError
from skills_auditor.lifecycle.engine import Manager
from skills_auditor.lifecycle import retention


class TestLifecycleModelRetention(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="lifecycle-model-retention-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "candidate"
        self.source.mkdir()
        (self.source / "SKILL.md").write_text("# Model fixture\n", encoding="utf-8")
        (self.source / "payload").write_text("H1", encoding="utf-8")
        (self.source / "scripts").mkdir()
        (self.source / "scripts" / "run").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (self.source / "scripts" / "run").chmod(0o755)
        (self.source / "relative-payload").symlink_to("payload")
        self.source_expected = tree_state(self.source)
        self.snapshot_trees = {}
        self.source_payload = "H1"
        self.source_mode = stat.S_IMODE((self.source / "payload").stat().st_mode)
        self.host = self.root / "hosts"
        self.host.mkdir()
        self.target = self.host / "first"
        self.manager = Manager(self.root)
        self.addCleanup(lambda: self.manager.repository.close())
        self.oracle = ModelOracle(self, self.manager)
        self.oracle.watch_tree(self.source, self.source_expected)
        self.oracle.watch_pointer(self.target, None)
        self.oracle.check()

    def check(self):
        result = self.oracle.check()
        self.assertEqual((self.source / "payload").read_text(), self.source_payload)
        self.assertEqual((self.source / "SKILL.md").read_text(), "# Model fixture\n")
        self.assertEqual(stat.S_IMODE((self.source / "payload").stat().st_mode), self.source_mode)
        return result

    def candidate(self, payload):
        self.check()
        (self.source / "payload").write_text(payload, encoding="utf-8")
        self.source_payload = payload
        entry = self.source_expected["payload"]
        self.source_expected["payload"] = (entry[0], entry[1], payload.encode("utf-8"))
        self.oracle.watch_tree(self.source, self.source_expected)
        self.check()

    def reviewed_snapshot(self, plan):
        """Expected bytes originate in the fixture, never a read of the store."""
        snapshot = plan["version"]["snapshot"]
        tree_hash = snapshot["snapshot_tree_sha256"]
        if plan["source"]:
            expected = normalized_tree(self.source_expected)
            if tree_hash in self.snapshot_trees:
                self.assertEqual(self.snapshot_trees[tree_hash], expected)
            self.snapshot_trees[tree_hash] = expected
        return snapshot, self.snapshot_trees[tree_hash]

    def core_after(self, plan):
        snapshot, expected = self.reviewed_snapshot(plan)
        if plan["after"]["state"] == "active":
            self.oracle.watch_tree(snapshot["path"], expected)
        for step in plan["steps"]:
            self.oracle.watch_pointer(step["path"], step["after"].get("link"))

    def retention_after(self, plan):
        for obj in plan["objects"]:
            expected = self.snapshot_trees[obj["snapshot_tree_sha256"]]
            original = self.manager.store_root / obj["name"] / "tree"
            quarantine = Path(obj["quarantine_path"]) / "tree"
            if plan["operation"] == "restore":
                self.oracle.watch_tree(original, expected)
                self.oracle.watch_tree(quarantine, None)
            elif plan["operation"] == "collect":
                self.oracle.watch_tree(original, None)
                self.oracle.watch_tree(quarantine, expected)
            elif plan["operation"] == "purge":
                self.oracle.watch_tree(quarantine, None)

    def batch_after(self, plan):
        for child in plan["children"]:
            if child["kind"] == "apply":
                self.core_after(child["plan"])
            else:
                for step in child["plan"]["steps"]:
                    self.oracle.watch_pointer(step["path"], step["before"].get("link"))

    def core(self, operation, **arguments):
        records = self.check()["records"]
        previous = records.get("installation", {}).get(arguments.get("installation_id"))
        expected = expected_transition(previous, operation)
        plan = self.manager.plan(operation, **arguments)
        self.check()
        self.core_after(plan)
        receipt = self.manager.apply(plan, approve_plan_id=plan["plan_id"])
        actual = self.oracle.expect_installation(
            receipt["installation_id"], state=expected["state"],
            authorization=expected["authorization"], generation=expected["generation"],
            target=arguments.get("target") or previous["target"],
        )
        if previous:
            self.assertEqual(actual["installation_id"], previous["installation_id"])
            self.assertEqual(actual["skill_id"], previous["skill_id"])
            if expected["grant_policy"] == "new":
                self.assertNotEqual(actual["authorization"]["grant_id"], previous["authorization"]["grant_id"])
            else:
                self.assertEqual(actual["authorization"]["grant_id"], previous["authorization"]["grant_id"])
        self.check()
        return receipt

    def retain(self, operation, **arguments):
        self.check()
        plan = retention.plan_retention(self.manager, operation, **arguments)
        self.check()
        self.retention_after(plan)
        result = retention.apply_retention(self.manager, plan, approve_plan_id=plan["plan_id"],
                                           permanent_delete=operation == "purge")
        self.check()
        return result

    def collectable(self, expected):
        self.check()
        plan = retention.plan_retention(self.manager, "collect")
        self.assertEqual({obj["snapshot_tree_sha256"] for obj in plan["objects"]}, set(expected))
        self.check()
        return plan

    def snapshot(self, version_id):
        return self.manager.repository.get("version", version_id)["data"]["snapshot"]

    def installed(self, receipt, payload, target=None, state="active"):
        self.oracle.expect_installation(receipt["installation_id"], state=state,
                                        authorization="valid", grant_id=receipt["grant_id"],
                                        version_id=receipt["version_id"],
                                        target=target or self.target, payload=payload)
        self.check()

    def physical(self, obj, *, location, payload, identity=None):
        path = Path(obj["quarantine_path"]) if location == "quarantine" else self.manager.store_root / obj["name"]
        other = self.manager.store_root / obj["name"] if location == "quarantine" else Path(obj["quarantine_path"])
        self.assertFalse(os.path.lexists(other))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), obj["root_mode"])
        self.assertEqual((path / "tree" / "payload").read_text(), payload)
        self.assertFalse(stat.S_IMODE((path / "tree").stat().st_mode) & 0o222)
        self.assertFalse(stat.S_IMODE((path / "tree" / "payload").stat().st_mode) & 0o222)
        if identity is not None:
            self.assertEqual((path.stat().st_dev, path.stat().st_ino), identity)
        self.check()

    def crash(self, code, *arguments, boundary_expectation=None):
        self.check()
        self.manager.repository.close()
        result = subprocess.run([sys.executable, "-c", code, str(self.root), *arguments],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 71, result.stderr)
        self.assertEqual(result.stdout, "", "The lost response must not disclose its generated ID")
        if boundary_expectation:
            boundary_expectation()
        self.manager = Manager(self.root, create=False)
        self.check()

    def test_s07_shared_hash_roots_then_collect_restore_collect_purge_preserve_history(self):
        first = self.core("install", source=self.source, target=self.target)
        self.installed(first, "H1")
        second_target = self.host / "second"
        second = self.core("install-retained", version_id=first["version_id"], target=second_target)
        self.assertNotEqual(first["installation_id"], second["installation_id"])
        self.assertEqual(first["version_id"], second["version_id"])
        self.installed(second, "H1", second_target)
        old = self.snapshot(first["version_id"])
        old_root = Path(old["path"]).parent
        original_identity = (old_root.stat().st_dev, old_root.stat().st_ino)
        self.candidate("H2")
        updated = self.core("update", installation_id=first["installation_id"], source=self.source)
        self.installed(updated, "H2")
        self.installed(second, "H1", second_target)
        self.retain("policy", keep_recent=0)
        self.retain("expire", receipt_ids=[first["receipt_id"]])
        self.collectable([])
        h1_receipts = [second["receipt_id"]]
        for operation, state in (("disable", "disabled"), ("archive", "archived"), ("uninstall", "uninstalled")):
            second = self.core(operation, installation_id=second["installation_id"])
            h1_receipts.append(second["receipt_id"])
            self.installed(second, None, second_target, state=state)
            self.collectable([])
        self.retain("expire", receipt_ids=h1_receipts)
        plan = self.collectable([old["snapshot_tree_sha256"]])
        obj = plan["objects"][0]
        self.retention_after(plan)
        collected = retention.apply_retention(self.manager, plan, approve_plan_id=plan["plan_id"])
        self.assertEqual(collected["objects"], [obj])
        self.physical(obj, location="quarantine", payload="H1", identity=original_identity)
        self.installed(updated, "H2")
        self.retain("restore", object_ids=[obj["quarantine_id"]])
        self.physical(obj, location="store", payload="H1", identity=original_identity)
        self.collectable([old["snapshot_tree_sha256"]])
        recollected = self.retain("collect", object_ids=[old["snapshot_tree_sha256"]])
        next_obj = recollected["objects"][0]
        self.assertNotEqual(next_obj["quarantine_id"], obj["quarantine_id"])
        self.physical(next_obj, location="quarantine", payload="H1", identity=original_identity)
        self.retain("purge", object_ids=[next_obj["quarantine_id"]], grace_seconds=0)
        self.assertFalse(os.path.lexists(next_obj["quarantine_path"]))
        self.oracle.expect_snapshot(first["version_id"], present=False)
        self.installed(updated, "H2")
        self.collectable([])

    def test_s07_nested_inverse_history_remains_readable_to_retention_after_purge(self):
        batch = BatchManager(self.manager)
        plan = batch.plan([self.manager.plan("install", source=self.source, target=self.target)])
        completed = []
        self.batch_after(plan)
        receipt = batch.apply(plan, approve_plan_id=plan["plan_id"], batch_id="forward")
        completed.append(receipt)
        self.check()
        for original, inverse_id in (("forward", "inverse"), ("inverse", "inverse-of-inverse")):
            inverse = batch.plan_compensation(original)
            self.check()
            self.batch_after(inverse)
            receipt = batch.apply(inverse, approve_plan_id=inverse["plan_id"], batch_id=inverse_id)
            completed.append(receipt)
            self.check()
            for historical in completed:
                self.assertEqual(batch.inspect(historical["batch_id"])["receipt_id"], historical["receipt_id"])
            self.collectable([])
        current = next(row for row in self.manager.list_installations() if row["state"] == "active")
        self.core("uninstall", installation_id=current["installation_id"])
        self.retain("policy", keep_recent=0)
        receipt_ids = [row["id"] for row in self.manager.repository.list("receipt")]
        self.retain("expire", receipt_ids=receipt_ids)
        collected = self.retain("collect")
        self.assertEqual(len(collected["objects"]), 1)
        self.retain("purge", grace_seconds=0)
        for historical in completed:
            self.assertEqual(batch.inspect(historical["batch_id"])["receipt_id"], historical["receipt_id"])
        self.collectable([])

    def test_s07_pending_core_parent_and_inverse_roots_release_only_after_compensation(self):
        # A real child crash publishes H1, but cannot publish an installation or receipt.
        batch = BatchManager(self.manager)
        plan = batch.plan([self.manager.plan("install", source=self.source, target=self.target)])
        snapshot, expected_tree = self.reviewed_snapshot(plan["children"][0]["plan"])
        code = """
import json, os, sys
from skills_auditor.lifecycle.engine import Manager
from skills_auditor.lifecycle.batch import BatchManager
plan = json.loads(sys.argv[2])
def stop(name, value):
    if name == 'batch:child:0:transaction:staged': os._exit(71)
BatchManager(Manager(sys.argv[1])).apply(plan, approve_plan_id=plan['plan_id'], batch_id='pending-parent', checkpoint=stop)
"""
        self.crash(code, json.dumps(plan), boundary_expectation=lambda: self.oracle.watch_tree(snapshot["path"], expected_tree))
        batch = BatchManager(self.manager)
        original = batch.inspect("pending-parent")
        child_id = original["children"][0]["transaction_id"]
        self.oracle.expect_pending("transaction", child_id)
        self.oracle.expect_pending("batch", "pending-parent")
        self.assertEqual((Path(snapshot["path"]) / "payload").read_text(), "H1")
        collection = self.collectable([])
        self.assertIn("in-flight:" + child_id, collection["protected"][snapshot["snapshot_tree_sha256"]])
        self.assertIn("in-flight-batch:pending-parent", collection["protected"][snapshot["snapshot_tree_sha256"]])
        inverse = batch.plan_compensation("pending-parent")
        code = code.replace("batch:child:0:transaction:staged", "batch:prepared").replace("batch_id='pending-parent'", "batch_id='pending-inverse'")
        self.crash(code, json.dumps(inverse))
        self.candidate("H2")
        batch = BatchManager(self.manager)
        self.oracle.expect_pending("batch", "pending-inverse")
        collection = self.collectable([])
        self.assertIn("in-flight-batch:pending-inverse", collection["protected"][snapshot["snapshot_tree_sha256"]])
        self.assertFalse(os.path.lexists(self.target))
        self.assertEqual(self.manager.repository.list("receipt"), [])
        self.batch_after(inverse)
        batch.recover("pending-inverse", mode="resume", approve_plan_id=inverse["plan_id"])
        self.check()
        self.oracle.expect_pending("transaction", child_id, present=False)
        self.oracle.expect_pending("batch", "pending-parent", present=False)
        self.oracle.expect_pending("batch", "pending-inverse", present=False)
        self.assertEqual(self.manager.repository.list("receipt"), [])
        self.assertEqual(self.manager.repository.list("installation"), [])
        self.assertFalse(os.path.lexists(self.target))
        self.collectable([snapshot["snapshot_tree_sha256"]])

    def test_s07_parent_only_wal_protects_retained_content_before_child_creation(self):
        installed = self.core("install", source=self.source, target=self.target)
        removed = self.core("uninstall", installation_id=installed["installation_id"])
        self.retain("policy", keep_recent=0)
        self.retain("expire", receipt_ids=[installed["receipt_id"], removed["receipt_id"]])
        snapshot = self.snapshot(installed["version_id"])
        self.collectable([snapshot["snapshot_tree_sha256"]])
        batch = BatchManager(self.manager)
        plan = batch.plan([self.manager.plan("install-retained", version_id=installed["version_id"], target=self.target)])
        before = self.check()["records"]
        code = """
import json, os, sys
from skills_auditor.lifecycle.engine import Manager
from skills_auditor.lifecycle.batch import BatchManager
plan = json.loads(sys.argv[2])
def stop(name, value):
    if name == 'batch:prepared': os._exit(71)
BatchManager(Manager(sys.argv[1])).apply(plan, approve_plan_id=plan['plan_id'], checkpoint=stop)
"""
        self.crash(code, json.dumps(plan))
        pending = [row for row in self.oracle.pending() if row["kind"] == "batch"]
        self.assertEqual(len(pending), 1)
        parent_id = pending[0]["id"]
        after = self.check()["records"]
        self.assertEqual(after["transaction"], before["transaction"])
        self.assertEqual(after["receipt"], before["receipt"])
        self.assertEqual(after["installation"], before["installation"])
        collection = self.collectable([])
        self.assertIn("in-flight-batch:" + parent_id, collection["protected"][snapshot["snapshot_tree_sha256"]])
        self.assertFalse(os.path.lexists(self.target))
        batch = BatchManager(self.manager)
        inverse = batch.plan_compensation(parent_id)
        self.assertEqual(inverse["children"], [])
        self.batch_after(inverse)
        batch.apply(inverse, approve_plan_id=inverse["plan_id"])
        self.check()
        self.assertEqual(self.oracle.pending(), [])
        self.assertEqual(self.check()["records"]["receipt"], before["receipt"])
        self.collectable([snapshot["snapshot_tree_sha256"]])

    def test_s07_partial_purge_keeps_exact_cursor_and_preserves_foreign_entry(self):
        installed = self.core("install", source=self.source, target=self.target)
        removed = self.core("uninstall", installation_id=installed["installation_id"])
        self.retain("policy", keep_recent=0)
        self.retain("expire", receipt_ids=[installed["receipt_id"], removed["receipt_id"]])
        collected = self.retain("collect")
        obj = collected["objects"][0]
        quarantine = Path(obj["quarantine_path"])
        plan = retention.plan_retention(self.manager, "purge", object_ids=[obj["quarantine_id"]], grace_seconds=0)
        before_receipts = self.check()["records"]["retention-receipt"]
        code = """
import json, os, sys
from skills_auditor.lifecycle.engine import Manager
from skills_auditor.lifecycle.retention import apply_retention
plan = json.loads(sys.argv[2])
def stop(name, value):
    if name == 'retention:0:delete_effect': os._exit(71)
apply_retention(Manager(sys.argv[1]), plan, approve_plan_id=plan['plan_id'], permanent_delete=True, checkpoint=stop)
"""
        self.crash(code, json.dumps(plan))
        pending = [row for row in self.oracle.pending() if row["kind"] == "retention-transaction"]
        self.assertEqual(len(pending), 1)
        transaction_id = pending[0]["id"]
        tx = retention.recover_retention(self.manager, transaction_id)
        step = tx["objects"][0]
        self.assertIsNone(tx["receipt_id"])
        self.assertEqual(step["deleted"], [])
        self.assertEqual(step["pending_delete"], obj["inventory"][0]["path"])
        self.assertEqual(step["pending_delete"], "manifest.json", "This boundary precedes deletion of any snapshot tree entry")
        self.assertFalse(os.path.lexists(quarantine / step["pending_delete"]))
        self.assertEqual(self.check()["records"]["retention-object"][obj["quarantine_id"]]["state"], "purging")
        self.assertEqual(self.check()["records"]["retention-receipt"], before_receipts)
        with self.assertRaises(LifecycleError) as not_approved:
            retention.recover_retention(self.manager, transaction_id, mode="resume", approve_plan_id=plan["plan_id"])
        self.assertEqual(not_approved.exception.code, "retention_permanent_delete_required")
        self.assertEqual(retention.recover_retention(self.manager, transaction_id), tx)
        foreign = quarantine / "external-note"
        quarantine.chmod(stat.S_IMODE(quarantine.stat().st_mode) | 0o700)
        foreign.write_text("external owner", encoding="utf-8")
        foreign_identity = foreign.stat().st_ino
        with self.assertRaises(LifecycleError) as conflict:
            retention.recover_retention(self.manager, transaction_id, mode="resume", approve_plan_id=plan["plan_id"], permanent_delete=True)
        self.assertEqual(conflict.exception.code, "retention_incomplete")
        self.assertEqual(foreign.read_text(), "external owner")
        self.assertEqual(foreign.stat().st_ino, foreign_identity)
        self.assertEqual(self.check()["records"]["retention-receipt"], before_receipts)
        self.assertIsNone(retention.recover_retention(self.manager, transaction_id)["receipt_id"])
        # The fixture's external owner resolves its own conflict; recovery itself
        # must not remove the foreign entry to manufacture a successful purge.
        foreign.unlink()
        self.retention_after(plan)
        result = retention.recover_retention(self.manager, transaction_id, mode="resume", approve_plan_id=plan["plan_id"], permanent_delete=True)
        self.assertTrue(result["permanently_deleted"])
        self.assertFalse(os.path.lexists(quarantine))
        self.assertFalse(os.path.lexists(self.manager.store_root / obj["name"]))
        self.assertEqual(len(self.check()["records"]["retention-receipt"]), len(before_receipts) + 1)
        self.assertEqual(self.check()["records"]["retention-object"][obj["quarantine_id"]]["state"], "purged")
        self.assertEqual(self.oracle.pending(), [])
        self.collectable([])

    def test_s06_lost_retention_id_is_named_by_new_plan_and_default_or_different_apply(self):
        installed = self.core("install", source=self.source, target=self.target)
        removed = self.core("uninstall", installation_id=installed["installation_id"])
        self.retain("policy", keep_recent=0)
        self.retain("expire", receipt_ids=[installed["receipt_id"], removed["receipt_id"]])
        plan = self.collectable([self.snapshot(installed["version_id"])["snapshot_tree_sha256"]])
        obj = plan["objects"][0]
        before_receipts = self.check()["records"]["retention-receipt"]
        code = """
import json, os, sys
from skills_auditor.lifecycle.engine import Manager
from skills_auditor.lifecycle.retention import apply_retention
plan = json.loads(sys.argv[2])
def stop(name, value):
    if name == 'retention:0:effect': os._exit(71)
apply_retention(Manager(sys.argv[1]), plan, approve_plan_id=plan['plan_id'], checkpoint=stop)
"""
        self.crash(code, json.dumps(plan), boundary_expectation=lambda: self.retention_after(plan))
        pending = [row for row in self.oracle.pending() if row["kind"] == "retention-transaction"]
        self.assertEqual(len(pending), 1)
        original_id = pending[0]["id"]
        self.physical(obj, location="quarantine", payload="H1")
        original = retention.recover_retention(self.manager, original_id)
        self.assertIsNone(original["receipt_id"])
        self.assertEqual(self.check()["records"]["retention-receipt"], before_receipts)
        actions = (
            ("new-plan", lambda: retention.plan_retention(self.manager, "collect")),
            ("default-id", lambda: retention.apply_retention(self.manager, plan, approve_plan_id=plan["plan_id"])),
            ("different-id", lambda: retention.apply_retention(self.manager, plan, approve_plan_id=plan["plan_id"], transaction_id="different-attempt")),
        )
        for name, action in actions:
            with self.subTest(attempt=name):
                self.check()
                with self.assertRaises(LifecycleError) as blocked:
                    action()
                self.assertEqual(blocked.exception.code, "retention_recovery_required")
                self.assertEqual(retention.recover_retention(self.manager, original_id), original)
                self.assertEqual([row["id"] for row in self.oracle.pending() if row["kind"] == "retention-transaction"], [original_id])
                self.assertEqual(self.check()["records"]["retention-receipt"], before_receipts)
                self.physical(obj, location="quarantine", payload="H1")
                self.assertEqual(blocked.exception.details.get("transaction_id"), original_id)
        result = retention.recover_retention(self.manager, original_id, mode="resume", approve_plan_id=plan["plan_id"])
        self.assertEqual(result["transaction_id"], original_id)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(self.check()["records"]["retention-receipt"]), len(before_receipts) + 1)
        self.oracle.expect_pending("retention-transaction", original_id, present=False)
        self.physical(obj, location="quarantine", payload="H1")
        self.retain("restore", object_ids=[obj["quarantine_id"]])
        self.physical(obj, location="store", payload="H1")


if __name__ == "__main__":
    unittest.main()
