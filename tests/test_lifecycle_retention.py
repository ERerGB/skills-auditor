"""Retention changes payload availability, never erases approval history."""

import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import selectors
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import jsonschema

from skills_auditor.lifecycle.common import LifecycleError, digest
from skills_auditor.lifecycle.engine import Manager
from skills_auditor.lifecycle import retention
from skills_auditor.lifecycle.snapshots import inspect_source, materialize


class RetentionFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="skills-retention-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "SKILL.md").write_text("# H1")
        self.host = self.root / "host"
        self.host.mkdir()
        self.target = self.host / "skill"
        self.manager = Manager(self.root)
        self.addCleanup(self.manager.repository.close)

    def apply_plan(self, plan, **kwargs):
        return retention.apply_retention(self.manager, plan, approve_plan_id=plan["plan_id"], **kwargs)

    def install(self):
        plan = self.manager.plan("install", source=self.source, target=self.target)
        receipt = self.manager.apply(plan, approve_plan_id=plan["plan_id"])
        return receipt

    def operate(self, operation, receipt, **kwargs):
        plan = self.manager.plan(operation, installation_id=receipt["installation_id"], **kwargs)
        return self.manager.apply(plan, approve_plan_id=plan["plan_id"])

    def unreferenced(self):
        inspected = inspect_source(self.source)
        return materialize(self.source, self.manager.store_root,
                           inspected["source_tree_sha256"], inspected["snapshot_tree_sha256"])

    def policy(self, **kwargs):
        return self.apply_plan(retention.plan_retention(self.manager, "policy", **kwargs))

    def quarantine(self):
        descriptor = self.unreferenced()
        plan = retention.plan_retention(self.manager, "collect")
        result = self.apply_plan(plan)
        return descriptor, plan, result


