"""Write-ahead recovery never mistakes a partial installation for success."""

import os
import json
import subprocess
import sys
import selectors
from unittest.mock import patch

from skills_auditor.lifecycle.common import LifecycleError
from skills_auditor.lifecycle.engine import Manager
from test_lifecycle_engine import LifecycleFixture


class TestLifecycleRecovery(LifecycleFixture):
    def test_no_effects_cancellation_preserves_damage_denial_and_unblocks_repair(self):
        _, installed = self.install()
        pending = self.manager.plan("renew", installation_id=installed["installation_id"])
        def fail(name, value):
            if name == "transaction:prepared":
                raise OSError("no effects yet")
        with self.assertRaises(LifecycleError):
            self.manager.apply(pending, approve_plan_id=pending["plan_id"], transaction_id="cancel-damaged", checkpoint=fail)
        payload = self.target / "payload"
        mode = payload.stat().st_mode & 0o777
        payload.chmod(mode | 0o200)
        payload.write_text("DAMAGED H1")
        payload.chmod(mode)
        self.assertFalse(self.manager.verify(installed["installation_id"])["valid"])
        inode = self.target.lstat().st_ino
        cancelled = self.manager.recover("cancel-damaged", mode="compensate", approve_plan_id=pending["plan_id"])
        self.assertEqual(cancelled["state"], "compensated")
        self.assertEqual(self.target.lstat().st_ino, inode)
        self.assertEqual(self.manager.get_installation(installed["installation_id"])["authorization"]["state"], "invalidated")
        self.assertEqual(len(self.manager.repository.list("receipt")), 1)
        (self.source / "payload").write_text("H2")
        repaired = self.approve("update", installed["installation_id"], source=self.source)
        self.assertTrue(self.manager.verify(installed["installation_id"])["valid"])
        self.assertNotEqual(repaired["grant_id"], installed["grant_id"])
        self.assertEqual((self.target / "payload").read_text(), "H2")

    def test_no_effects_cancellation_itself_records_newly_observed_damage(self):
        _, installed = self.install()
        pending = self.manager.plan("renew", installation_id=installed["installation_id"])
        def fail(name, value):
            if name == "transaction:prepared":
                raise OSError("no effects yet")
        with self.assertRaises(LifecycleError):
            self.manager.apply(pending, approve_plan_id=pending["plan_id"], transaction_id="cancel-observes", checkpoint=fail)
        payload = self.target / "payload"
        mode = payload.stat().st_mode & 0o777
        payload.chmod(mode | 0o200)
        payload.write_text("DAMAGED H1")
        payload.chmod(mode)
        self.assertEqual(self.manager.recover("cancel-observes", mode="compensate", approve_plan_id=pending["plan_id"])["state"], "compensated")
        authorization = self.manager.get_installation(installed["installation_id"])["authorization"]
        self.assertEqual(authorization["state"], "invalidated")
        self.assertIn("snapshot_tree", authorization["reason_codes"])
        self.assertEqual(authorization["grant_id"], installed["grant_id"])

    def test_actual_restoration_still_refuses_damaged_retained_bytes(self):
        from pathlib import Path
        _, installed = self.install()
        old_payload = Path(self.manager.repository.get("version", installed["version_id"])["data"]["snapshot"]["path"]) / "payload"
        (self.source / "payload").write_text("H2")
        pending = self.manager.plan("update", installation_id=installed["installation_id"], source=self.source)
        def fail(name, value):
            if name == "step:0:effect":
                raise OSError("H2 pointer already switched")
        with self.assertRaises(LifecycleError):
            self.manager.apply(pending, approve_plan_id=pending["plan_id"], transaction_id="restore-damaged", checkpoint=fail)
        mode = old_payload.stat().st_mode & 0o777
        old_payload.chmod(mode | 0o200)
        old_payload.write_text("DAMAGED H1")
        old_payload.chmod(mode)
        inode = self.target.lstat().st_ino
        with self.assertRaises(LifecycleError):
            self.manager.recover("restore-damaged", mode="compensate", approve_plan_id=pending["plan_id"])
        self.assertEqual(self.target.lstat().st_ino, inode)
        self.assertEqual((self.target / "payload").read_text(), "H2")
        self.assertEqual(self.manager.inspect_transaction("restore-damaged")["state"], "recovery_needed")
        self.assertEqual(len(self.manager.repository.list("receipt")), 1)

    def test_pending_transaction_fences_new_mutation_until_explicit_recovery(self):
        _, installed = self.install()
        pending = self.manager.plan("renew", installation_id=installed["installation_id"])
        def fail(name, value):
            if name == "transaction:prepared":
                raise OSError("prepared interruption")
        with self.assertRaises(LifecycleError):
            self.manager.apply(pending, approve_plan_id=pending["plan_id"], transaction_id="pending-original", checkpoint=fail)
        later = self.manager.plan("renew", installation_id=installed["installation_id"])
        with self.assertRaises(LifecycleError) as caught:
            self.manager.apply(later, approve_plan_id=later["plan_id"], transaction_id="later-operation")
        self.assertEqual(caught.exception.code, "pending_transaction")
        self.assertEqual(caught.exception.details["transaction_id"], "pending-original")
        self.assertIsNone(self.manager.repository.get("transaction", "later-operation"))
        self.assertEqual(len(self.manager.repository.list("receipt")), 1)
        self.assertEqual(self.manager.recover("pending-original", mode="compensate", approve_plan_id=pending["plan_id"])["state"], "compensated")
        result = self.manager.apply(later, approve_plan_id=later["plan_id"], transaction_id="later-operation")
        self.assertEqual(result["status"], "completed")

    def test_pending_target_is_reserved_even_before_installation_record_exists(self):
        pending = self.manager.plan("install", source=self.source, target=self.target)
        def fail(name, value):
            if name == "transaction:prepared":
                raise OSError("prepared interruption")
        with self.assertRaises(LifecycleError):
            self.manager.apply(pending, approve_plan_id=pending["plan_id"], transaction_id="pending-install", checkpoint=fail)
        later = self.manager.plan("install", source=self.source, target=self.target)
        self.assertNotEqual(pending["installation_id"], later["installation_id"])
        with self.assertRaises(LifecycleError) as caught:
            self.manager.apply(later, approve_plan_id=later["plan_id"])
        self.assertEqual(caught.exception.code, "pending_transaction")
        self.assertFalse(self.target.is_symlink())
        self.assertEqual(self.manager.repository.list("receipt"), [])
        self.assertEqual(self.manager.recover("pending-install", mode="resume", approve_plan_id=pending["plan_id"])["status"], "completed")

    def test_explicit_revoke_during_pending_work_allows_only_owned_compensation(self):
        _, installed = self.install()
        (self.source / "payload").write_text("H2")
        pending = self.manager.plan("update", source=self.source, installation_id=installed["installation_id"])
        def fail(name, value):
            if name == "step:0:effect":
                raise OSError("pointer switched without publication")
        with self.assertRaises(LifecycleError):
            self.manager.apply(pending, approve_plan_id=pending["plan_id"], transaction_id="pending-update", checkpoint=fail)
        revoke = self.manager.plan("revoke", installation_id=installed["installation_id"])
        revoked = self.manager.apply(revoke, approve_plan_id=revoke["plan_id"])
        before = self.manager.get_installation(installed["installation_id"])
        with self.assertRaises(LifecycleError):
            self.manager.recover("pending-update", mode="resume", approve_plan_id=pending["plan_id"])
        recovered = self.manager.recover("pending-update", mode="compensate", approve_plan_id=pending["plan_id"])
        self.assertEqual(recovered["state"], "compensated")
        self.assertEqual(self.manager.get_installation(installed["installation_id"]), before)
        self.assertEqual(before["authorization"]["state"], "revoked")
        self.assertEqual(before["receipt_id"], revoked["receipt_id"])
        self.assertEqual((self.target / "payload").read_text(), "H1")
        self.assertEqual(len(self.manager.repository.list("receipt")), 2)

    def test_unreadable_commit_probe_retains_primary_and_can_resume_owned_effect(self):
        plan = self.manager.plan("install", source=self.source, target=self.target)
        original = self.manager.repository.get
        armed = {"value": False}
        def unreadable(kind, identifier):
            if armed["value"] and kind == "transaction":
                raise OSError("transaction journal read unavailable")
            return original(kind, identifier)
        def fail(name, value):
            if name == "step:0:effect":
                armed["value"] = True
                raise OSError("original filesystem boundary failure")
        with patch.object(self.manager.repository, "get", side_effect=unreadable):
            with self.assertRaises(LifecycleError) as caught:
                self.manager.apply(plan, approve_plan_id=plan["plan_id"], transaction_id="uncertain-read", checkpoint=fail)
        self.assertEqual(caught.exception.code, "journal_write_failed")
        self.assertIn("original filesystem boundary failure", str(caught.exception.details))
        self.assertIn("transaction journal read unavailable", str(caught.exception.details))
        self.assertEqual(caught.exception.details["transaction_id"], "uncertain-read")
        inode = self.target.lstat().st_ino
        self.assertEqual(self.manager.repository.list("receipt"), [])
        receipt = self.manager.recover("uncertain-read", mode="resume", approve_plan_id=plan["plan_id"])
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(self.target.lstat().st_ino, inode)

    def test_second_move_step_failure_preserves_partial_evidence_and_can_resume(self):
        _, receipt = self.install()
        destination = self.host / "destination"
        plan = self.manager.plan("move", installation_id=receipt["installation_id"], target=destination)

        def fail(name, transaction):
            if name == "step:1:intent":
                raise OSError("injected source removal failure")

        with self.assertRaises(LifecycleError) as caught:
            self.manager.apply(plan, approve_plan_id=plan["plan_id"], transaction_id="move-partial", checkpoint=fail)
        self.assertEqual(caught.exception.code, "transaction_failed")
        self.assertTrue(self.target.is_symlink())
        self.assertTrue(destination.is_symlink())
        tx = self.manager.inspect_transaction("move-partial")
        self.assertEqual(tx["state"], "recovery_needed")
        self.assertEqual(tx["steps"][0]["state"], "completed")
        self.assertIsNone(tx.get("receipt_id"))
        with self.assertRaises(LifecycleError):
            self.manager.apply(plan, approve_plan_id=plan["plan_id"], transaction_id="move-partial")
        final = Manager(self.root).recover("move-partial", mode="resume", approve_plan_id=plan["plan_id"])
        self.assertEqual(final["status"], "completed")
        self.assertFalse(self.target.is_symlink())
        self.assertTrue(destination.is_symlink())

    def test_effect_before_completion_is_reconciled_without_repeating_replace(self):
        plan = self.manager.plan("install", source=self.source, target=self.target)

        def fail(name, transaction):
            if name == "step:0:effect":
                raise OSError("interrupted after pointer replacement")

        with self.assertRaises(LifecycleError):
            self.manager.apply(plan, approve_plan_id=plan["plan_id"], transaction_id="uncertain", checkpoint=fail)
        self.assertTrue(self.target.is_symlink())
        with patch("skills_auditor.lifecycle.engine.os.replace") as replace:
            result = self.manager.recover("uncertain", mode="resume", approve_plan_id=plan["plan_id"])
        replace.assert_not_called()
        self.assertEqual(result["status"], "completed")

    def test_compensation_restores_owned_pointer_and_preserves_foreign_occupancy(self):
        _, initial = self.install()
        old_link = os.readlink(self.target)
        (self.source / "payload").write_text("H2")
        plan = self.manager.plan("update", source=self.source, installation_id=initial["installation_id"])

        def fail(name, transaction):
            if name == "step:0:effect":
                raise OSError("interrupt")

        with self.assertRaises(LifecycleError):
            self.manager.apply(plan, approve_plan_id=plan["plan_id"], transaction_id="compensate", checkpoint=fail)
        result = self.manager.recover("compensate", mode="compensate", approve_plan_id=plan["plan_id"])
        self.assertEqual(result["state"], "compensated")
        self.assertEqual(os.readlink(self.target), old_link)
        self.assertEqual((self.target / "payload").read_text(), "H1")
        self.assertIsNone(result.get("receipt_id"))

        newer = self.manager.plan("update", source=self.source, installation_id=initial["installation_id"])
        with self.assertRaises(LifecycleError):
            self.manager.apply(newer, approve_plan_id=newer["plan_id"], transaction_id="foreign", checkpoint=fail)
        self.target.unlink()
        self.target.write_text("third-party")
        with self.assertRaises(LifecycleError) as caught:
            self.manager.recover("foreign", mode="compensate", approve_plan_id=newer["plan_id"])
        self.assertEqual(caught.exception.code, "recovery_conflict")
        self.assertEqual(self.target.read_text(), "third-party")
        tx = self.manager.inspect_transaction("foreign")
        self.assertIn("interrupt", tx["error"]["message"])
        self.assertTrue(tx["recovery_errors"])
        self.assertIsNone(tx.get("receipt_id"))

    def test_recovery_uses_recorded_snapshot_not_a_changed_candidate(self):
        _, initial = self.install()

        def fail(name, transaction):
            if name == "step:0:effect":
                raise OSError("interrupt")

        for mode in ("compensate", "resume"):
            with self.subTest(mode=mode):
                (self.source / "payload").write_text("H2")
                plan = self.manager.plan("update", source=self.source, installation_id=initial["installation_id"])
                with self.assertRaises(LifecycleError):
                    self.manager.apply(plan, approve_plan_id=plan["plan_id"], transaction_id=mode, checkpoint=fail)
                (self.source / "payload").write_text("H3")
                result = self.manager.recover(mode, mode=mode, approve_plan_id=plan["plan_id"])
                self.assertEqual((self.target / "payload").read_text(), "H1" if mode == "compensate" else "H2")
                self.assertEqual(result.get("state", result.get("status")), "compensated" if mode == "compensate" else "completed")

    def test_real_process_death_at_durable_boundaries_and_explicit_resume(self):
        boundaries = ("transaction:prepared", "transaction:staged", "step:0:intent",
                      "step:0:staged", "step:0:effect", "step:0:completed",
                      "transaction:before_commit", "transaction:committed")
        code = """
import json, os, sys
from skills_auditor.lifecycle.engine import Manager
plan = json.loads(sys.argv[2])
def die(name, transaction):
    if name == sys.argv[3]:
        os._exit(73)
Manager(sys.argv[1]).apply(plan, approve_plan_id=plan['plan_id'], transaction_id=sys.argv[4], checkpoint=die)
"""
        for index, boundary in enumerate(boundaries):
            with self.subTest(boundary=boundary):
                target = self.host / ("crash-" + str(index))
                plan = self.manager.plan("install", source=self.source, target=target)
                transaction_id = "crash-" + str(index)
                result = subprocess.run([sys.executable, "-c", code, str(self.root), json.dumps(plan), boundary, transaction_id], capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 73, result.stderr)
                tx = self.manager.inspect_transaction(transaction_id)
                self.assertEqual(tx["plan"]["plan_id"], plan["plan_id"])
                if target.is_symlink():
                    self.assertEqual((target / "payload").read_text(), "H1")
                if boundary != "transaction:committed":
                    self.assertIsNone(tx["receipt_id"])
                receipt = Manager(self.root).recover(transaction_id, mode="resume", approve_plan_id=plan["plan_id"])
                self.assertEqual(receipt["status"], "completed")
                self.assertEqual((target / "payload").read_text(), "H1")
                self.assertFalse(list(self.host.glob(".skills-auditor-tx-" + transaction_id + "-*")))

    def test_compensating_staged_but_unpublished_link_cleans_only_owned_staging(self):
        plan = self.manager.plan("install", source=self.source, target=self.target)

        def fail(name, transaction):
            if name == "step:0:staged":
                raise OSError("staged but unpublished")

        with self.assertRaises(LifecycleError):
            self.manager.apply(plan, approve_plan_id=plan["plan_id"], transaction_id="staged", checkpoint=fail)
        stage = self.host / ".skills-auditor-tx-staged-0"
        self.assertTrue(stage.is_symlink())
        self.assertFalse(self.target.is_symlink())
        tx = self.manager.recover("staged", mode="compensate", approve_plan_id=plan["plan_id"])
        self.assertEqual(tx["state"], "compensated")
        self.assertFalse(stage.is_symlink())
        self.assertFalse(self.target.exists())

    def test_final_registry_write_failure_rolls_back_receipt_but_not_pointer(self):
        plan = self.manager.plan("install", source=self.source, target=self.target)
        original = self.manager.repository.put

        def fail(kind, identifier, data, **kwargs):
            if kind == "receipt":
                raise OSError("receipt persistence failed")
            return original(kind, identifier, data, **kwargs)

        with patch.object(self.manager.repository, "put", side_effect=fail):
            with self.assertRaises(LifecycleError):
                self.manager.apply(plan, approve_plan_id=plan["plan_id"], transaction_id="receipt-failed")
        self.assertTrue(self.target.is_symlink())
        self.assertIsNone(self.manager.repository.get("installation", plan["installation_id"]))
        self.assertEqual(self.manager.repository.list("receipt"), [])
        self.assertEqual(self.manager.repository.list("grant"), [])
        tx = self.manager.inspect_transaction("receipt-failed")
        self.assertEqual(tx["state"], "recovery_needed")
        self.assertIsNone(tx["receipt_id"])
        self.assertIn("receipt persistence failed", tx["error"]["message"])
        final = self.manager.recover("receipt-failed", mode="resume", approve_plan_id=plan["plan_id"])
        self.assertEqual(final["status"], "completed")

    def test_foreign_change_before_final_commit_cannot_publish_success(self):
        plan = self.manager.plan("install", source=self.source, target=self.target)

        def intervene(name, transaction):
            if name == "transaction:before_commit":
                self.target.unlink()
                self.target.write_text("foreign")

        with self.assertRaises(LifecycleError):
            self.manager.apply(plan, approve_plan_id=plan["plan_id"], transaction_id="final-race", checkpoint=intervene)
        self.assertEqual(self.target.read_text(), "foreign")
        self.assertEqual(self.manager.repository.list("receipt"), [])
        self.assertEqual(self.manager.repository.list("grant"), [])
        self.assertEqual(self.manager.inspect_transaction("final-race")["state"], "recovery_needed")

    def test_two_processes_and_distinct_stores_serialize_one_shared_target(self):
        holder_code = """
import json, sys
from skills_auditor.lifecycle.engine import Manager
plan = json.loads(sys.argv[2])
def hold(name, transaction):
    if name == 'transaction:prepared':
        print('locked', flush=True)
        sys.stdin.readline()
Manager(sys.argv[1]).apply(plan, approve_plan_id=plan['plan_id'], transaction_id='holder', checkpoint=hold)
"""
        contender_code = """
import json, sys
from skills_auditor.lifecycle.engine import Manager
from skills_auditor.lifecycle.common import LifecycleError
plan = json.loads(sys.argv[2])
try:
    Manager(sys.argv[1]).apply(plan, approve_plan_id=plan['plan_id'], transaction_id='contender')
except LifecycleError as error:
    print(error.code)
    sys.exit(3)
"""
        for other_store in (False, True):
            with self.subTest(other_store=other_store):
                first_project = self.root / ("first-" + str(other_store))
                first_project.mkdir()
                first = Manager(first_project)
                self.addCleanup(first.repository.close)
                project = self.root / ("other-" + str(other_store)) if other_store else first_project
                project.mkdir(exist_ok=True)
                contender = Manager(project)
                self.addCleanup(contender.repository.close)
                target = self.host / ("shared-" + str(other_store))
                holder_plan = first.plan("install", source=self.source, target=target)
                contender_plan = contender.plan("install", source=self.source, target=target)
                process = subprocess.Popen([sys.executable, "-c", holder_code, str(first_project), json.dumps(holder_plan)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                self.addCleanup(lambda process=process: process.kill() if process.poll() is None else None)
                selector = selectors.DefaultSelector()
                self.addCleanup(selector.close)
                selector.register(process.stdout, selectors.EVENT_READ)
                self.assertTrue(selector.select(timeout=10), "holder failed to reach the durable lock boundary")
                self.assertEqual(process.stdout.readline().strip(), "locked")
                result = subprocess.run([sys.executable, "-c", contender_code, str(project), json.dumps(contender_plan)], capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 3, result.stderr)
                self.assertEqual(result.stdout.strip(), "lock_contended")
                self.assertFalse(target.is_symlink())
                stdout, stderr = process.communicate("continue\n", timeout=10)
                self.assertEqual(process.returncode, 0, stderr)
                self.assertEqual((target / "payload").read_text(), "H1")
                with self.assertRaises(LifecycleError) as caught:
                    contender.apply(contender_plan, approve_plan_id=contender_plan["plan_id"], transaction_id="contender")
                self.assertEqual(caught.exception.code, "stale_plan")

    def test_prepared_journal_failure_has_no_filesystem_effect(self):
        plan = self.manager.plan("install", source=self.source, target=self.target)
        with patch.object(self.manager.repository, "put", side_effect=OSError("journal unavailable")):
            with self.assertRaises(LifecycleError) as caught:
                self.manager.apply(plan, approve_plan_id=plan["plan_id"], transaction_id="no-intent")
        self.assertEqual(caught.exception.code, "journal_write_failed")
        self.assertFalse(self.target.is_symlink())
        self.assertIsNone(self.manager.repository.get("transaction", "no-intent"))
        self.assertEqual(self.manager.repository.list("receipt"), [])

    def test_failure_observation_allows_compensation_without_restoring_old_authority(self):
        _, first = self.install()
        (self.source / "payload").write_text("H2")
        plan = self.manager.plan("update", source=self.source, installation_id=first["installation_id"])

        def fail(name, transaction):
            if name == "step:0:effect":
                raise OSError("interrupted")

        with self.assertRaises(LifecycleError):
            self.manager.apply(plan, approve_plan_id=plan["plan_id"], transaction_id="observed-partial", checkpoint=fail)
        failed = self.manager.verify(first["installation_id"])
        self.assertEqual(failed["approval"]["state"], "invalidated")
        with self.assertRaises(LifecycleError):
            self.manager.recover("observed-partial", mode="resume", approve_plan_id=plan["plan_id"])
        tx = self.manager.recover("observed-partial", mode="compensate", approve_plan_id=plan["plan_id"])
        self.assertEqual(tx["state"], "compensated")
        self.assertEqual((self.target / "payload").read_text(), "H1")
        self.assertEqual(self.manager.verify(first["installation_id"])["approval"]["state"], "invalidated")
        self.approve("renew", first["installation_id"])
        self.assertTrue(self.manager.verify(first["installation_id"])["valid"])

    def test_noop_preaction_identity_race_is_rejected_without_rebuilding_pointer(self):
        _, receipt = self.install()
        plan = self.manager.plan("renew", installation_id=receipt["installation_id"])
        foreign_identity = []

        def replace_identity(name, transaction):
            if name == "step:0:intent":
                raw = os.readlink(self.target)
                self.target.unlink()
                self.target.symlink_to(raw, target_is_directory=True)
                foreign_identity.append(self.target.lstat().st_ino)

        with self.assertRaises(LifecycleError):
            self.manager.apply(plan, approve_plan_id=plan["plan_id"], transaction_id="noop-race", checkpoint=replace_identity)
        self.assertEqual(self.target.lstat().st_ino, foreign_identity[0])
        self.assertIsNone(self.manager.inspect_transaction("noop-race")["receipt_id"])

    def test_stage_name_collision_with_a_legal_owned_target_is_rejected_before_effects(self):
        target = self.host / ".skills-auditor-tx-move-fixed-0"
        first = self.manager.plan("install", source=self.source, target=target)
        receipt = self.manager.apply(first, approve_plan_id=first["plan_id"])
        original = target.lstat()
        plan = self.manager.plan("move", target=self.host / "destination", installation_id=receipt["installation_id"])
        with self.assertRaises(LifecycleError) as caught:
            self.manager.apply(plan, approve_plan_id=plan["plan_id"], transaction_id="move-fixed")
        self.assertEqual(caught.exception.code, "staging_conflict")
        self.assertEqual(target.lstat().st_ino, original.st_ino)
        self.assertFalse((self.host / "destination").is_symlink())
        self.assertIsNone(self.manager.repository.get("transaction", "move-fixed"))

    def test_staging_name_aliases_follow_casefold_and_unicode_lock_equivalence(self):
        for label, transaction_id in (("CASE", "case"), ("Case", "case")):
            with self.subTest(label=label):
                target = self.host / (".skills-auditor-tx-" + label + "-0")
                plan = self.manager.plan("install", source=self.source, target=target)
                with self.assertRaises(LifecycleError) as caught:
                    self.manager.apply(plan, approve_plan_id=plan["plan_id"], transaction_id=transaction_id)
                self.assertEqual(caught.exception.code, "staging_conflict")
                self.assertFalse(target.is_symlink())
                self.assertIsNone(self.manager.repository.get("transaction", transaction_id))

    def test_real_verification_process_death_leaves_old_grant_requires_reapproval(self):
        _, receipt = self.install()
        code = """
import os, sys
from skills_auditor.lifecycle.engine import Manager
def die(name, data):
    if name == 'verification:started':
        os._exit(73)
Manager(sys.argv[1]).verify(sys.argv[2], checkpoint=die)
"""
        result = subprocess.run([sys.executable, "-c", code, str(self.root), receipt["installation_id"]], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 73, result.stderr)
        observed = Manager(self.root).verify(receipt["installation_id"])
        self.assertEqual(observed["grant_id"], receipt["grant_id"])
        self.assertEqual(observed["approval"]["state"], "invalidated")
        self.assertIn("verification_interrupted", observed["approval"]["reason_codes"])
        self.assertEqual(len(self.manager.repository.list("receipt")), 1)
        self.approve("renew", receipt["installation_id"])
        self.assertTrue(self.manager.verify(receipt["installation_id"])["valid"])

    def test_authorization_commit_failure_cannot_erase_uncertain_observation(self):
        _, receipt = self.install()
        raw = os.readlink(self.target)
        self.target.unlink()
        original = self.manager.repository.put

        def fail(kind, identifier, data, **kwargs):
            if kind == "installation":
                raise LifecycleError("repository_io_error", "authorization commit failed")
            return original(kind, identifier, data, **kwargs)

        with patch.object(self.manager.repository, "put", side_effect=fail):
            with self.assertRaises(LifecycleError):
                self.manager.verify(receipt["installation_id"])
        self.target.symlink_to(raw, target_is_directory=True)
        observed = self.manager.verify(receipt["installation_id"])
        self.assertEqual(observed["approval"]["state"], "invalidated")
        self.assertIn("verification_interrupted", observed["approval"]["reason_codes"])

    def test_real_target_lock_contention_does_not_start_observation_or_revoke(self):
        _, receipt = self.install()
        self.manager.verify(receipt["installation_id"])
        previous = self.manager.repository.get("verification-run", receipt["installation_id"])
        code = """
import sys
from skills_auditor.lifecycle.locking import locked_paths
with locked_paths([sys.argv[1]]):
    print('locked', flush=True)
    sys.stdin.readline()
"""
        process = subprocess.Popen([sys.executable, "-c", code, str(self.target)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        selector = selectors.DefaultSelector()
        self.addCleanup(selector.close)
        selector.register(process.stdout, selectors.EVENT_READ)
        self.assertTrue(selector.select(timeout=10))
        self.assertEqual(process.stdout.readline().strip(), "locked")
        with self.assertRaises(LifecycleError) as caught:
            self.manager.verify(receipt["installation_id"])
        self.assertEqual(caught.exception.code, "lock_contended")
        self.assertEqual(self.manager.repository.get("verification-run", receipt["installation_id"]), previous)
        self.assertEqual(self.manager.get_installation(receipt["installation_id"])["authorization"]["state"], "valid")
        with self.assertRaises(LifecycleError) as caught:
            self.manager.plan("renew", installation_id=receipt["installation_id"])
        self.assertEqual(caught.exception.code, "lock_contended")
        self.assertEqual(self.manager.repository.get("verification-run", receipt["installation_id"]), previous)
        self.assertEqual(self.manager.get_installation(receipt["installation_id"])["authorization"]["state"], "valid")
        _, stderr = process.communicate("release\n", timeout=10)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertTrue(self.manager.verify(receipt["installation_id"])["valid"])
