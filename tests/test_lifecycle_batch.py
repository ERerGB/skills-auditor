"""Recoverable batches retain honest per-child outcomes across real crashes."""

import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from skills_auditor.lifecycle.batch import BatchManager
from skills_auditor.lifecycle.common import LifecycleError, digest
from skills_auditor.lifecycle.engine import Manager


class TestLifecycleBatch(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="skills-auditor-batch-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.manager = Manager(self.root)
        self.addCleanup(self.manager.repository.close)
        self.batch = BatchManager(self.manager)
        self.sources, self.targets = [], []
        for index in range(2):
            source = self.root / ("candidate-" + str(index))
            source.mkdir()
            (source / "SKILL.md").write_text("# Candidate\n")
            (source / "payload").write_text("H1")
            host = self.root / ("host-" + str(index))
            host.mkdir()
            self.sources.append(source)
            self.targets.append(host / "skill")

    def install_plans(self):
        return [self.manager.plan("install", source=source, target=target) for source, target in zip(self.sources, self.targets)]

    def execute(self, plan, **kwargs):
        return self.batch.apply(plan, approve_plan_id=plan["plan_id"], **kwargs)

    def test_completed_batch_retry_records_sticky_failure_without_repeating_effects(self):
        plan = self.batch.plan(self.install_plans())
        receipt = self.execute(plan, batch_id="retry-observation")
        identifier = plan["children"][0]["plan"]["installation_id"]
        self.manager.verify(identifier)
        original_grant = self.manager.get_installation(identifier)["authorization"]["grant_id"]
        original_link = os.readlink(self.targets[0])
        second_inode = self.targets[1].lstat().st_ino
        self.targets[0].unlink()
        with self.assertRaises(LifecycleError):
            self.execute(plan, batch_id="retry-observation")
        self.assertEqual(self.manager.get_installation(identifier)["authorization"]["state"], "invalidated")
        self.targets[0].symlink_to(original_link)
        self.assertFalse(self.manager.verify(identifier)["valid"])
        self.assertEqual(self.manager.get_installation(identifier)["authorization"]["grant_id"], original_grant)
        self.assertEqual(self.targets[1].lstat().st_ino, second_inode)
        self.assertEqual(self.batch.inspect("retry-observation")["receipt_id"], receipt["receipt_id"])
        self.assertEqual(len(self.manager.repository.list("receipt")), 2)

    def test_parent_only_pending_batch_fences_core_and_other_batches(self):
        plan = self.batch.plan(self.install_plans())
        def fail(name, value):
            if name == "batch:prepared":
                raise OSError("parent-only interruption")
        with self.assertRaises(LifecycleError):
            self.execute(plan, batch_id="pending-parent", checkpoint=fail)
        later = self.install_plans()
        with self.assertRaises(LifecycleError) as caught:
            self.manager.apply(later[0], approve_plan_id=later[0]["plan_id"])
        self.assertEqual(caught.exception.code, "pending_batch")
        self.assertEqual(caught.exception.details["batch_id"], "pending-parent")
        with self.assertRaises(LifecycleError):
            self.execute(self.batch.plan(later), batch_id="later-parent")
        self.assertIsNone(self.manager.repository.get("batch", "later-parent"))
        self.assertEqual(self.manager.repository.list("transaction"), [])
        inverse = self.batch.plan_compensation("pending-parent")
        self.assertEqual(self.execute(inverse)["status"], "completed")
        self.assertEqual(self.execute(self.batch.plan(later))["status"], "completed")

    def test_saved_batch_missing_current_parent_invalidates_before_parent_intent(self):
        installs = self.install_plans()
        self.execute(self.batch.plan(installs))
        renewals = [self.manager.plan("renew", installation_id=plan["installation_id"]) for plan in installs]
        saved = self.batch.plan(renewals)
        parent = self.targets[0].parent
        parent.rename(self.root / "displaced-host")
        with self.assertRaises(LifecycleError):
            self.execute(saved, batch_id="missing-parent")
        self.assertEqual(self.manager.get_installation(installs[0]["installation_id"])["authorization"]["state"], "invalidated")
        self.assertEqual(self.manager.get_installation(installs[1]["installation_id"])["authorization"]["state"], "valid")
        self.assertIsNone(self.manager.repository.get("batch", "missing-parent"))
        (self.root / "displaced-host").rename(parent)
        self.assertFalse(self.manager.verify(installs[0]["installation_id"])["valid"])

    def test_no_effects_inverse_cancellation_keeps_denial_without_blocking_repair(self):
        core = self.install_plans()[0]
        installed = self.manager.apply(core, approve_plan_id=core["plan_id"])
        pending = self.batch.plan([self.manager.plan("renew", installation_id=installed["installation_id"])])
        def fail(name, value):
            if name == "batch:child:0:transaction:prepared":
                raise OSError("child has no effects")
        with self.assertRaises(LifecycleError):
            self.execute(pending, batch_id="cancel-no-effects", checkpoint=fail)
        payload = self.targets[0] / "payload"
        mode = payload.stat().st_mode & 0o777
        payload.chmod(mode | 0o200)
        payload.write_text("DAMAGED H1")
        payload.chmod(mode)
        inverse = self.batch.plan_compensation("cancel-no-effects")
        self.assertEqual(self.execute(inverse)["status"], "completed")
        self.assertEqual(self.batch.inspect("cancel-no-effects")["state"], "compensated")
        self.assertEqual(self.manager.get_installation(installed["installation_id"])["authorization"]["state"], "invalidated")
        (self.sources[0] / "payload").write_text("H2")
        repair = self.manager.plan("update", installation_id=installed["installation_id"], source=self.sources[0])
        self.manager.apply(repair, approve_plan_id=repair["plan_id"])
        self.assertTrue(self.manager.verify(installed["installation_id"])["valid"])

    def test_exact_approval_whole_batch_preflight_and_idempotent_completed_retry(self):
        plan = self.batch.plan(self.install_plans())
        with self.assertRaises(LifecycleError):
            self.batch.apply(plan, approve_plan_id=None)
        self.assertEqual(self.manager.repository.list("batch"), [])
        (self.sources[1] / "payload").write_text("H2")
        with self.assertRaises(LifecycleError):
            self.execute(plan, batch_id="stale")
        self.assertEqual(self.manager.repository.list("transaction"), [])
        self.assertEqual(self.manager.repository.list("batch"), [])
        self.assertFalse(any(path.is_symlink() for path in self.targets))
        plan = self.batch.plan(self.install_plans())
        receipt = self.execute(plan, batch_id="complete")
        inodes = [path.lstat().st_ino for path in self.targets]
        self.assertEqual(self.execute(plan, batch_id="complete"), receipt)
        self.assertEqual(self.batch.recover("complete", mode="resume", approve_plan_id=plan["plan_id"]), receipt)
        self.assertEqual([path.lstat().st_ino for path in self.targets], inodes)
        self.assertEqual(len(self.manager.repository.list("receipt")), 2)

    def test_batch_identity_and_rehashed_invalid_children_rejected_without_effects(self):
        plans = self.install_plans()
        for children in ([], [plans[0], plans[0]], [plans[0], {**plans[1], "plan_id": "wrong"}]):
            with self.subTest(children=children):
                with self.assertRaises(LifecycleError):
                    self.batch.plan(children)
        plan = self.batch.plan(plans)
        for mutation in (lambda p: p.update(children=[]), lambda p: p.update(compensates_revision=True), lambda p: p["children"][0].update(kind="compensate")):
            changed = copy.deepcopy(plan)
            mutation(changed)
            changed["plan_id"] = digest({key: value for key, value in changed.items() if key != "plan_id"})
            with self.assertRaises(LifecycleError):
                self.execute(changed)
        self.assertEqual(self.manager.repository.list("batch"), [])
        self.assertFalse(any(path.is_symlink() for path in self.targets))

    def test_real_process_death_has_explicit_resume_and_no_duplicate_child_effects(self):
        script = """
import json, os, sys
from skills_auditor.lifecycle.engine import Manager
from skills_auditor.lifecycle.batch import BatchManager
plan=json.loads(sys.argv[2])
def die(name, value):
    if name == sys.argv[3]: os._exit(73)
BatchManager(Manager(sys.argv[1])).apply(plan, approve_plan_id=plan['plan_id'], batch_id='crashed', checkpoint=die)
"""
        for boundary in ("batch:prepared", "batch:child:0:started", "batch:child:0:step:0:effect", "batch:child:0:transaction:committed", "batch:child:0:recorded", "batch:child:1:step:0:effect", "batch:before_commit"):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory(dir=self.root) as directory:
                root = Path(directory)
                manager = Manager(root)
                self.addCleanup(manager.repository.close)
                plans = [manager.plan("install", source=source, target=root / ("target-" + str(index))) for index, source in enumerate(self.sources)]
                batch = BatchManager(manager)
                plan = batch.plan(plans)
                died = subprocess.run([sys.executable, "-c", script, str(root), json.dumps(plan), boundary], capture_output=True, text=True, timeout=20)
                self.assertEqual(died.returncode, 73, died.stderr)
                before = {path: path.lstat().st_ino for path in root.glob("target-*") if path.is_symlink()}
                with self.assertRaises(LifecycleError) as caught:
                    batch.apply(plan, approve_plan_id=plan["plan_id"], batch_id="crashed")
                self.assertEqual(caught.exception.details["batch_id"], "crashed")
                with self.assertRaises(LifecycleError):
                    batch.recover("crashed", mode="resume")
                receipt = batch.recover("crashed", mode="resume", approve_plan_id=plan["plan_id"])
                self.assertEqual(receipt["status"], "completed")
                self.assertEqual(len(manager.repository.list("receipt")), 2)
                for path, inode in before.items():
                    self.assertEqual(path.lstat().st_ino, inode)

    def test_inverse_cannot_adopt_an_unrelated_installation_with_recomputed_checksum(self):
        plans = self.install_plans()
        original = self.batch.plan([plans[0]])
        self.execute(original, batch_id="original")
        unrelated = self.manager.apply(plans[1], approve_plan_id=plans[1]["plan_id"])
        inverse = self.batch.plan_compensation("original")
        inverse["children"][0]["plan"] = self.manager.plan("uninstall", installation_id=unrelated["installation_id"])
        inverse["plan_id"] = digest({key: value for key, value in inverse.items() if key != "plan_id"})
        with self.assertRaises(LifecycleError):
            self.execute(inverse)
        self.assertTrue(all(path.is_symlink() for path in self.targets))
        self.assertEqual(self.batch.inspect("original")["state"], "completed")

    def test_completed_parent_rejects_missing_children_and_changed_approval_or_child_id(self):
        plan = self.batch.plan(self.install_plans())
        receipt = self.execute(plan, batch_id="completed-proof")
        original = self.manager.repository.get("batch", "completed-proof")["data"]
        mutations = (lambda tx: tx.update(children=[]), lambda tx: tx.update(approved_plan_id="foreign"), lambda tx: tx["children"][0].update(transaction_id="foreign"))
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                changed = copy.deepcopy(original)
                mutate(changed)
                record = self.manager.repository.get("batch", "completed-proof")
                self.manager.repository.put("batch", "completed-proof", changed, expected_revision=record["revision"])
                with self.assertRaises(LifecycleError):
                    self.execute(plan, batch_id="completed-proof")
                with self.assertRaises(LifecycleError):
                    self.batch.recover("completed-proof", mode="resume", approve_plan_id=plan["plan_id"])
        self.assertEqual(self.manager.repository.get("batch-receipt", receipt["receipt_id"])["data"], receipt)

    def test_prepared_parent_cannot_truncate_children_and_report_false_completion(self):
        plan = self.batch.plan(self.install_plans())
        def fail(name, value):
            if name == "batch:prepared":
                raise OSError("stopped before child effects")
        with self.assertRaises(LifecycleError):
            self.execute(plan, batch_id="truncated", checkpoint=fail)
        record = self.manager.repository.get("batch", "truncated")
        self.manager.repository.put("batch", "truncated", {**record["data"], "children": []}, expected_revision=record["revision"])
        with self.assertRaises(LifecycleError):
            self.batch.recover("truncated", mode="resume", approve_plan_id=plan["plan_id"])
        self.assertFalse(any(target.is_symlink() for target in self.targets))
        self.assertEqual(self.manager.repository.list("batch-receipt"), [])

    def test_static_terminal_inspection_requires_real_receipt_and_compensation_proof(self):
        plan = self.batch.plan(self.install_plans())
        def fail(name, value):
            if name == "batch:prepared":
                raise OSError("durable parent only")
        with self.assertRaises(LifecycleError):
            self.execute(plan, batch_id="fake-terminal", checkpoint=fail)
        record = self.manager.repository.get("batch", "fake-terminal")
        original = copy.deepcopy(record["data"])
        changed = copy.deepcopy(original)
        changed.update(state="completed", receipt_id="fake-parent-receipt")
        for reference in changed["children"]:
            reference.update(state="completed", receipt_id="fake-child-" + str(reference["index"]))
        self.manager.repository.put("batch", "fake-terminal", changed, expected_revision=record["revision"])
        with self.assertRaises(LifecycleError):
            self.batch.inspect("fake-terminal")
        record = self.manager.repository.get("batch", "fake-terminal")
        changed = {**original, "state": "compensated", "compensation_batch_id": "fake-inverse"}
        self.manager.repository.put("batch", "fake-terminal", changed, expected_revision=record["revision"])
        with self.assertRaises(LifecycleError):
            self.batch.inspect("fake-terminal")

    def test_compensation_plan_requires_original_completed_receipt_proof(self):
        plan = self.batch.plan(self.install_plans())
        receipt = self.execute(plan, batch_id="missing-history")
        original = self.manager.repository.get
        def missing(kind, identifier):
            return None if kind == "batch-receipt" and identifier == receipt["receipt_id"] else original(kind, identifier)
        with patch.object(self.manager.repository, "get", side_effect=missing):
            with self.assertRaises(LifecycleError):
                self.batch.plan_compensation("missing-history")
        self.assertTrue(all(target.is_symlink() for target in self.targets))

    def test_saved_inverse_apply_rechecks_original_completed_receipt_proof(self):
        plan = self.batch.plan(self.install_plans())
        receipt = self.execute(plan, batch_id="missing-history")
        inverse = self.batch.plan_compensation("missing-history")
        original = self.manager.repository.get
        def missing(kind, identifier):
            return None if kind == "batch-receipt" and identifier == receipt["receipt_id"] else original(kind, identifier)
        with patch.object(self.manager.repository, "get", side_effect=missing):
            with self.assertRaises(LifecycleError):
                self.execute(inverse, batch_id="refused-inverse")
        self.assertIsNone(self.manager.repository.get("batch", "refused-inverse"))
        self.assertTrue(all(target.is_symlink() for target in self.targets))

    def test_inverse_resume_rechecks_original_completed_receipt_proof(self):
        plan = self.batch.plan(self.install_plans())
        receipt = self.execute(plan, batch_id="missing-history")
        inverse = self.batch.plan_compensation("missing-history")
        def stop(name, value):
            if name == "batch:prepared":
                raise OSError("inverse intent only")
        with self.assertRaises(LifecycleError):
            self.execute(inverse, batch_id="refused-resume", checkpoint=stop)
        original = self.manager.repository.get
        def missing(kind, identifier):
            return None if kind == "batch-receipt" and identifier == receipt["receipt_id"] else original(kind, identifier)
        with patch.object(self.manager.repository, "get", side_effect=missing):
            with self.assertRaises(LifecycleError):
                self.batch.recover("refused-resume", mode="resume", approve_plan_id=inverse["plan_id"])
        self.assertTrue(all(target.is_symlink() for target in self.targets))
        self.assertIsNone(self.batch.inspect("refused-resume")["receipt_id"])

    def test_forged_terminal_child_reference_cannot_skip_an_unfinished_core_transaction(self):
        plan = self.batch.plan(self.install_plans())
        def fail(name, value):
            if name == "batch:child:0:transaction:prepared":
                raise OSError("child has intent only")
        with self.assertRaises(LifecycleError):
            self.execute(plan, batch_id="forged-child", checkpoint=fail)
        record = self.manager.repository.get("batch", "forged-child")
        changed = record["data"]
        changed["children"][0].update(state="completed", receipt_id="fake-completion")
        self.manager.repository.put("batch", "forged-child", changed, expected_revision=record["revision"])
        with self.assertRaises(LifecycleError):
            self.batch.recover("forged-child", mode="resume", approve_plan_id=plan["plan_id"])
        self.assertFalse(any(target.is_symlink() for target in self.targets))
        self.assertEqual(self.manager.repository.list("batch-receipt"), [])

    def test_explicit_inverse_survives_real_process_death_and_preserves_receipts(self):
        plan = self.batch.plan(self.install_plans())
        def fail(name, value):
            if name == "batch:child:1:step:0:effect":
                raise OSError("second child incomplete")
        with self.assertRaises(LifecycleError):
            self.execute(plan, batch_id="original-partial", checkpoint=fail)
        historical = self.manager.repository.list("receipt")
        inverse = self.batch.plan_compensation("original-partial")
        script = """
import json,os,sys
from skills_auditor.lifecycle.engine import Manager
from skills_auditor.lifecycle.batch import BatchManager
plan=json.loads(sys.argv[2])
def die(name,value):
    if name == 'batch:child:1:step:0:effect': os._exit(74)
BatchManager(Manager(sys.argv[1])).apply(plan,approve_plan_id=plan['plan_id'],batch_id='inverse-crash',checkpoint=die)
"""
        died = subprocess.run([sys.executable, "-c", script, str(self.root), json.dumps(inverse)], capture_output=True, text=True, timeout=20)
        self.assertEqual(died.returncode, 74, died.stderr)
        self.assertFalse(any(target.is_symlink() for target in self.targets))
        with self.assertRaises(LifecycleError):
            self.batch.recover("original-partial", mode="resume", approve_plan_id=plan["plan_id"])
        receipt = self.batch.recover("inverse-crash", mode="resume", approve_plan_id=inverse["plan_id"])
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(self.batch.inspect("original-partial")["state"], "compensated")
        for old in historical:
            self.assertEqual(self.manager.repository.get("receipt", old["id"]), old)
        self.assertEqual(self.execute(inverse, batch_id="inverse-crash"), receipt)

    def test_new_parent_cannot_adopt_a_preexisting_completed_child_transaction(self):
        plan = self.batch.plan(self.install_plans())
        child = plan["children"][0]
        transaction_id = self.batch._child_id("adoption", 0, child)
        self.manager.apply(child["plan"], approve_plan_id=child["plan"]["plan_id"], transaction_id=transaction_id)
        with self.assertRaises(LifecycleError):
            self.execute(plan, batch_id="adoption")
        self.assertIsNone(self.manager.repository.get("batch", "adoption"))
        self.assertFalse(self.targets[1].is_symlink())

    def test_partial_batch_requires_new_reviewed_inverse_and_preserves_foreign_entries(self):
        plan = self.batch.plan(self.install_plans())

        def fail(name, data):
            if name == "batch:child:1:step:0:effect":
                raise OSError("interrupted second child")

        with self.assertRaises(LifecycleError):
            self.execute(plan, batch_id="partial", checkpoint=fail)
        self.assertTrue(all(path.is_symlink() for path in self.targets))
        inverse = self.batch.plan_compensation("partial")
        self.assertEqual([child["kind"] for child in inverse["children"]], ["compensate", "apply"])
        with self.assertRaises(LifecycleError):
            self.batch.apply(inverse, approve_plan_id=plan["plan_id"])
        self.targets[1].unlink()
        self.targets[1].write_text("foreign")
        with self.assertRaises(LifecycleError):
            self.execute(inverse, batch_id="inverse")
        self.assertEqual(self.targets[1].read_text(), "foreign")
        self.assertNotEqual(self.batch.inspect("partial")["state"], "compensated")
        self.assertEqual(len(self.manager.repository.list("receipt")), 1)

    def test_completed_uninstall_inverse_creates_new_retained_identity_without_source(self):
        install = self.install_plans()[0]
        original = self.manager.apply(install, approve_plan_id=install["plan_id"])
        plan = self.batch.plan([self.manager.plan("uninstall", installation_id=original["installation_id"])])
        self.execute(plan, batch_id="uninstalled")
        shutil.rmtree(self.sources[0])
        inverse = self.batch.plan_compensation("uninstalled")
        self.assertEqual(inverse["children"][0]["plan"]["operation"], "install-retained")
        self.assertNotEqual(inverse["children"][0]["plan"]["installation_id"], original["installation_id"])
        self.execute(inverse)
        self.assertEqual(self.batch.inspect("uninstalled")["state"], "compensated")
        self.assertEqual(self.manager.get_installation(original["installation_id"])["state"], "uninstalled")
        self.assertEqual((self.targets[0] / "payload").read_text(), "H1")

    def test_empty_effect_cancellation_and_unsupported_inverse_are_honest(self):
        plan = self.batch.plan(self.install_plans())
        def fail(name, data):
            if name == "batch:prepared":
                raise OSError("stopped before any child")
        with self.assertRaises(LifecycleError):
            self.execute(plan, batch_id="empty", checkpoint=fail)
        inverse = self.batch.plan_compensation("empty")
        self.assertEqual(inverse["children"], [])
        self.execute(inverse)
        self.assertEqual(self.batch.inspect("empty")["state"], "compensated")
        self.assertEqual(self.manager.repository.list("receipt"), [])
        install = self.install_plans()[0]
        receipt = self.manager.apply(install, approve_plan_id=install["plan_id"])
        disable = self.manager.plan("disable", installation_id=receipt["installation_id"])
        self.manager.apply(disable, approve_plan_id=disable["plan_id"])
        uninstall = self.batch.plan([self.manager.plan("uninstall", installation_id=receipt["installation_id"])])
        self.execute(uninstall, batch_id="unsupported")
        inverse = self.batch.plan_compensation("unsupported")
        self.assertEqual(inverse["children"], [])
        self.assertTrue(inverse["uncompensated"])
        result = self.execute(inverse)
        self.assertTrue(result["uncompensated"])
        self.assertEqual(self.batch.inspect("unsupported")["state"], "recovery_needed")
        self.assertFalse(self.targets[0].is_symlink())

    def test_every_target_lock_is_held_across_children_even_for_another_project(self):
        other_root = self.root / "other-project"
        other_root.mkdir()
        other = Manager(other_root)
        self.addCleanup(other.repository.close)
        competing = other.plan("install", source=self.sources[1], target=self.targets[1])
        script = """
import json,sys
from skills_auditor.lifecycle.engine import Manager
from skills_auditor.lifecycle.common import LifecycleError
plan=json.loads(sys.argv[2])
try: Manager(sys.argv[1]).apply(plan,approve_plan_id=plan['plan_id'])
except LifecycleError as error:
 print(error.code);sys.exit(3)
"""
        attempts = []
        def compete(name, value):
            if name == "batch:child:0:started":
                attempts.append(subprocess.run([sys.executable, "-c", script, str(other_root), json.dumps(competing)], capture_output=True, text=True, timeout=10))
        self.execute(self.batch.plan(self.install_plans()), checkpoint=compete)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0].returncode, 3, attempts[0].stderr)
        self.assertEqual(attempts[0].stdout.strip(), "lock_contended")
        self.assertEqual(other.repository.list("transaction"), [])

    def test_parent_failure_and_failure_journal_error_keep_primary_and_children(self):
        plan = self.batch.plan(self.install_plans())
        original = self.manager.repository.put
        def fail_write(kind, identifier, data, **kwargs):
            if kind == "batch" and data["state"] == "recovery_needed":
                raise OSError("secondary parent persistence failure")
            return original(kind, identifier, data, **kwargs)
        def fail(name, value):
            if name == "batch:child:0:recorded":
                raise OSError("primary boundary failure")
        with patch.object(self.manager.repository, "put", side_effect=fail_write):
            with self.assertRaises(LifecycleError) as caught:
                self.execute(plan, batch_id="journal-failure", checkpoint=fail)
        self.assertEqual(caught.exception.code, "batch_journal_failed")
        self.assertEqual(caught.exception.details["batch_id"], "journal-failure")
        self.assertIn("primary boundary failure", str(caught.exception.details))
        self.assertIn("secondary parent persistence failure", str(caught.exception.details))
        self.assertTrue(self.targets[0].is_symlink())
        self.assertFalse(self.targets[1].is_symlink())
        self.assertEqual(len(self.manager.repository.list("receipt")), 1)
        self.assertEqual(self.manager.repository.list("batch-receipt"), [])
        self.assertEqual(self.batch.recover("journal-failure", mode="resume", approve_plan_id=plan["plan_id"])["status"], "completed")

    def test_initial_parent_intent_failure_has_no_child_or_filesystem_effects(self):
        plan = self.batch.plan(self.install_plans())
        with patch.object(self.batch, "_put", side_effect=OSError("parent intent unavailable")):
            with self.assertRaises(LifecycleError) as caught:
                self.execute(plan, batch_id="unrecorded")
        self.assertEqual(caught.exception.code, "batch_journal_failed")
        self.assertFalse(any(target.is_symlink() for target in self.targets))
        for kind in ("transaction", "batch", "receipt", "batch-receipt"):
            self.assertEqual(self.manager.repository.list(kind), [])

    def test_unreadable_commit_state_preserves_primary_as_uncertain_and_resumes(self):
        plan = self.batch.plan(self.install_plans())
        original = self.manager.repository.get
        armed = {"value": False}
        def unreadable(kind, identifier):
            if armed["value"] and kind == "batch":
                raise OSError("journal read unavailable")
            return original(kind, identifier)
        def fail(name, value):
            if name == "batch:child:0:recorded":
                armed["value"] = True
                raise OSError("original execution failure")
        with patch.object(self.manager.repository, "get", side_effect=unreadable):
            with self.assertRaises(LifecycleError) as caught:
                self.execute(plan, batch_id="read-uncertain", checkpoint=fail)
        self.assertEqual(caught.exception.code, "batch_journal_failed")
        self.assertEqual(caught.exception.details["batch_id"], "read-uncertain")
        self.assertIn("original execution failure", str(caught.exception.details))
        self.assertIn("journal read unavailable", str(caught.exception.details))
        inode = self.targets[0].lstat().st_ino
        self.assertFalse(self.targets[1].is_symlink())
        self.assertEqual(self.manager.repository.list("batch-receipt"), [])
        self.batch.recover("read-uncertain", mode="resume", approve_plan_id=plan["plan_id"])
        self.assertEqual(self.targets[0].lstat().st_ino, inode)
        self.assertEqual(len(self.manager.repository.list("receipt")), 2)

    def test_terminal_child_proof_checks_durable_step_completion_not_only_state_label(self):
        plan = self.batch.plan(self.install_plans())
        receipt = self.execute(plan, batch_id="terminal-steps")
        identifier = receipt["children"][0]["transaction_id"]
        record = self.manager.repository.get("transaction", identifier)
        changed = record["data"]
        changed["steps"][0]["state"] = "intent"
        self.manager.repository.put("transaction", identifier, changed, expected_revision=record["revision"])
        child_receipt = self.manager.repository.get("receipt", changed["receipt_id"])
        self.manager.repository.put("receipt", changed["receipt_id"], {**child_receipt["data"], "steps": changed["steps"]}, expected_revision=child_receipt["revision"])
        with self.assertRaises(LifecycleError):
            self.batch.inspect("terminal-steps")

    def test_inverse_revoke_requires_a_fresh_grant_and_fences_later_lifecycle_changes(self):
        install = self.install_plans()[0]
        original = self.manager.apply(install, approve_plan_id=install["plan_id"])
        plan = self.batch.plan([self.manager.plan("revoke", installation_id=original["installation_id"])])
        self.execute(plan, batch_id="denial")
        inverse = self.batch.plan_compensation("denial")
        self.assertEqual(inverse["children"][0]["plan"]["operation"], "renew")
        self.execute(inverse, batch_id="fresh-approval")
        current = self.manager.get_installation(original["installation_id"])
        self.assertNotEqual(current["authorization"]["grant_id"], original["grant_id"])
        self.assertEqual(self.manager.repository.get("authorization", original["grant_id"])["data"]["state"], "revoked")
        moved = self.batch.plan([self.manager.plan("rename", installation_id=original["installation_id"], name="batch-name")])
        self.execute(moved, batch_id="rename-original")
        inverse = self.batch.plan_compensation("rename-original")
        newer = self.manager.plan("rename", installation_id=original["installation_id"], name="later-name")
        self.manager.apply(newer, approve_plan_id=newer["plan_id"])
        inverse["children"][0]["plan"] = self.manager.plan("rename", installation_id=original["installation_id"], name=current["name"])
        inverse["plan_id"] = digest({key: value for key, value in inverse.items() if key != "plan_id"})
        with self.assertRaises(LifecycleError):
            self.execute(inverse)
        self.assertEqual(self.manager.get_installation(original["installation_id"])["name"], "later-name")

    def test_target_candidate_overlap_across_children_is_rejected(self):
        first = self.manager.plan("install", source=self.sources[0], target=self.sources[1] / "nested")
        second = self.manager.plan("install", source=self.sources[1], target=self.targets[1])
        with self.assertRaises(LifecycleError):
            self.batch.plan([first, second])
        self.assertFalse((self.sources[1] / "nested").exists())

    def test_real_inverse_operation_matrix_restores_owned_state_with_new_authorization(self):
        cases = [(operation, None) for operation in ("update", "edit", "rollback", "move", "rename", "disable", "archive", "renew", "revoke")]
        cases.extend([("enable", "disable"), ("enable", "archive")])
        for operation, preparation in cases:
            with self.subTest(operation=operation, preparation=preparation), tempfile.TemporaryDirectory(dir=self.root) as directory:
                root = Path(directory)
                manager = Manager(root)
                self.addCleanup(manager.repository.close)
                source = root / "candidate"
                source.mkdir()
                (source / "SKILL.md").write_text("# Example\n")
                (source / "payload").write_text("H1")
                target = root / "installed"
                first = manager.plan("install", source=source, target=target, name="original-name")
                manager.apply(first, approve_plan_id=first["plan_id"])
                identifier = first["installation_id"]
                if operation == "rollback":
                    (source / "payload").write_text("H2")
                    prepare = manager.plan("update", installation_id=identifier, source=source)
                    manager.apply(prepare, approve_plan_id=prepare["plan_id"])
                if preparation:
                    prepare = manager.plan(preparation, installation_id=identifier)
                    manager.apply(prepare, approve_plan_id=prepare["plan_id"])
                before = manager.get_installation(identifier)
                prior_receipt = manager.repository.get("receipt", before["receipt_id"])
                prior_grant = manager.repository.get("grant", before["authorization"]["grant_id"])
                prior_payload = (Path(manager._version(before["version_id"])["snapshot"]["path"]) / "payload").read_text()
                options = {}
                if operation in {"update", "edit"}:
                    (source / "payload").write_text("H2")
                    options["source"] = source
                elif operation == "rollback":
                    options["version_id"] = first["version"]["version_id"]
                elif operation == "move":
                    options["target"] = root / "moved"
                elif operation == "rename":
                    options["name"] = "changed-name"
                batch = BatchManager(manager)
                plan = batch.plan([manager.plan(operation, installation_id=identifier, **options)])
                batch.apply(plan, approve_plan_id=plan["plan_id"], batch_id="forward")
                inverse = batch.plan_compensation("forward")
                self.assertFalse(inverse["uncompensated"])
                batch.apply(inverse, approve_plan_id=inverse["plan_id"], batch_id="inverse")
                restored = manager.get_installation(identifier)
                for key in ("installation_id", "skill_id", "target", "state", "name", "version_id"):
                    self.assertEqual(restored[key], before[key], key)
                self.assertEqual(manager.repository.get("receipt", prior_receipt["id"]), prior_receipt)
                self.assertEqual(manager.repository.get("grant", prior_grant["id"]), prior_grant)
                self.assertGreater(restored["generation"], before["generation"])
                if restored["state"] == "active":
                    self.assertEqual((target / "payload").read_text(), prior_payload)
                    self.assertNotEqual(restored["authorization"]["grant_id"], before["authorization"]["grant_id"])
                    self.assertTrue(manager.verify(identifier)["valid"])
                else:
                    self.assertFalse(target.is_symlink())
                if operation == "move":
                    self.assertFalse((root / "moved").is_symlink())
                self.assertEqual(batch.inspect("forward")["state"], "compensated")

    def test_inverse_of_renewed_invalidated_approval_preserves_denial_explicitly(self):
        plan = self.install_plans()[0]
        receipt = self.manager.apply(plan, approve_plan_id=plan["plan_id"])
        pointer = os.readlink(self.targets[0])
        self.targets[0].unlink()
        self.manager.verify(receipt["installation_id"])
        self.targets[0].symlink_to(pointer, target_is_directory=True)
        renewal = self.batch.plan([self.manager.plan("renew", installation_id=receipt["installation_id"])])
        self.execute(renewal, batch_id="renewed-denial")
        inverse = self.batch.plan_compensation("renewed-denial")
        self.assertEqual(inverse["children"][0]["plan"]["operation"], "revoke")
        self.execute(inverse)
        self.assertEqual(self.manager.get_installation(receipt["installation_id"])["authorization"]["state"], "revoked")
        self.assertEqual(self.manager.repository.get("authorization", receipt["grant_id"])["data"]["state"], "invalidated")
        self.assertEqual((self.targets[0] / "payload").read_text(), "H1")