class TestLifecycleRetention(RetentionFixture):
    def test_plan_is_read_only_and_exact_explicit_approval_is_required(self):
        descriptor = self.unreferenced()
        source_before = inspect_source(self.source)
        tree = Path(descriptor["path"])
        before = tree.stat().st_ino
        plan = retention.plan_retention(self.manager, "collect")
        self.assertEqual(len(plan["objects"]), 1)
        self.assertEqual(tree.stat().st_ino, before)
        self.assertFalse((self.manager.store_root / ".quarantine").exists())
        with self.assertRaises(LifecycleError):
            retention.apply_retention(self.manager, plan, approve_plan_id="not-approved")
        self.assertTrue(tree.is_dir())
        receipt = self.apply_plan(plan)
        self.assertEqual(receipt["status"], "completed")
        self.assertFalse(tree.exists())
        self.assertEqual(inspect_source(self.source), source_before)
        self.assertEqual(receipt["permanently_deleted"], False)
        self.assertEqual(len(receipt["objects"]), 1)
        self.assertTrue(Path(receipt["objects"][0]["quarantine_path"]).is_dir())

    def test_active_disabled_archived_and_receipt_references_protect_payload(self):
        receipt = self.install()
        for state in ("active", "disabled", "archived"):
            if state != "active":
                receipt = self.operate("disable" if state == "disabled" else "archive", receipt)
            plan = retention.plan_retention(self.manager, "collect")
            self.assertEqual(plan["objects"], [])
            self.assertTrue(plan["protected"])
            with self.assertRaises(LifecycleError):
                retention.plan_retention(self.manager, "expire", receipt_ids=[receipt["receipt_id"]])
        self.assertTrue(Path(os.readlink(self.target)).exists() if self.target.is_symlink()
                        else self.manager.repository.get("version", receipt["version_id"]) is not None)

    def test_history_expiry_and_rollback_policy_are_separate_reviewed_changes(self):
        first = self.install()
        (self.source / "SKILL.md").write_text("# H2")
        second = self.operate("update", first, source=self.source)
        expired = retention.plan_retention(self.manager, "expire", receipt_ids=[first["receipt_id"]])
        original = copy.deepcopy(self.manager.repository.get("receipt", first["receipt_id"]))
        self.apply_plan(expired)
        self.assertEqual(self.manager.repository.get("receipt", first["receipt_id"]), original)
        self.assertEqual(retention.plan_retention(self.manager, "collect")["objects"], [])
        self.policy(keep_recent=0, pin_version_ids=[first["version_id"]])
        self.assertEqual(retention.plan_retention(self.manager, "collect")["objects"], [])
        self.policy(keep_recent=0)
        self.assertEqual(retention.plan_retention(self.manager, "collect")["objects"], [])
        self.policy(pin_version_ids=[])
        collect = retention.plan_retention(self.manager, "collect")
        self.assertEqual(len(collect["objects"]), 1)
        self.apply_plan(collect)
        self.assertEqual(self.target.resolve(), Path(self.manager.repository.get("version", second["version_id"])["data"]["snapshot"]["path"]))
        self.assertTrue(self.manager.verify(second["installation_id"])["valid"])

    def test_uninstalled_payload_can_be_collected_without_erasing_metadata(self):
        first = self.install()
        last = self.operate("uninstall", first)
        self.policy(keep_recent=0)
        self.apply_plan(retention.plan_retention(self.manager, "expire", receipt_ids=[first["receipt_id"], last["receipt_id"]]))
        plan = retention.plan_retention(self.manager, "collect")
        self.assertEqual(len(plan["objects"]), 1)
        self.apply_plan(plan)
        self.assertEqual(self.manager.get_installation(first["installation_id"])["state"], "uninstalled")
        self.assertEqual(len(self.manager.repository.list("receipt")), 2)
        self.assertEqual(len(self.manager.repository.list("transaction")), 2)

    def test_new_reference_and_rehashed_impossible_plan_reject_before_effects(self):
        descriptor = self.unreferenced()
        stale = retention.plan_retention(self.manager, "collect")
        self.install()
        with self.assertRaises(LifecycleError) as caught:
            self.apply_plan(stale)
        self.assertEqual(caught.exception.code, "retention_stale_plan")
        self.assertTrue(Path(descriptor["path"]).is_dir())
        forged = copy.deepcopy(stale)
        forged["objects"][0]["name"] = "../outside"
        forged["plan_id"] = digest({key: value for key, value in forged.items() if key != "plan_id"})
        with self.assertRaises(LifecycleError):
            self.apply_plan(forged)
        self.assertTrue(self.target.is_symlink())

    def test_missing_or_corrupt_reference_fails_closed(self):
        receipt = self.install()
        original_get = self.manager.repository.list
        with patch.object(self.manager.repository, "list", side_effect=lambda kind: [] if kind == "version" else original_get(kind)):
            with self.assertRaises(LifecycleError) as caught:
                retention.plan_retention(self.manager, "collect")
        self.assertEqual(caught.exception.code, "retention_references_invalid")
        self.assertTrue(self.target.is_symlink())

    def test_incident_and_agent_note_refs_survive_receipt_expiry_until_explicit_resolution_expiry(self):
        from skills_auditor.lifecycle import incidents
        first = self.install()
        link = os.readlink(self.target)
        self.target.unlink()
        failure = self.manager.verify(first["installation_id"])
        incidents.record_verification(self.manager.repository, self.manager.get_installation(first["installation_id"]), failure)
        incident = incidents.list_incidents(self.manager)[0]
        with self.assertRaises(LifecycleError):
            retention.plan_retention(self.manager, "expire", incident_ids=[incident["incident_id"]])
        self.target.symlink_to(link)
        (self.source / "SKILL.md").write_text("# H2")
        second = self.operate("update", first, source=self.source)
        incidents.append_note(self.manager, incident["incident_id"], "Keep old evidence", actor="reviewer", tool="test",
                              evidence_refs=[{"kind": "receipt", "id": first["receipt_id"]},
                                             {"kind": "version", "id": first["version_id"]}])
        self.policy(keep_recent=0)
        self.apply_plan(retention.plan_retention(self.manager, "expire", receipt_ids=[first["receipt_id"]]))
        self.assertEqual(retention.plan_retention(self.manager, "collect")["objects"], [])
        incidents.resolve(self.manager, incident["incident_id"], actor="reviewer", tool="test", disposition="accepted_history", explanation="Preserve metadata; no semantic safety claim.")
        self.assertEqual(retention.plan_retention(self.manager, "collect")["objects"], [])
        self.apply_plan(retention.plan_retention(self.manager, "expire", incident_ids=[incident["incident_id"]]))
        collection = retention.plan_retention(self.manager, "collect")
        self.assertEqual(len(collection["objects"]), 1)
        self.apply_plan(collection)
        self.assertTrue(incidents.investigate(self.manager, incident["incident_id"]))
        self.assertTrue(self.manager.verify(second["installation_id"])["valid"])

    def test_inflight_transaction_protects_snapshot_until_compensated(self):
        plan = self.manager.plan("install", source=self.source, target=self.target)
        def fail(name, transaction):
            if name == "transaction:staged":
                raise OSError("pause before pointer activation")
        with self.assertRaises(LifecycleError):
            self.manager.apply(plan, approve_plan_id=plan["plan_id"], transaction_id="unfinished-install", checkpoint=fail)
        collection = retention.plan_retention(self.manager, "collect")
        self.assertEqual(collection["objects"], [])
        self.assertIn("in-flight:unfinished-install", str(collection["protected"]))
        self.manager.recover("unfinished-install", mode="compensate", approve_plan_id=plan["plan_id"])
        self.assertEqual(len(retention.plan_retention(self.manager, "collect")["objects"]), 1)

    def test_crashed_batch_parent_protects_payload_before_any_child_transaction(self):
        from skills_auditor.lifecycle.batch import BatchManager
        descriptor = self.unreferenced()
        before = inspect_source(self.source)
        stale = retention.plan_retention(self.manager, "collect")
        batch = BatchManager(self.manager)
        plan = batch.plan([self.manager.plan("install", source=self.source, target=self.target)])
        code = """
import json, os, sys
from pathlib import Path
from skills_auditor.lifecycle.batch import BatchManager
from skills_auditor.lifecycle.engine import Manager
plan = json.loads(sys.argv[2])
def crash(name, value):
    if name == 'batch:prepared':
        os._exit(71)
BatchManager(Manager(Path(sys.argv[1]))).apply(plan, approve_plan_id=plan['plan_id'], batch_id='parent-only', checkpoint=crash)
"""
        result = subprocess.run([sys.executable, "-c", code, str(self.root), json.dumps(plan)], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 71, result.stderr)
        self.assertEqual(batch.inspect("parent-only")["state"], "prepared")
        self.assertEqual(self.manager.repository.list("transaction"), [])
        collection = retention.plan_retention(self.manager, "collect")
        self.assertEqual(collection["objects"], [])
        self.assertIn("in-flight-batch:parent-only", collection["protected"][descriptor["snapshot_tree_sha256"]])
        with self.assertRaises(LifecycleError):
            self.apply_plan(stale)
        self.assertTrue(Path(descriptor["path"]).is_dir())
        inverse = batch.plan_compensation("parent-only")
        batch.apply(inverse, approve_plan_id=inverse["plan_id"])
        self.assertEqual(batch.inspect("parent-only")["state"], "compensated")
        self.assertEqual(len(retention.plan_retention(self.manager, "collect")["objects"]), 1)
        self.assertEqual(inspect_source(self.source), before)
        self.assertFalse(self.target.is_symlink())
        self.assertEqual(self.manager.repository.list("receipt"), [])

    def test_inflight_batch_protects_before_and_candidate_until_real_compensation(self):
        from skills_auditor.lifecycle.batch import BatchManager
        first = self.install()
        old = self.manager.repository.get("version", first["version_id"])["data"]["snapshot"]
        (self.source / "SKILL.md").write_text("# H2")
        candidate = self.unreferenced()
        batch = BatchManager(self.manager)
        plan = batch.plan([self.manager.plan("update", installation_id=first["installation_id"], source=self.source)])
        def fail(name, value):
            if name == "batch:child:0:started":
                raise OSError("stop after parent running marker, before child intent")
        with self.assertRaises(LifecycleError):
            batch.apply(plan, approve_plan_id=plan["plan_id"], batch_id="update-pending", checkpoint=fail)
        self.assertEqual(len(self.manager.repository.list("transaction")), 1)
        collection = retention.plan_retention(self.manager, "collect")
        self.assertEqual(collection["objects"], [])
        for snapshot in (old, candidate):
            self.assertIn("in-flight-batch:update-pending", collection["protected"][snapshot["snapshot_tree_sha256"]])
        inverse = batch.plan_compensation("update-pending")
        batch.apply(inverse, approve_plan_id=inverse["plan_id"])
        collect = retention.plan_retention(self.manager, "collect")
        self.assertEqual([obj["snapshot_tree_sha256"] for obj in collect["objects"]], [candidate["snapshot_tree_sha256"]])
        self.assertTrue(self.manager.verify(first["installation_id"])["valid"])

    def test_false_terminal_batch_cannot_release_references_without_completion_proof(self):
        from skills_auditor.lifecycle.batch import BatchManager
        descriptor = self.unreferenced()
        batch = BatchManager(self.manager)
        plan = batch.plan([self.manager.plan("install", source=self.source, target=self.target)])
        def fail(name, value):
            if name == "batch:prepared":
                raise OSError("stop before child")
        with self.assertRaises(LifecycleError):
            batch.apply(plan, approve_plan_id=plan["plan_id"], batch_id="terminal-forgery", checkpoint=fail)
        original = self.manager.repository.get("batch", "terminal-forgery")["data"]
        for state in ("completed", "compensated"):
            with self.subTest(state=state):
                forged = copy.deepcopy(original)
                forged["state"] = state
                if state == "completed":
                    forged["receipt_id"] = "missing-batch-receipt"
                    forged["children"][0].update(state="completed", receipt_id="missing-child-receipt")
                else:
                    forged["compensation_batch_id"] = "missing-inverse"
                self.manager.repository.put("batch", "terminal-forgery", forged,
                                            expected_revision=self.manager.repository.get("batch", "terminal-forgery")["revision"])
                with self.assertRaises(LifecycleError):
                    retention.plan_retention(self.manager, "collect")
                self.assertTrue(Path(descriptor["path"]).is_dir())
        self.manager.repository.put("batch", "terminal-forgery", original,
                                    expected_revision=self.manager.repository.get("batch", "terminal-forgery")["revision"])
        inverse = batch.plan_compensation("terminal-forgery")
        receipt = batch.apply(inverse, approve_plan_id=inverse["plan_id"])
        real_get = self.manager.repository.get
        with patch.object(self.manager.repository, "get", side_effect=lambda kind, key: None if kind == "batch-receipt" and key == receipt["receipt_id"] else real_get(kind, key)):
            with self.assertRaises(LifecycleError):
                retention.plan_retention(self.manager, "collect")
        self.assertEqual(len(retention.plan_retention(self.manager, "collect")["objects"]), 1)

    def test_inverse_and_original_batch_hold_unfinished_child_until_compensation_finishes(self):
        from skills_auditor.lifecycle.batch import BatchManager
        batch = BatchManager(self.manager)
        plan = batch.plan([self.manager.plan("install", source=self.source, target=self.target)])
        def fail_child(name, value):
            if name == "batch:child:0:transaction:staged":
                raise OSError("child snapshot ready, activation pending")
        with self.assertRaises(LifecycleError):
            batch.apply(plan, approve_plan_id=plan["plan_id"], batch_id="original-batch", checkpoint=fail_child)
        inverse = batch.plan_compensation("original-batch")
        self.assertEqual(inverse["children"][0]["kind"], "compensate")
        def fail_inverse(name, value):
            if name == "batch:prepared":
                raise OSError("inverse owns original, no compensation effect yet")
        with self.assertRaises(LifecycleError):
            batch.apply(inverse, approve_plan_id=inverse["plan_id"], batch_id="inverse-batch", checkpoint=fail_inverse)
        (self.source / "SKILL.md").write_text("# Source later changed, not used for historical GC proof")
        collection = retention.plan_retention(self.manager, "collect")
        self.assertEqual(collection["objects"], [])
        tree_hash = plan["children"][0]["plan"]["version"]["snapshot"]["snapshot_tree_sha256"]
        self.assertIn("in-flight-batch:original-batch", collection["protected"][tree_hash])
        self.assertIn("in-flight-batch:inverse-batch", collection["protected"][tree_hash])
        batch.recover("inverse-batch", mode="resume", approve_plan_id=inverse["plan_id"])
        self.assertEqual(batch.inspect("original-batch")["state"], "compensated")
        self.assertEqual([obj["snapshot_tree_sha256"] for obj in retention.plan_retention(self.manager, "collect")["objects"]], [tree_hash])
        self.assertEqual(self.manager.repository.list("receipt"), [])
        self.assertFalse(self.target.is_symlink())

    def test_completed_batch_history_survives_explicit_payload_expiry_and_purge(self):
        from skills_auditor.lifecycle.batch import BatchManager
        batch = BatchManager(self.manager)
        plan = batch.plan([self.manager.plan("install", source=self.source, target=self.target)])
        batch_receipt = batch.apply(plan, approve_plan_id=plan["plan_id"], batch_id="historical-batch")
        first = self.manager.repository.get("receipt", batch_receipt["children"][0]["receipt_id"])["data"]
        last = self.operate("uninstall", first)
        self.policy(keep_recent=0)
        self.apply_plan(retention.plan_retention(self.manager, "expire", receipt_ids=[first["receipt_id"], last["receipt_id"]]))
        collected = self.apply_plan(retention.plan_retention(self.manager, "collect"))
        self.assertEqual(len(collected["objects"]), 1)
        self.apply_plan(retention.plan_retention(self.manager, "purge", grace_seconds=0), permanent_delete=True)
        self.assertEqual(batch.inspect("historical-batch")["state"], "completed")
        self.assertEqual(retention.plan_retention(self.manager, "collect")["objects"], [])
        self.assertEqual(len(self.manager.repository.list("batch-receipt")), 1)
        self.assertEqual(len(self.manager.repository.list("receipt")), 2)

    def test_reference_added_at_effect_boundary_prevents_collection(self):
        descriptor = self.unreferenced()
        plan = retention.plan_retention(self.manager, "collect")
        def retain(name, transaction):
            if name == "retention:0:intent":
                fake_plan = self.manager.plan("install", source=self.source, target=self.target)
                self.manager.repository.put("transaction", "new-owner", {"transaction_id": "new-owner", "state": "prepared", "plan": fake_plan})
        with self.assertRaises(LifecycleError):
            self.apply_plan(plan, transaction_id="boundary-ref", checkpoint=retain)
        self.assertTrue(Path(descriptor["path"]).is_dir())
        self.assertIsNone(retention.recover_retention(self.manager, "boundary-ref")["receipt_id"])

    def test_object_modified_after_plan_and_final_effect_cannot_publish_false_success(self):
        descriptor = self.unreferenced()
        plan = retention.plan_retention(self.manager, "collect")
        payload = Path(descriptor["path"]) / "SKILL.md"
        payload.chmod(0o644)
        payload.write_text("changed")
        with self.assertRaises(LifecycleError):
            self.apply_plan(plan)
        payload.write_text("# H1"); payload.chmod(0o444)
        plan = retention.plan_retention(self.manager, "collect")
        def tamper(name, transaction):
            if name == "retention:0:completed":
                path = Path(transaction["objects"][0]["object"]["quarantine_path"]) / "tree" / "SKILL.md"
                path.chmod(0o644); path.write_text("foreign bytes")
        with self.assertRaises(LifecycleError):
            self.apply_plan(plan, transaction_id="final-tamper", checkpoint=tamper)
        self.assertIsNone(retention.recover_retention(self.manager, "final-tamper")["receipt_id"])

    def test_collect_failure_after_rename_is_recoverable_without_false_receipt(self):
        descriptor = self.unreferenced()
        plan = retention.plan_retention(self.manager, "collect")

        def fail(name, tx):
            if name == "retention:0:effect":
                raise OSError("interrupted after quarantine rename")

        with self.assertRaises(LifecycleError):
            self.apply_plan(plan, transaction_id="collect-failed", checkpoint=fail)
        tx = retention.recover_retention(self.manager, "collect-failed")
        self.assertEqual(tx["state"], "recovery_needed")
        self.assertIsNone(tx["receipt_id"])
        self.assertFalse(Path(descriptor["path"]).exists())
        result = retention.recover_retention(self.manager, "collect-failed", mode="resume", approve_plan_id=plan["plan_id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.apply_plan(plan, transaction_id="collect-failed"), result)
        restore = retention.plan_retention(self.manager, "restore", object_ids=[result["objects"][0]["quarantine_id"]])
        self.apply_plan(restore)
        self.assertTrue(Path(descriptor["path"]).is_dir())

    def test_restore_refuses_foreign_occupancy(self):
        descriptor, _, receipt = self.quarantine()
        restore = retention.plan_retention(self.manager, "restore", object_ids=[receipt["objects"][0]["quarantine_id"]])
        original = Path(descriptor["path"]).parent
        original.mkdir()
        (original / "foreign").write_text("keep")
        with self.assertRaises(LifecycleError):
            self.apply_plan(restore)
        self.assertEqual((original / "foreign").read_text(), "keep")
        self.assertTrue(Path(receipt["objects"][0]["quarantine_path"]).exists())

    def test_purge_requires_grace_separate_approval_and_explicit_permanent_delete(self):
        descriptor, _, receipt = self.quarantine()
        qid = receipt["objects"][0]["quarantine_id"]
        with self.assertRaises(LifecycleError):
            retention.plan_retention(self.manager, "purge", object_ids=[qid])
        plan = retention.plan_retention(self.manager, "purge", object_ids=[qid], grace_seconds=0)
        with self.assertRaises(LifecycleError):
            self.apply_plan(plan)
        result = self.apply_plan(plan, permanent_delete=True)
        self.assertTrue(result["permanently_deleted"])
        self.assertFalse(Path(receipt["objects"][0]["quarantine_path"]).exists())
        self.assertEqual(self.manager.repository.get("retention-object", qid)["data"]["state"], "purged")
        with self.assertRaises(LifecycleError):
            retention.plan_retention(self.manager, "restore", object_ids=[qid])
        self.assertTrue(self.source.is_dir())

    def test_partial_purge_retains_tombstone_and_resumes_exact_remaining_entries(self):
        descriptor, _, collected = self.quarantine()
        qid = collected["objects"][0]["quarantine_id"]
        plan = retention.plan_retention(self.manager, "purge", object_ids=[qid], grace_seconds=0)
        original = retention._delete_entry
        calls = []

        def fail(*args, **kwargs):
            calls.append(args)
            if len(calls) == 2:
                raise OSError("injected second unlink failure")
            return original(*args, **kwargs)

        with patch.object(retention, "_delete_entry", side_effect=fail):
            with self.assertRaises(LifecycleError):
                self.apply_plan(plan, transaction_id="partial-purge", permanent_delete=True)
        state = self.manager.repository.get("retention-object", qid)["data"]
        self.assertEqual(state["state"], "purging")
        tx = retention.recover_retention(self.manager, "partial-purge")
        self.assertIsNone(tx["receipt_id"])
        self.assertTrue(tx["objects"][0]["deleted"])
        with self.assertRaises(LifecycleError):
            retention.recover_retention(self.manager, "partial-purge", mode="resume", approve_plan_id=plan["plan_id"])
        result = retention.recover_retention(self.manager, "partial-purge", mode="resume", approve_plan_id=plan["plan_id"], permanent_delete=True)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.manager.repository.get("retention-object", qid)["data"]["state"], "purged")
        self.assertTrue(self.source.is_dir())

    def test_foreign_store_entries_and_quarantine_symlinks_are_rejected(self):
        descriptor = self.unreferenced()
        foreign = self.manager.store_root / "foreign"
        foreign.write_text("keep")
        with self.assertRaises(LifecycleError):
            retention.plan_retention(self.manager, "collect")
        foreign.unlink()
        quarantine = self.manager.store_root / ".quarantine"
        quarantine.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(LifecycleError):
            retention.plan_retention(self.manager, "collect")
        self.assertTrue((self.source / "SKILL.md").is_file())
        self.assertTrue(Path(descriptor["path"]).is_dir())

    def test_unknown_and_uninitialized_stages_are_preserved_not_assumed_orphans(self):
        self.manager.store_root.mkdir(parents=True)
        stage = self.manager.store_root / (".stage-" + "a" * 64 + "-unknownx")
        stage.mkdir()
        (stage / "private.tmp").write_text("uncommitted intent")
        planned = retention.plan_retention(self.manager, "collect")
        self.assertEqual(planned["objects"], [])
        with self.assertRaises((LifecycleError, OSError)):
            retention.plan_retention(self.manager, "collect", stage_names=[stage.name])
        self.assertEqual((stage / "private.tmp").read_text(), "uncommitted intent")

    def test_purge_does_not_follow_internal_symlinks_or_erase_external_source(self):
        (self.source / "shortcut").symlink_to("SKILL.md")
        descriptor, _, collected = self.quarantine()
        plan = retention.plan_retention(self.manager, "purge", object_ids=[collected["objects"][0]["quarantine_id"]], grace_seconds=0)
        self.apply_plan(plan, permanent_delete=True)
        self.assertEqual((self.source / "shortcut").read_text(), "# H1")
        self.assertTrue((self.source / "SKILL.md").is_file())

    def test_journal_and_receipt_failures_preserve_correct_before_and_after_states(self):
        descriptor = self.unreferenced()
        plan = retention.plan_retention(self.manager, "collect")
        real = self.manager.repository.put
        def fail_intent(kind, *args, **kwargs):
            if kind == "retention-transaction":
                raise OSError("journal unavailable")
            return real(kind, *args, **kwargs)
        with patch.object(self.manager.repository, "put", side_effect=fail_intent):
            with self.assertRaises(LifecycleError) as caught:
                self.apply_plan(plan)
        self.assertEqual(caught.exception.code, "retention_journal_failed")
        self.assertTrue(Path(descriptor["path"]).exists())
        def fail_receipt(kind, *args, **kwargs):
            if kind == "retention-receipt":
                raise OSError("receipt unavailable")
            return real(kind, *args, **kwargs)
        with patch.object(self.manager.repository, "put", side_effect=fail_receipt):
            with self.assertRaises(LifecycleError):
                self.apply_plan(plan, transaction_id="no-receipt")
        self.assertFalse(Path(descriptor["path"]).exists())
        self.assertEqual(self.manager.repository.list("retention-receipt"), [])
        self.assertIsNone(retention.recover_retention(self.manager, "no-receipt")["receipt_id"])
        result = retention.recover_retention(self.manager, "no-receipt", mode="resume", approve_plan_id=plan["plan_id"])
        self.assertEqual(result["status"], "completed")

    def test_post_effect_error_and_failed_failure_journal_remain_recoverable(self):
        descriptor = self.unreferenced()
        source_before = inspect_source(self.source)
        plan = retention.plan_retention(self.manager, "collect")
        real_put = self.manager.repository.put
        def fail_journal(kind, identifier, data, **kwargs):
            if kind == "retention-transaction" and data["state"] == "recovery_needed":
                raise OSError("failure journal unavailable")
            return real_put(kind, identifier, data, **kwargs)
        def fail_effect(name, value):
            if name == "retention:0:effect":
                raise OSError("collection effect response failed")
        with patch.object(self.manager.repository, "put", side_effect=fail_journal):
            with self.assertRaises(LifecycleError) as caught:
                self.apply_plan(plan, transaction_id="effect-double-failure", checkpoint=fail_effect)
        self.assertEqual(caught.exception.code, "retention_journal_failed")
        self.assertIn("collection effect response failed", caught.exception.details["primary"])
        self.assertIn("failure journal unavailable", caught.exception.details["journal"])
        tx = retention.recover_retention(self.manager, "effect-double-failure")
        self.assertNotEqual(tx["state"], "completed")
        self.assertEqual(tx["objects"][0]["state"], "intent")
        self.assertIsNone(tx["receipt_id"])
        self.assertEqual(self.manager.repository.list("retention-receipt"), [])
        self.assertFalse(Path(descriptor["path"]).exists())
        self.assertTrue(Path(plan["objects"][0]["quarantine_path"]).is_dir())
        result = retention.recover_retention(self.manager, "effect-double-failure", mode="resume", approve_plan_id=plan["plan_id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(inspect_source(self.source), source_before)

    def test_compensation_effect_and_failure_journal_errors_preserve_restored_object(self):
        descriptor = self.unreferenced()
        plan = retention.plan_retention(self.manager, "collect")
        def fail_collect(name, value):
            if name == "retention:0:effect":
                raise OSError("collect interrupted")
        with self.assertRaises(LifecycleError):
            self.apply_plan(plan, transaction_id="compensate-double-failure", checkpoint=fail_collect)
        real_put = self.manager.repository.put
        armed = False
        def fail_journal(kind, identifier, data, **kwargs):
            if armed and kind == "retention-transaction":
                raise OSError("compensation journal unavailable")
            return real_put(kind, identifier, data, **kwargs)
        def fail_effect(name, value):
            nonlocal armed
            if name == "retention-compensate:0:effect":
                armed = True
                raise OSError("compensation effect response failed")
        with patch.object(self.manager.repository, "put", side_effect=fail_journal):
            with self.assertRaises(LifecycleError) as caught:
                retention.recover_retention(self.manager, "compensate-double-failure", mode="compensate", approve_plan_id=plan["plan_id"], checkpoint=fail_effect)
        self.assertEqual(caught.exception.code, "retention_journal_failed")
        self.assertEqual(caught.exception.details["transaction_id"], "compensate-double-failure")
        self.assertIn("compensation effect response failed", caught.exception.details["primary"])
        self.assertIn("compensation journal unavailable", caught.exception.details["journal"])
        tx = retention.recover_retention(self.manager, "compensate-double-failure")
        self.assertEqual(tx["objects"][0]["state"], "compensating")
        self.assertIsNone(tx["receipt_id"])
        self.assertTrue(Path(descriptor["path"]).is_dir())
        self.assertFalse(Path(plan["objects"][0]["quarantine_path"]).exists())
        result = retention.recover_retention(self.manager, "compensate-double-failure", mode="compensate", approve_plan_id=plan["plan_id"])
        self.assertEqual(result["state"], "compensated")
        self.assertEqual(result["errors"][0]["message"], "collect interrupted")
        self.assertEqual(self.manager.repository.list("retention-receipt"), [])
        self.assertEqual(stat.S_IMODE(Path(descriptor["path"]).parent.stat().st_mode), plan["objects"][0]["root_mode"])

    def test_committed_response_failure_exposes_generated_recovery_identity(self):
        descriptor = self.unreferenced()
        plan = retention.plan_retention(self.manager, "collect")
        real_complete = retention._complete
        completed = {}
        def lose_response(manager, tx):
            completed.update(real_complete(manager, tx))
            raise OSError("completed response lost")
        with patch.object(retention, "_complete", side_effect=lose_response):
            with self.assertRaises(LifecycleError) as caught:
                self.apply_plan(plan)
        self.assertEqual(caught.exception.code, "retention_committed_response_failed")
        identifier = caught.exception.details["transaction_id"]
        self.assertEqual(identifier, completed["transaction_id"])
        self.assertEqual(retention.recover_retention(self.manager, identifier)["state"], "completed")
        self.assertFalse(Path(descriptor["path"]).exists())
        self.assertEqual(retention.recover_retention(self.manager, identifier, mode="resume", approve_plan_id=plan["plan_id"]), completed)
        self.assertEqual(len(self.manager.repository.list("retention-receipt")), 1)

    def test_unreadable_commit_state_preserves_primary_and_generated_recovery_identity(self):
        descriptor = self.unreferenced()
        plan = retention.plan_retention(self.manager, "collect")
        real_get = self.manager.repository.get
        observed = {}
        def stop(name, tx):
            if name == "retention:0:effect":
                observed["transaction_id"] = tx["transaction_id"]
                raise OSError("original collection response failed")
        def unreadable(kind, identifier):
            if observed and kind == "retention-transaction":
                raise OSError("commit state read unavailable")
            return real_get(kind, identifier)
        with patch.object(self.manager.repository, "get", side_effect=unreadable):
            with self.assertRaises(LifecycleError) as caught:
                self.apply_plan(plan, checkpoint=stop)
        self.assertEqual(caught.exception.code, "retention_journal_failed")
        identifier = caught.exception.details["transaction_id"]
        self.assertEqual(identifier, observed["transaction_id"])
        self.assertIn("original collection response failed", caught.exception.details["primary"])
        self.assertIn("commit state read unavailable", caught.exception.details["journal"])
        tx = retention.recover_retention(self.manager, identifier)
        self.assertNotEqual(tx["state"], "completed")
        self.assertIsNone(tx["receipt_id"])
        self.assertEqual(self.manager.repository.list("retention-receipt"), [])
        self.assertFalse(Path(descriptor["path"]).exists())
        self.assertTrue(Path(plan["objects"][0]["quarantine_path"]).is_dir())
        result = retention.recover_retention(self.manager, identifier, mode="resume", approve_plan_id=plan["plan_id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(self.manager.repository.list("retention-receipt")), 1)

    def test_unreadable_state_after_real_commit_never_overwrites_completion(self):
        self.unreferenced()
        plan = retention.plan_retention(self.manager, "collect")
        real_complete, real_get = retention._complete, self.manager.repository.get
        completed = {}
        def lose_response(manager, tx):
            completed.update(real_complete(manager, tx))
            raise OSError("response failed after durable commit")
        def unreadable(kind, identifier):
            if completed and kind == "retention-transaction":
                raise OSError("cannot determine completion now")
            return real_get(kind, identifier)
        with patch.object(retention, "_complete", side_effect=lose_response), patch.object(self.manager.repository, "get", side_effect=unreadable):
            with self.assertRaises(LifecycleError) as caught:
                self.apply_plan(plan)
        self.assertEqual(caught.exception.code, "retention_journal_failed")
        self.assertEqual(caught.exception.details["transaction_id"], completed["transaction_id"])
        tx = retention.recover_retention(self.manager, completed["transaction_id"])
        self.assertEqual(tx["state"], "completed")
        self.assertEqual(tx["errors"], [])
        self.assertEqual(retention.recover_retention(self.manager, completed["transaction_id"], mode="resume", approve_plan_id=plan["plan_id"]), completed)
        self.assertEqual(len(self.manager.repository.list("retention-receipt")), 1)

    def test_existing_incomplete_apply_names_the_required_recovery_record(self):
        self.unreferenced()
        plan = retention.plan_retention(self.manager, "collect")
        def stop(name, tx):
            if name == "retention:prepared":
                raise OSError("paused before effects")
        with self.assertRaises(LifecycleError):
            self.apply_plan(plan, transaction_id="known-incomplete", checkpoint=stop)
        with self.assertRaises(LifecycleError) as caught:
            self.apply_plan(plan, transaction_id="known-incomplete")
        self.assertEqual(caught.exception.code, "retention_recovery_required")
        self.assertEqual(caught.exception.details["transaction_id"], "known-incomplete")

    def test_plan_argument_bounds_paths_and_schema_corruption_fail_before_effects(self):
        descriptor = self.unreferenced()
        cases = [dict(grace_seconds=value) for value in (True, -1, 31536001, 1.1)]
        cases += [dict(object_ids=[value]) for value in ("/", "../escape", ".", "a/b")]
        cases += [dict(keep_recent=True), dict(pin_version_ids=["unknown"])]
        for options in cases:
            with self.subTest(options=options), self.assertRaises(LifecycleError):
                retention.plan_retention(self.manager, "collect", **options)
        for operation in ("unknown", None):
            with self.assertRaises(LifecycleError):
                retention.plan_retention(self.manager, operation)
        for plan in (None, {}, {"schema_version": "wrong"}):
            with self.assertRaises(LifecycleError):
                retention.apply_retention(self.manager, plan, approve_plan_id="anything")
        self.assertTrue(Path(descriptor["path"]).is_dir())

    def test_real_process_death_mid_purge_resumes_without_missing_file_failure(self):
        _, _, collected = self.quarantine()
        plan = retention.plan_retention(self.manager, "purge", object_ids=[collected["objects"][0]["quarantine_id"]], grace_seconds=0)
        code = """
import json,os,sys
from skills_auditor.lifecycle.engine import Manager
from skills_auditor.lifecycle.retention import apply_retention
plan=json.loads(sys.argv[2])
def die(name,tx):
    if name=='retention:0:delete_effect': os._exit(73)
apply_retention(Manager(sys.argv[1]),plan,approve_plan_id=plan['plan_id'],transaction_id='purge-crash',permanent_delete=True,checkpoint=die)
"""
        child = subprocess.run([sys.executable, "-c", code, str(self.root), json.dumps(plan)], capture_output=True, text=True, timeout=15)
        self.assertEqual(child.returncode, 73, child.stderr)
        before = retention.recover_retention(self.manager, "purge-crash")
        self.assertIsNotNone(before["objects"][0]["pending_delete"])
        result = retention.recover_retention(self.manager, "purge-crash", mode="resume", approve_plan_id=plan["plan_id"], permanent_delete=True)
        self.assertTrue(result["permanently_deleted"])
        self.assertTrue(self.source.exists())

    def test_purge_rechecks_file_bytes_after_delete_intent(self):
        _, _, collected = self.quarantine()
        plan = retention.plan_retention(self.manager, "purge", grace_seconds=0)
        modified = []
        def change(name, tx):
            if name == "retention:0:delete_intent" and not modified:
                step = tx["objects"][0]
                path = Path(step["object"]["quarantine_path"]) / step["pending_delete"]
                path.chmod(0o644)
                path.write_text("foreign replacement bytes")
                modified.append(path)
        with self.assertRaises(LifecycleError):
            self.apply_plan(plan, permanent_delete=True, transaction_id="purge-race", checkpoint=change)
        self.assertEqual(modified[0].read_text(), "foreign replacement bytes")
        self.assertIsNone(retention.recover_retention(self.manager, "purge-race")["receipt_id"])

    def test_completed_retry_checks_receipt_binding_and_rejects_later_restore(self):
        descriptor = self.unreferenced()
        plan = retention.plan_retention(self.manager, "collect")
        result = self.apply_plan(plan, transaction_id="retry-binding")
        record = self.manager.repository.get("retention-receipt", result["receipt_id"])
        changed = {**record["data"], "plan_id": "f" * 64}
        self.manager.repository.put("retention-receipt", result["receipt_id"], changed, expected_revision=record["revision"])
        with self.assertRaises(LifecycleError):
            self.apply_plan(plan, transaction_id="retry-binding")
        current = self.manager.repository.get("retention-receipt", result["receipt_id"])
        self.manager.repository.put("retention-receipt", result["receipt_id"], result, expected_revision=current["revision"])
        self.apply_plan(retention.plan_retention(self.manager, "restore"))
        with self.assertRaises(LifecycleError):
            self.apply_plan(plan, transaction_id="retry-binding")
        self.assertTrue(Path(descriptor["path"]).is_dir())

    def test_active_bounded_override_protects_old_version_until_expiry(self):
        from skills_auditor.lifecycle import invocation
        first = self.install()
        verification = self.manager.verify(first["installation_id"])
        now = datetime.fromisoformat(verification["observed_at"].replace("Z", "+00:00")) + timedelta(seconds=20)
        override_plan = invocation.plan_override(self.manager, first["installation_id"], reason="Temporary adapter exception", now=now, max_age_seconds=1)
        override = invocation.apply_override(self.manager, override_plan, approve_plan_id=override_plan["plan_id"], now=now)
        (self.source / "SKILL.md").write_text("# H2")
        self.operate("update", first, source=self.source)
        self.policy(keep_recent=0)
        self.apply_plan(retention.plan_retention(self.manager, "expire", receipt_ids=[first["receipt_id"]]))
        self.assertEqual(retention.plan_retention(self.manager, "collect")["objects"], [])
        expired = datetime.fromisoformat(override["expires_at"].replace("Z", "+00:00")) + timedelta(seconds=1)
        with patch.object(retention, "_clock", return_value=expired):
            self.assertEqual(len(retention.plan_retention(self.manager, "collect")["objects"]), 1)
        invocation.revoke_override(self.manager, override["override_id"], reason="Explicitly end exception", now=now)
        self.assertEqual(len(retention.plan_retention(self.manager, "collect")["objects"]), 1)

    def test_schema_valid_but_inconsistent_reference_records_are_rejected(self):
        first = self.install()
        original_list = self.manager.repository.list
        modifications = (
            ("version", lambda d: d.update(version_id="foreign")),
            ("version", lambda d: d["snapshot"].update(path="/outside")),
            ("version", lambda d: d.update(created_at="not-time")),
            ("installation", lambda d: d.update(state="future-state")),
            ("receipt", lambda d: d.update(status="failed")),
            ("transaction", lambda d: d.update(state="unknown")),
            ("transaction", lambda d: d.update(transaction_id="foreign")),
            ("receipt", lambda d: d.update(transaction_id="foreign")),
        )
        for kind, mutate in modifications:
            with self.subTest(kind=kind):
                def corrupt(requested):
                    records = copy.deepcopy(original_list(requested))
                    if requested == kind:
                        mutate(records[0]["data"])
                    return records
                with patch.object(self.manager.repository, "list", side_effect=corrupt):
                    with self.assertRaises(LifecycleError) as caught:
                        retention.plan_retention(self.manager, "collect")
                self.assertEqual(caught.exception.code, "retention_references_invalid")
                self.assertTrue(self.target.is_symlink())

    def test_metadata_only_failure_resume_and_recovery_permissions(self):
        plan = retention.plan_retention(self.manager, "policy", keep_recent=0)
        def fail(name, tx):
            if name == "retention:prepared":
                raise OSError("interrupted policy")
        with self.assertRaises(LifecycleError):
            self.apply_plan(plan, transaction_id="policy-recovery", checkpoint=fail)
        with self.assertRaises(LifecycleError):
            retention.plan_retention(self.manager, "collect")
        for options in ({"mode": "restore"}, {"mode": "resume"}, {"mode": "resume", "approve_plan_id": "other"}):
            with self.assertRaises(LifecycleError):
                retention.recover_retention(self.manager, "policy-recovery", **options)
        result = retention.recover_retention(self.manager, "policy-recovery", mode="resume", approve_plan_id=plan["plan_id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(retention.recover_retention(self.manager, "policy-recovery", mode="resume", approve_plan_id=plan["plan_id"]), result)
        with self.assertRaises(LifecycleError):
            retention.recover_retention(self.manager, "missing")

    def test_rehashed_inventory_path_and_quarantine_escape_are_rejected(self):
        self.unreferenced()
        plan = retention.plan_retention(self.manager, "collect")
        for key, value in (("quarantine_path", "/outside"), ("quarantine_id", "../outside"), ("kind", "other")):
            with self.subTest(key=key):
                corrupt = copy.deepcopy(plan); corrupt["objects"][0][key] = value
                corrupt["plan_id"] = digest({k: v for k, v in corrupt.items() if k != "plan_id"})
                with self.assertRaises(LifecycleError):
                    self.apply_plan(corrupt)
        corrupt = copy.deepcopy(plan)
        corrupt["objects"][0]["inventory"][0]["path"] = "../outside"
        corrupt["plan_id"] = digest({k: v for k, v in corrupt.items() if k != "plan_id"})
        with self.assertRaises(LifecycleError):
            self.apply_plan(corrupt)
        self.assertEqual(self.manager.repository.list("retention-transaction"), [])

    def test_foreign_entry_after_partial_purge_is_preserved_on_resume(self):
        _, _, receipt = self.quarantine()
        plan = retention.plan_retention(self.manager, "purge", grace_seconds=0)
        def stop(name, tx):
            if name == "retention:0:delete_effect":
                raise OSError("pause deletion")
        with self.assertRaises(LifecycleError):
            self.apply_plan(plan, permanent_delete=True, transaction_id="purge-foreign", checkpoint=stop)
        quarantine = Path(receipt["objects"][0]["quarantine_path"])
        quarantine.chmod(0o755)
        (quarantine / "foreign").write_text("keep")
        with self.assertRaises(LifecycleError):
            retention.recover_retention(self.manager, "purge-foreign", mode="resume", approve_plan_id=plan["plan_id"], permanent_delete=True)
        self.assertEqual((quarantine / "foreign").read_text(), "keep")
        self.assertEqual(self.manager.repository.get("retention-object", receipt["objects"][0]["quarantine_id"])["data"]["state"], "purging")

    def test_new_reference_after_partial_collection_can_compensate_without_a_success_receipt(self):
        descriptor = self.unreferenced()
        plan = retention.plan_retention(self.manager, "collect")
        def fail(name, tx):
            if name == "retention:0:effect":
                raise OSError("after quarantine")
        with self.assertRaises(LifecycleError):
            self.apply_plan(plan, transaction_id="collect-compensate", checkpoint=fail)
        installation_plan = self.manager.plan("install", source=self.source, target=self.target)
        self.manager.repository.put("transaction", "new-reference", {"transaction_id": "new-reference", "state": "prepared", "plan": installation_plan})
        with self.assertRaises(LifecycleError):
            retention.recover_retention(self.manager, "collect-compensate", mode="resume", approve_plan_id=plan["plan_id"])
        def interrupt(name, tx):
            if name == "retention-compensate:0:effect":
                raise OSError("compensation response interrupted")
        with self.assertRaises(LifecycleError):
            retention.recover_retention(self.manager, "collect-compensate", mode="compensate", approve_plan_id=plan["plan_id"], checkpoint=interrupt)
        result = retention.recover_retention(self.manager, "collect-compensate", mode="compensate", approve_plan_id=plan["plan_id"])
        self.assertEqual(result["state"], "compensated")
        self.assertIsNone(result["receipt_id"])
        self.assertGreaterEqual(len(result["errors"]), 2)
        self.assertTrue(Path(descriptor["path"]).is_dir())
        self.assertEqual(retention.recover_retention(self.manager, "collect-compensate", mode="compensate", approve_plan_id=plan["plan_id"]), result)
        with self.assertRaises(LifecycleError):
            retention.recover_retention(self.manager, "collect-compensate", mode="resume", approve_plan_id=plan["plan_id"])
        self.assertEqual(self.manager.repository.list("retention-receipt"), [])

    def test_compensation_preserves_foreign_original_occupancy_and_purge_cannot_compensate(self):
        descriptor = self.unreferenced()
        plan = retention.plan_retention(self.manager, "collect")
        def fail(name, tx):
            if name == "retention:0:effect":
                raise OSError("after quarantine")
        with self.assertRaises(LifecycleError):
            self.apply_plan(plan, transaction_id="compensation-foreign", checkpoint=fail)
        original = Path(descriptor["path"]).parent
        original.mkdir(); (original / "foreign").write_text("keep")
        with self.assertRaises(LifecycleError):
            retention.recover_retention(self.manager, "compensation-foreign", mode="compensate", approve_plan_id=plan["plan_id"])
        self.assertEqual((original / "foreign").read_text(), "keep")
        self.assertTrue(Path(plan["objects"][0]["quarantine_path"]).exists())

    def test_recent_policy_orders_offsets_chronologically_and_same_instant_deterministically(self):
        receipts = [self.install()]
        for contents in ("# H2", "# H3"):
            (self.source / "SKILL.md").write_text(contents)
            receipts.append(self.operate("update", receipts[0], source=self.source))
        receipts.append(self.operate("uninstall", receipts[0]))
        self.apply_plan(retention.plan_retention(self.manager, "expire", receipt_ids=[receipt["receipt_id"] for receipt in receipts]))
        dates = ("2026-01-01T00:00:00+08:00", "2025-12-31T18:00:00Z", "2025-12-31T20:00:00+02:00")
        for receipt, created in zip(receipts, dates):
            record = self.manager.repository.get("version", receipt["version_id"])
            self.manager.repository.put("version", receipt["version_id"], {**record["data"], "created_at": created}, expected_revision=record["revision"])
        self.policy(keep_recent=1)
        plan = retention.plan_retention(self.manager, "collect")
        expected_version = max(receipts[1]["version_id"], receipts[2]["version_id"])
        expected_hash = self.manager.repository.get("version", expected_version)["data"]["snapshot"]["snapshot_tree_sha256"]
        self.assertEqual(plan["protected"], {expected_hash: ["recent-rollback"]})
        self.assertEqual(len(plan["objects"]), 2)

    def test_malformed_and_future_version_timestamps_fail_closed(self):
        receipt = self.install()
        record = self.manager.repository.get("version", receipt["version_id"])
        for created in ("2026-01-01T00:00:00", "2026-01-01 00:00:00Z", "2999-01-01T00:00:00Z"):
            current = self.manager.repository.get("version", receipt["version_id"])
            self.manager.repository.put("version", receipt["version_id"], {**record["data"], "created_at": created}, expected_revision=current["revision"])
            with self.assertRaises(LifecycleError):
                retention.plan_retention(self.manager, "collect")
        self.assertTrue(self.target.is_symlink())

    def test_corrupt_quarantine_records_reject_before_inventory_or_path_use(self):
        _, _, receipt = self.quarantine()
        original_list = self.manager.repository.list
        variants = ("identifier", "name", "inventory", "state")
        for variant in variants:
            with self.subTest(variant=variant):
                def corrupt(kind):
                    records = copy.deepcopy(original_list(kind))
                    if kind == "retention-object":
                        record = records[0]
                        if variant == "identifier":
                            record["id"] = "../outside"
                            record["data"]["object"].update(quarantine_id="../outside", quarantine_path=str(self.manager.store_root / ".quarantine/../outside"))
                        elif variant == "name":
                            record["data"]["object"].update(name="../outside", object_id="../outside")
                        elif variant == "inventory":
                            record["data"]["object"]["inventory"][0]["path"] = "../outside"
                        else:
                            record["data"]["state"] = "unknown"
                    return records
                with patch.object(self.manager.repository, "list", side_effect=corrupt), patch.object(retention, "_inventory") as scan:
                    with self.assertRaises(LifecycleError):
                        retention.plan_retention(self.manager, "purge", grace_seconds=0)
                    scan.assert_not_called()
        self.assertTrue(Path(receipt["objects"][0]["quarantine_path"]).is_dir())

    def test_recovery_journal_objects_and_delete_progress_must_bind_approved_plan(self):
        self.quarantine()
        plan = retention.plan_retention(self.manager, "purge", grace_seconds=0)
        def stop(name, tx):
            if name == "retention:prepared":
                raise OSError("before effect")
        with self.assertRaises(LifecycleError):
            self.apply_plan(plan, transaction_id="corrupt-progress", permanent_delete=True, checkpoint=stop)
        original = retention.recover_retention(self.manager, "corrupt-progress")
        mutations = (
            lambda tx: tx["objects"][0]["object"].update(quarantine_path="/outside"),
            lambda tx: tx["objects"][0].update(pending_delete="../../outside"),
            lambda tx: tx["objects"][0].update(deleted=[tx["objects"][0]["object"]["inventory"][-1]["path"]]),
            lambda tx: tx.update(transaction_id="foreign"),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                record = self.manager.repository.get("retention-transaction", "corrupt-progress")
                changed = copy.deepcopy(original); mutate(changed)
                self.manager.repository.put("retention-transaction", "corrupt-progress", changed, expected_revision=record["revision"])
                with patch.object(retention, "_delete_entry") as delete:
                    with self.assertRaises(LifecycleError):
                        retention.recover_retention(self.manager, "corrupt-progress", mode="resume", approve_plan_id=plan["plan_id"], permanent_delete=True)
                    delete.assert_not_called()
        self.assertTrue(Path(plan["objects"][0]["quarantine_path"]).is_dir())

    def test_multiobject_compensation_is_reverse_order_and_purge_cannot_compensate(self):
        first = self.unreferenced()
        (self.source / "SKILL.md").write_text("# H2")
        second = self.unreferenced()
        plan = retention.plan_retention(self.manager, "collect")
        def stop(name, tx):
            if name == "retention:1:effect":
                raise OSError("second object response lost")
        with self.assertRaises(LifecycleError):
            self.apply_plan(plan, transaction_id="two-objects", checkpoint=stop)
        effects = []
        result = retention.recover_retention(self.manager, "two-objects", mode="compensate", approve_plan_id=plan["plan_id"], checkpoint=lambda name, tx: effects.append(name))
        self.assertEqual(result["state"], "compensated")
        self.assertEqual([name for name in effects if name.endswith(":effect")], ["retention-compensate:1:effect", "retention-compensate:0:effect"])
        self.assertTrue(Path(first["path"]).is_dir()); self.assertTrue(Path(second["path"]).is_dir())
        self.apply_plan(retention.plan_retention(self.manager, "collect"))
        purge = retention.plan_retention(self.manager, "purge", grace_seconds=0)
        def pause(name, tx):
            if name == "retention:prepared":
                raise OSError("pause")
        with self.assertRaises(LifecycleError):
            self.apply_plan(purge, transaction_id="no-undo-purge", permanent_delete=True, checkpoint=pause)
        with self.assertRaises(LifecycleError):
            retention.recover_retention(self.manager, "no-undo-purge", mode="compensate", approve_plan_id=purge["plan_id"], permanent_delete=True)

    def test_completed_collect_retry_refuses_changed_payload(self):
        _, plan, receipt = self.quarantine()
        tree = Path(receipt["objects"][0]["quarantine_path"]) / "tree/SKILL.md"
        tree.chmod(0o644); tree.write_text("foreign bytes")
        with self.assertRaises(LifecycleError):
            self.apply_plan(plan, transaction_id=receipt["transaction_id"])
        self.assertEqual(tree.read_text(), "foreign bytes")

    def test_compensation_checks_all_restored_objects_before_terminal_state(self):
        self.unreferenced()
        (self.source / "SKILL.md").write_text("# H2")
        self.unreferenced()
        plan = retention.plan_retention(self.manager, "collect")
        def stop(name, tx):
            if name == "retention:1:effect":
                raise OSError("pause")
        with self.assertRaises(LifecycleError):
            self.apply_plan(plan, transaction_id="compensation-final", checkpoint=stop)
        changed = self.manager.store_root / plan["objects"][1]["name"] / "tree/SKILL.md"
        def tamper(name, tx):
            if name == "retention-compensate:0:effect":
                changed.chmod(0o644); changed.write_text("foreign")
        with self.assertRaises(LifecycleError):
            retention.recover_retention(self.manager, "compensation-final", mode="compensate", approve_plan_id=plan["plan_id"], checkpoint=tamper)
        result = retention.recover_retention(self.manager, "compensation-final")
        self.assertEqual(result["state"], "recovery_needed")
        self.assertIsNone(result["receipt_id"])
        self.assertEqual(changed.read_text(), "foreign")

    def test_real_crash_after_rename_recovers_and_history_is_schema_valid(self):
        self.unreferenced()
        plan = retention.plan_retention(self.manager, "collect")
        code = """
import json,os,sys
from skills_auditor.lifecycle.engine import Manager
from skills_auditor.lifecycle.retention import apply_retention
plan=json.loads(sys.argv[2])
def die(name,tx):
    if name=='retention:0:effect': os._exit(73)
apply_retention(Manager(sys.argv[1]),plan,approve_plan_id=plan['plan_id'],transaction_id='crashed',checkpoint=die)
"""
        result = subprocess.run([sys.executable, "-c", code, str(self.root), json.dumps(plan)], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 73, result.stderr)
        recovered = retention.recover_retention(self.manager, "crashed", mode="resume", approve_plan_id=plan["plan_id"])
        for name, value in (("plan", plan), ("receipt", recovered)):
            schema = json.loads((Path(__file__).resolve().parents[1] / "skills_auditor/schemas" / ("lifecycle-retention-" + name + "-v1.schema.json")).read_text())
            jsonschema.Draft202012Validator.check_schema(schema)
            jsonschema.validate(value, schema)

    def test_real_crash_between_permission_change_and_rename_compensates_original_mode(self):
        descriptor = self.unreferenced()
        plan = retention.plan_retention(self.manager, "collect")
        original = Path(descriptor["path"]).parent
        original_mode = stat.S_IMODE(original.stat().st_mode)
        code = """
import json,os,sys
from skills_auditor.lifecycle.engine import Manager
from skills_auditor.lifecycle import retention
plan=json.loads(sys.argv[2])
retention.os.rename=lambda *a,**k: os._exit(71)
retention.apply_retention(Manager(sys.argv[1]),plan,approve_plan_id=plan['plan_id'],transaction_id='killed-rename')
"""
        result = subprocess.run([sys.executable, "-c", code, str(self.root), json.dumps(plan)], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 71, result.stderr)
        self.assertEqual(stat.S_IMODE(original.stat().st_mode), original_mode | 0o700)
        result = retention.recover_retention(self.manager, "killed-rename", mode="compensate", approve_plan_id=plan["plan_id"])
        self.assertEqual(result["state"], "compensated")
        self.assertEqual(stat.S_IMODE(original.stat().st_mode), original_mode)
        self.assertEqual((original / "tree/SKILL.md").read_text(), "# H1")
        self.assertEqual(self.manager.repository.list("retention-receipt"), [])
        original.chmod(0o777)
        with self.assertRaises(LifecycleError):
            retention.recover_retention(self.manager, "killed-rename", mode="compensate", approve_plan_id=plan["plan_id"])
        self.assertEqual(stat.S_IMODE(original.stat().st_mode), 0o777)

    def test_nonjournaled_mode_change_is_preserved_not_silently_compensated(self):
        descriptor = self.unreferenced()
        plan = retention.plan_retention(self.manager, "collect")
        def stop(name, tx):
            if name == "retention:0:intent":
                raise OSError("before rename")
        with self.assertRaises(LifecycleError):
            self.apply_plan(plan, transaction_id="foreign-mode", checkpoint=stop)
        original = Path(descriptor["path"]).parent
        original.chmod(0o777)
        with self.assertRaises(LifecycleError):
            retention.recover_retention(self.manager, "foreign-mode", mode="compensate", approve_plan_id=plan["plan_id"])
        self.assertEqual(stat.S_IMODE(original.stat().st_mode), 0o777)
        self.assertEqual(retention.recover_retention(self.manager, "foreign-mode")["state"], "recovery_needed")

    def test_expiration_cannot_release_references_without_exact_approved_completion(self):
        first = self.install()
        last = self.operate("uninstall", first)
        self.policy(keep_recent=0)
        self.assertEqual(retention.plan_retention(self.manager, "collect")["objects"], [])
        for receipt in (first, last):
            self.manager.repository.put("payload-retention", "receipt:" + receipt["receipt_id"], {
                "kind": "receipt", "id": receipt["receipt_id"], "state": "expired"})
        with self.assertRaises(LifecycleError):
            retention.plan_retention(self.manager, "collect")
        self.assertEqual(self.manager.repository.list("retention-object"), [])

    def test_policy_cannot_release_recent_versions_without_approved_completion(self):
        first = self.install(); last = self.operate("uninstall", first)
        self.apply_plan(retention.plan_retention(self.manager, "expire", receipt_ids=[first["receipt_id"], last["receipt_id"]]))
        self.assertEqual(retention.plan_retention(self.manager, "collect")["objects"], [])
        self.manager.repository.put("retention-policy", "default", {"keep_recent": 0, "pin_version_ids": []})
        with self.assertRaises(LifecycleError):
            retention.plan_retention(self.manager, "collect")

    def test_expiry_receipt_event_and_transaction_corruption_preserve_payload(self):
        first = self.install(); last = self.operate("uninstall", first)
        self.policy(keep_recent=0)
        expiry = self.apply_plan(retention.plan_retention(self.manager, "expire", receipt_ids=[first["receipt_id"], last["receipt_id"]]))
        self.assertEqual(len(retention.plan_retention(self.manager, "collect")["objects"]), 1)
        original_get = self.manager.repository.get
        for kind, field, value in (("retention-receipt", "plan_id", "f" * 64), ("retention-transaction", "approved_plan_id", "not-approved"), ("retention-transaction", "state", "prepared")):
            def corrupt(requested, identifier):
                record = original_get(requested, identifier)
                if requested == kind and identifier in {expiry["receipt_id"], expiry["transaction_id"]}:
                    record = copy.deepcopy(record); record["data"][field] = value
                return record
            with self.subTest(kind=kind, field=field), patch.object(self.manager.repository, "get", side_effect=corrupt):
                with self.assertRaises(LifecycleError):
                    retention.plan_retention(self.manager, "collect")
        with patch.object(self.manager.repository, "events", return_value=[]):
            with self.assertRaises(LifecycleError):
                retention.plan_retention(self.manager, "collect")
        snapshot = self.manager.repository.get("version", first["version_id"])["data"]["snapshot"]
        self.assertTrue(Path(snapshot["path"]).is_dir())

    def test_metadata_projection_failure_rolls_back_completion_and_recovers_without_false_receipt(self):
        first = self.install(); last = self.operate("uninstall", first)
        original_put = self.manager.repository.put
        for operation, projection_kind, options in (("policy", "retention-policy", {"keep_recent": 0}),
                                                    ("expire", "payload-retention", {"receipt_ids": [first["receipt_id"], last["receipt_id"]]})):
            with self.subTest(operation=operation):
                plan = retention.plan_retention(self.manager, operation, **options)
                txid = "metadata-write-" + operation
                receipts = self.manager.repository.list("retention-receipt")
                def fail(kind, *args, **kwargs):
                    if kind == projection_kind:
                        raise OSError("projection write failed after completion was staged")
                    return original_put(kind, *args, **kwargs)
                with patch.object(self.manager.repository, "put", side_effect=fail):
                    with self.assertRaises(LifecycleError):
                        self.apply_plan(plan, transaction_id=txid)
                tx = retention.recover_retention(self.manager, txid)
                self.assertEqual(tx["state"], "recovery_needed")
                self.assertIsNone(tx["receipt_id"])
                self.assertNotIn("completion_event_sequence", tx)
                self.assertEqual(self.manager.repository.list("retention-receipt"), receipts)
                self.assertEqual(self.manager.repository.events("retention:" + txid), [])
                result = retention.recover_retention(self.manager, txid, mode="resume", approve_plan_id=plan["plan_id"])
                self.assertEqual(result["status"], "completed")
        self.assertEqual(len(retention.plan_retention(self.manager, "collect")["objects"]), 1)

    def test_completed_expiry_retry_requires_its_retained_completion_event(self):
        first = self.install(); last = self.operate("uninstall", first)
        plan = retention.plan_retention(self.manager, "expire", receipt_ids=[first["receipt_id"], last["receipt_id"]])
        receipt = self.apply_plan(plan)
        with patch.object(self.manager.repository, "events", return_value=[]):
            with self.assertRaises(LifecycleError):
                self.apply_plan(plan, transaction_id=receipt["transaction_id"])

    def test_false_terminal_incident_cannot_be_expired_without_historical_proof(self):
        from skills_auditor.lifecycle import incidents
        first = self.install()
        self.target.unlink()
        self.manager.verify(first["installation_id"])
        incident = incidents.list_incidents(self.manager)[0]
        row = self.manager.repository.get("incident", incident["incident_id"])
        changed = {**incident, "state": "resolved", "resolution": {
            "kind": "non_remediation", "resolved_at": incident["updated_at"],
            "disposition": "obsolete", "explanation": "No explicit resolution ever happened"}}
        self.manager.repository.put("incident", incident["incident_id"], changed, expected_revision=row["revision"])
        with self.assertRaises(LifecycleError):
            incidents.get_incident(self.manager, incident["incident_id"])
        with self.assertRaises(LifecycleError):
            retention.plan_retention(self.manager, "expire", incident_ids=[incident["incident_id"]])
        self.assertEqual(self.manager.repository.list("payload-retention"), [])

    def test_override_projection_cannot_silently_release_payload_without_immutable_proof(self):
        from skills_auditor.lifecycle import invocation
        first = self.install()
        verification = self.manager.verify(first["installation_id"])
        now = datetime.fromisoformat(verification["observed_at"].replace("Z", "+00:00")) + timedelta(seconds=20)
        plan = invocation.plan_override(self.manager, first["installation_id"], reason="Fixture exception", now=now, max_age_seconds=1)
        override = invocation.apply_override(self.manager, plan, approve_plan_id=plan["plan_id"], now=now)
        (self.source / "SKILL.md").write_text("# H2")
        second = self.operate("update", first, source=self.source)
        self.policy(keep_recent=0)
        self.apply_plan(retention.plan_retention(self.manager, "expire", receipt_ids=[first["receipt_id"]]))
        self.assertEqual(retention.plan_retention(self.manager, "collect")["objects"], [])
        for change in ({"state": "revoked"}, {"expires_at": "2000-01-01T00:00:00Z"}, {"version_id": second["version_id"]}):
            row = self.manager.repository.get("invocation-override", override["override_id"])
            self.manager.repository.put("invocation-override", override["override_id"], {**override, **change}, expected_revision=row["revision"])
            with self.subTest(change=change), self.assertRaises(LifecycleError):
                retention.plan_retention(self.manager, "collect")
        self.assertEqual(self.manager.repository.list("retention-object"), [])

    def test_live_materialization_stage_cannot_be_collected_but_crashed_stage_can(self):
        code = """
import json,sys
from pathlib import Path
from skills_auditor.lifecycle.snapshots import inspect_source,materialize
source,store=Path(sys.argv[1]),Path(sys.argv[2]); manifest=inspect_source(source)
def hold(name,details):
    if name=='snapshot_copied':
        print(details['stage'],flush=True);sys.stdin.readline()
materialize(source,store,manifest['source_tree_sha256'],manifest['snapshot_tree_sha256'],checkpoint=hold)
"""
        process = subprocess.Popen([sys.executable, "-c", code, str(self.source), str(self.manager.store_root)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                self.assertTrue(selector.select(timeout=10))
            stage = Path(process.stdout.readline().strip())
            self.assertTrue(stage.is_dir())
            self.assertEqual(retention.plan_retention(self.manager, "collect")["objects"], [])
            with self.assertRaises(LifecycleError) as caught:
                retention.plan_retention(self.manager, "collect", stage_names=[stage.name])
            self.assertEqual(caught.exception.code, "retention_stage_busy")
            process.kill(); process.wait(timeout=10)
            plan = retention.plan_retention(self.manager, "collect", stage_names=[stage.name])
            result = self.apply_plan(plan)
            self.assertEqual(result["status"], "completed")
            self.assertFalse(stage.exists())
        finally:
            if process.poll() is None:
                process.kill(); process.wait(timeout=10)
            for handle in (process.stdin, process.stdout, process.stderr):
                handle.close()
