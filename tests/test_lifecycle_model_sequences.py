"""S01–S04/S08: deterministic cross-object sequences, not isolated success tests.

Public APIs are action adapters. Expectations come from lifecycle_model's literal
transitions and explicit H1/H2 bytes. Crash scripts only use temporary projects;
production has no crash environment variable or implicit test behavior.
"""

import copy
from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from skills_auditor.integration import IntegrationSpec, IntegrationTarget, apply_integration_plan, build_integration_plan
from skills_auditor.lifecycle.batch import BatchManager
from skills_auditor.lifecycle.common import LifecycleError
from skills_auditor.lifecycle.engine import Manager
from skills_auditor.lifecycle.retention import apply_retention, plan_retention
from skills_auditor.lifecycle.status import preflight
from lifecycle_model import ModelOracle, TRANSITIONS, digest, expected_transition, normalized_tree, tree_state


@contextmanager
def fixture(case):
    with tempfile.TemporaryDirectory(prefix="skills-model-sequence-") as temporary:
        root = Path(temporary).resolve()
        source = root / "candidate"
        source.mkdir()
        (source / "SKILL.md").write_text("---\nname: model\ndescription: fixture\n---\n# Model\n")
        (source / "payload").write_text("H1")
        for name in ("host-a", "host-b", "host-c"):
            (root / name).mkdir()
        manager = Manager(root)
        try:
            oracle = ModelOracle(case, manager)
            oracle.model_trees = {}
            yield root, source, manager, oracle
        finally:
            manager.repository.close()


class TestLifecycleModelSequences(unittest.TestCase):
    def watch_after(self, oracle, plan):
        after = plan["after"]
        if plan["before"] and plan["before"]["target"] != after["target"]:
            oracle.watch_pointer(plan["before"]["target"], None)
        oracle.watch_pointer(after["target"], plan["version"]["snapshot"]["path"] if after["state"] == "active" else None)
        oracle.watch_tree(plan["version"]["snapshot"]["path"], oracle.model_trees[after["version_id"]])

    def action(self, oracle, manager, operation, *, installation_id=None, payload=None, **arguments):
        source_before = tree_state(arguments["source"]) if arguments.get("source") else None
        if source_before is not None:
            oracle.watch_tree(arguments["source"], source_before)
        before = oracle.check()["records"].get("installation", {}).get(installation_id)
        expected = expected_transition(before, operation)
        plan = manager.plan(operation, installation_id=installation_id, **arguments)
        if source_before is not None:
            oracle.model_trees[plan["version"]["version_id"]] = normalized_tree(source_before)
        saved_plan = copy.deepcopy(plan)
        oracle.check()
        receipt = manager.apply(plan, approve_plan_id=plan["plan_id"])
        self.watch_after(oracle, plan)
        self.assertEqual(plan, saved_plan, "execution rewrote the reviewed plan")
        current = oracle.expect_installation(receipt["installation_id"], state=expected["state"],
                                             authorization=expected["authorization"], generation=expected["generation"],
                                             target=plan["after"]["target"], version_id=plan["after"]["version_id"], payload=payload)
        if before:
            self.assertEqual(current["skill_id"], before["skill_id"])
            self.assertEqual(current["installation_id"], before["installation_id"])
            if expected["grant_policy"] == "new":
                self.assertNotEqual(receipt["grant_id"], before["authorization"]["grant_id"])
            else:
                self.assertEqual(receipt["grant_id"], before["authorization"]["grant_id"])
        if source_before is not None:
            self.assertEqual(tree_state(arguments["source"]), source_before)
        return plan, receipt

    def dual(self, root, source, manager, oracle):
        first_plan, first = self.action(oracle, manager, "install", source=source, target=root / "host-a" / "skill", payload="H1")
        _, second = self.action(oracle, manager, "install-retained", skill_id=first["skill_id"],
                                version_id=first["version_id"], target=root / "host-b" / "skill", payload="H1")
        self.assertNotEqual(first["installation_id"], second["installation_id"])
        self.assertNotEqual(first["grant_id"], second["grant_id"])
        return first_plan, first, second

    def saved_rollout(self, root, source, manager, first, second, *, oracle, alternate_source=False, operations=("update", "edit")):
        (source / "payload").write_text("H2")
        other = source
        if alternate_source:
            other = root / "another-candidate"
            shutil.copytree(source, other)
        plans = []
        for index, (receipt, candidate, operation) in enumerate(zip((first, second), (source, other), operations)):
            oracle.watch_tree(candidate, tree_state(candidate))
            with patch("skills_auditor.lifecycle.engine.utc_now", return_value="2026-09-07T00:00:0{}Z".format(index + 1)):
                plans.append(manager.plan(operation, installation_id=receipt["installation_id"], source=candidate))
            oracle.model_trees[plans[-1]["version"]["version_id"]] = normalized_tree(tree_state(candidate))
        self.assertEqual(plans[0]["version"]["version_id"], plans[1]["version"]["version_id"])
        self.assertNotEqual(plans[0]["created_at"], plans[1]["created_at"])
        return plans

    def crash(self, root, plan, boundary, *, batch=True, published=False):
        saved = root / "reviewed-plan.json"
        saved.write_text(json.dumps(plan))
        script = """
import json, os, sys
from pathlib import Path
from skills_auditor.lifecycle.engine import Manager
from skills_auditor.lifecycle.batch import BatchManager
from skills_auditor.lifecycle import engine
root, saved, boundary, batch, published = sys.argv[1:]
manager = Manager(root, create=False)
plan = json.loads(Path(saved).read_text())
if published == 'yes':
    original = engine.materialize
    def stopped(*args, **kwargs):
        result = original(*args, **kwargs)
        os._exit(74)
    engine.materialize = stopped
def stop(name, value):
    if name == boundary:
        os._exit(74)
if boundary.startswith('verification:'):
    manager.verify(plan['installation_id'], checkpoint=stop)
elif batch == 'yes':
    BatchManager(manager).apply(plan, approve_plan_id=plan['plan_id'], batch_id='crashed-rollout', checkpoint=stop)
else:
    manager.apply(plan, approve_plan_id=plan['plan_id'], transaction_id='crashed-core', checkpoint=stop)
raise RuntimeError('requested durable boundary was not reached')
"""
        result = subprocess.run([sys.executable, "-c", script, str(root), str(saved), boundary,
                                 "yes" if batch else "no", "yes" if published else "no"],
                                text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 74, result.stderr)

    def retarget_candidate(self, root, source, oracle):
        original = tree_state(source)
        replacement = root / (source.name + "-replacement")
        replacement.mkdir()
        (replacement / "SKILL.md").write_text("# H3 replacement")
        (replacement / "payload").write_text("H3")
        moved = root / (source.name + "-moved")
        source.rename(moved)
        oracle.watch_tree(moved, original)
        source.symlink_to(replacement, target_is_directory=True)
        oracle.watch_tree(source, tree_state(source))
        oracle.watch_tree(replacement, tree_state(replacement))

    def test_s01_all_fourteen_operations_follow_literal_state_and_history_model(self):
        with fixture(self) as (root, source, manager, oracle):
            seen = set()
            def perform(operation, **kwargs):
                seen.add(operation)
                return self.action(oracle, manager, operation, **kwargs)
            first_plan, receipt = perform("install", source=source, target=root / "host-a" / "skill", payload="H1")
            identifier = receipt["installation_id"]
            for operation, text in (("edit", "H2"), ("update", "H3")):
                (source / "payload").write_text(text)
                _, receipt = perform(operation, installation_id=identifier, source=source, payload=text)
            inode = (root / "host-a" / "skill").lstat().st_ino
            _, receipt = perform("renew", installation_id=identifier, payload="H3")
            self.assertEqual((root / "host-a" / "skill").lstat().st_ino, inode)
            perform("rename", installation_id=identifier, name="renamed", payload="H3")
            perform("move", installation_id=identifier, target=root / "host-b" / "skill", payload="H3")
            self.assertFalse(os.path.lexists(root / "host-a" / "skill"))
            for operation in ("disable", "enable", "archive", "enable"):
                perform(operation, installation_id=identifier, payload="H3" if operation == "enable" else None)
            perform("rollback", installation_id=identifier, version_id=first_plan["version"]["version_id"], payload="H1")
            perform("revoke", installation_id=identifier, payload="H1")
            perform("renew", installation_id=identifier, payload="H1")
            _, last = perform("uninstall", installation_id=identifier)
            _, replacement = perform("install-retained", version_id=last["version_id"], target=root / "host-b" / "skill", payload="H1")
            self.assertNotEqual(replacement["installation_id"], identifier)
            self.assertEqual(replacement["skill_id"], last["skill_id"])
            legacy_host = root / "legacy-host"
            legacy_host.mkdir()
            legacy_source = root / "legacy-sources" / "candidate"
            shutil.copytree(source, legacy_source)
            spec = IntegrationSpec(project_root=root, sources=(legacy_source.parent,), targets=(IntegrationTarget("fixture", root=legacy_host),))
            legacy_plan = build_integration_plan(spec)
            legacy_receipt, _ = apply_integration_plan(legacy_plan)
            historical_legacy = copy.deepcopy(legacy_receipt)
            perform("migrate", source=legacy_source, target=legacy_host / "model", legacy_receipt=legacy_receipt, payload="H3")
            self.assertEqual(legacy_receipt, historical_legacy)
            self.assertEqual(seen, set(TRANSITIONS))
            self.assertEqual(oracle.pending(), [])

    def test_s02_independent_same_skill_rollouts_share_facts_not_occurrence_metadata(self):
        for reverse, alternate, operations in ((False, False, ("update", "update")),
                                                (True, False, ("update", "edit")),
                                                (False, True, ("edit", "update")),
                                                (True, True, ("edit", "edit"))):
            with self.subTest(reverse=reverse, alternate=alternate, operations=operations), fixture(self) as (root, source, manager, oracle):
                _, first, second = self.dual(root, source, manager, oracle)
                plans = self.saved_rollout(root, source, manager, first, second, oracle=oracle, alternate_source=alternate, operations=operations)
                oracle.check()
                saved = copy.deepcopy(plans)
                selected = list(reversed(plans)) if reverse else plans
                batch = BatchManager(manager)
                parent = batch.plan(selected)
                receipt = batch.apply(parent, approve_plan_id=parent["plan_id"])
                for child in plans:
                    self.watch_after(oracle, child)
                self.assertEqual(receipt["status"], "completed")
                self.assertEqual(plans, saved)
                records = oracle.check()["records"]
                shared = records["version"][plans[0]["version"]["version_id"]]
                self.assertEqual(shared, selected[0]["version"], "first origin must remain immutable")
                for original, plan in zip((first, second), plans):
                    current = oracle.expect_installation(original["installation_id"], state="active", authorization="valid",
                                                         version_id=plan["version"]["version_id"], generation=2, payload="H2")
                    self.assertNotEqual(current["authorization"]["grant_id"], original["grant_id"])
                    tx = records["transaction"][current["last_transaction_id"]]
                    self.assertEqual(tx["plan"], plan)
                self.assertEqual(len(records["version"]), 2)
                self.assertEqual(oracle.pending(), [])

    def test_s02_saved_second_host_plan_survives_first_host_commit_outside_batch(self):
        with fixture(self) as (root, source, manager, oracle):
            _, first, second = self.dual(root, source, manager, oracle)
            plans = self.saved_rollout(root, source, manager, first, second, oracle=oracle, alternate_source=True)
            for plan in plans:
                manager.apply(plan, approve_plan_id=plan["plan_id"])
                self.watch_after(oracle, plan)
                oracle.check()
            for original in (first, second):
                oracle.expect_installation(original["installation_id"], state="active", authorization="valid", payload="H2")

    def test_s02_shared_version_preserves_two_different_adoption_parents(self):
        with fixture(self) as (root, source, manager, oracle):
            _, first, second = self.dual(root, source, manager, oracle)
            (source / "payload").write_text("H0")
            _, second = self.action(oracle, manager, "update", installation_id=second["installation_id"], source=source, payload="H0")
            plans = self.saved_rollout(root, source, manager, first, second, oracle=oracle, alternate_source=True)
            self.assertNotEqual(plans[0]["before"]["version_id"], plans[1]["before"]["version_id"])
            batch = BatchManager(manager)
            parent = batch.plan(plans)
            batch.apply(parent, approve_plan_id=parent["plan_id"])
            for child in plans:
                self.watch_after(oracle, child)
            for prior, plan in zip((first, second), plans):
                current = oracle.expect_installation(prior["installation_id"], state="active", authorization="valid", payload="H2")
                tx = oracle.check()["records"]["transaction"][current["last_transaction_id"]]
                self.assertEqual(tx["plan"]["before"]["version_id"], prior["version_id"])
                self.assertEqual(tx["plan"], plan)

    def test_s02_legacy_adoption_of_existing_shared_version_preserves_origin(self):
        with fixture(self) as (root, source, manager, oracle):
            _, first = self.action(oracle, manager, "install", source=source, target=root / "host-a" / "skill", payload="H1")
            legacy = root / "legacy-sources" / "model"
            shutil.copytree(source, legacy)
            spec = IntegrationSpec(project_root=root, sources=(legacy.parent,), targets=(IntegrationTarget("fixture", root=root / "host-b"),))
            receipt, _ = apply_integration_plan(build_integration_plan(spec))
            old_origin = oracle.check()["records"]["version"][first["version_id"]]
            plan = manager.plan("migrate", source=legacy, target=root / "host-b" / "model", skill_id=first["skill_id"], legacy_receipt=receipt)
            saved = copy.deepcopy(plan)
            adopted = manager.apply(plan, approve_plan_id=plan["plan_id"])
            self.watch_after(oracle, plan)
            records = oracle.check()["records"]
            self.assertEqual(records["version"][first["version_id"]], old_origin)
            self.assertEqual(records["transaction"][adopted["transaction_id"]]["plan"], saved)
            self.assertEqual(adopted["version_id"], first["version_id"])
            self.assertNotEqual(adopted["installation_id"], first["installation_id"])
            oracle.expect_installation(adopted["installation_id"], state="active", authorization="valid", payload="H1")

    def test_s02_shared_rollout_real_death_then_resume_or_explicit_compensation(self):
        for boundary, mode in (("batch:child:0:transaction:committed", "resume"),
                               ("batch:child:1:step:0:effect", "resume"),
                               ("batch:child:0:transaction:committed", "compensate")):
            with self.subTest(boundary=boundary, mode=mode), fixture(self) as (root, source, manager, oracle):
                _, first, second = self.dual(root, source, manager, oracle)
                plans = self.saved_rollout(root, source, manager, first, second, oracle=oracle, alternate_source=True)
                batch = BatchManager(manager)
                parent = batch.plan(plans)
                oracle.check()
                self.crash(root, parent, boundary)
                self.watch_after(oracle, plans[0])
                if boundary == "batch:child:1:step:0:effect":
                    self.watch_after(oracle, plans[1])
                interrupted = oracle.check()["records"]
                self.assertEqual(interrupted["installation"][first["installation_id"]]["generation"], 2)
                self.assertEqual(interrupted["installation"][second["installation_id"]]["generation"], 1)
                self.assertEqual(interrupted["installation"][second["installation_id"]]["authorization"]["grant_id"], second["grant_id"])
                self.assertEqual((root / "host-a" / "skill" / "payload").read_text(), "H2")
                self.assertEqual((root / "host-b" / "skill" / "payload").read_text(), "H2" if boundary == "batch:child:1:step:0:effect" else "H1")
                oracle.expect_pending("batch", "crashed-rollout")
                reopened = Manager(root, create=False)
                try:
                    restored = BatchManager(reopened)
                    if mode == "resume":
                        result = restored.recover("crashed-rollout", mode="resume", approve_plan_id=parent["plan_id"])
                        for child in plans:
                            self.watch_after(oracle, child)
                        expected_payload = "H2"
                    else:
                        inverse = restored.plan_compensation("crashed-rollout")
                        self.assertEqual(inverse["uncompensated"], [])
                        result = restored.apply(inverse, approve_plan_id=inverse["plan_id"])
                        for child in inverse["children"]:
                            self.watch_after(oracle, child["plan"])
                        expected_payload = "H1"
                    self.assertEqual(result["status"], "completed")
                    for original in (first, second):
                        oracle.expect_installation(original["installation_id"], state="active", payload=expected_payload)
                    self.assertEqual(oracle.pending(), [])
                finally:
                    reopened.repository.close()

    def test_s02_publication_before_readiness_requires_source_or_explicit_cancellation(self):
        with fixture(self) as (root, source, manager, oracle):
            _, first = self.action(oracle, manager, "install", source=source, target=root / "host-a" / "skill", payload="H1")
            (source / "payload").write_text("H2")
            plan = manager.plan("update", installation_id=first["installation_id"], source=source)
            oracle.watch_tree(source, tree_state(source))
            oracle.model_trees[plan["version"]["version_id"]] = normalized_tree(tree_state(source))
            oracle.check()
            self.crash(root, plan, "unused", batch=False, published=True)
            oracle.watch_tree(plan["version"]["snapshot"]["path"], oracle.model_trees[plan["version"]["version_id"]])
            oracle.expect_pending("transaction", "crashed-core")
            tx = oracle.check()["records"]["transaction"]["crashed-core"]
            self.assertFalse(tx["snapshot_ready"])
            self.assertTrue(Path(plan["version"]["snapshot"]["path"]).is_dir())
            shutil.rmtree(source)
            oracle.watch_tree(source, None)
            with self.assertRaises(LifecycleError):
                manager.recover("crashed-core", mode="resume", approve_plan_id=plan["plan_id"])
            oracle.expect_installation(first["installation_id"], state="active", authorization="valid", payload="H1")
            manager.recover("crashed-core", mode="compensate", approve_plan_id=plan["plan_id"])
            oracle.expect_pending("transaction", "crashed-core", present=False)
            source.mkdir()
            (source / "SKILL.md").write_text("# Replacement")
            (source / "payload").write_text("H3")
            self.action(oracle, manager, "update", installation_id=first["installation_id"], source=source, payload="H3")

    def test_s02_durable_readiness_allows_exact_snapshot_resume_after_candidate_disappears(self):
        with fixture(self) as (root, source, manager, oracle):
            _, first = self.action(oracle, manager, "install", source=source, target=root / "host-a" / "skill", payload="H1")
            (source / "payload").write_text("H2")
            plan = manager.plan("update", installation_id=first["installation_id"], source=source)
            oracle.watch_tree(source, tree_state(source))
            oracle.model_trees[plan["version"]["version_id"]] = normalized_tree(tree_state(source))
            oracle.check()
            self.crash(root, plan, "transaction:staged", batch=False)
            oracle.watch_tree(plan["version"]["snapshot"]["path"], oracle.model_trees[plan["version"]["version_id"]])
            tx = oracle.check()["records"]["transaction"]["crashed-core"]
            self.assertTrue(tx["snapshot_ready"])
            shutil.rmtree(source)
            oracle.watch_tree(source, None)
            result = manager.recover("crashed-core", mode="resume", approve_plan_id=plan["plan_id"])
            self.watch_after(oracle, plan)
            self.assertEqual(result["status"], "completed")
            oracle.expect_installation(first["installation_id"], state="active", authorization="valid", payload="H2")
            self.assertEqual(oracle.pending(), [])

    def test_s02_parent_committed_real_death_reopens_exact_receipt_without_repeating_effects(self):
        with fixture(self) as (root, source, manager, oracle):
            _, first, second = self.dual(root, source, manager, oracle)
            plans = self.saved_rollout(root, source, manager, first, second, oracle=oracle)
            parent = BatchManager(manager).plan(plans)
            oracle.check()
            self.crash(root, parent, "batch:committed")
            for child in plans:
                self.watch_after(oracle, child)
            records = oracle.check()["records"]
            tx = records["batch"]["crashed-rollout"]
            receipt = records["batch-receipt"][tx["receipt_id"]]
            self.assertEqual(tx["state"], "completed")
            inodes = [(root / host / "skill").lstat().st_ino for host in ("host-a", "host-b")]
            reopened = Manager(root, create=False)
            try:
                recovered = BatchManager(reopened).recover("crashed-rollout", mode="resume", approve_plan_id=parent["plan_id"])
                self.assertEqual(recovered, receipt)
                self.assertEqual(BatchManager(reopened).apply(parent, approve_plan_id=parent["plan_id"], batch_id="crashed-rollout"), receipt)
            finally:
                reopened.repository.close()
            self.assertEqual([(root / host / "skill").lstat().st_ino for host in ("host-a", "host-b")], inodes)
            self.assertEqual(oracle.check()["records"]["receipt"], records["receipt"])
            self.assertEqual(oracle.pending(), [])

    def test_s02_completed_core_retry_ignores_historical_candidate_alias_but_observes_current_damage(self):
        for mode in ("apply", "recover"):
            with self.subTest(mode=mode), fixture(self) as (root, source, manager, oracle):
                plan, first = self.action(oracle, manager, "install", source=source, target=root / "host-a" / "skill", payload="H1")
                self.retarget_candidate(root, source, oracle)
                def retry():
                    if mode == "apply":
                        return manager.apply(plan, approve_plan_id=plan["plan_id"], transaction_id=first["transaction_id"])
                    return manager.recover(first["transaction_id"], mode="resume", approve_plan_id=plan["plan_id"])
                self.assertEqual(retry(), first)
                oracle.check()
                target = root / "host-a" / "skill"
                link = os.readlink(target)
                target.unlink()
                oracle.watch_pointer(target, None)
                with self.assertRaises(LifecycleError):
                    retry()
                self.assertEqual(oracle.check()["records"]["installation"][first["installation_id"]]["authorization"]["state"], "invalidated")
                target.symlink_to(link)
                oracle.watch_pointer(target, link)
                self.assertFalse(manager.verify(first["installation_id"])["valid"])
                oracle.check()

    def test_s02_candidate_alias_recovery_distinguishes_durable_snapshot_readiness(self):
        for ready in (False, True):
            with self.subTest(ready=ready), fixture(self) as (root, source, manager, oracle):
                _, first = self.action(oracle, manager, "install", source=source, target=root / "host-a" / "skill", payload="H1")
                (source / "payload").write_text("H2")
                oracle.watch_tree(source, tree_state(source))
                plan = manager.plan("update", installation_id=first["installation_id"], source=source)
                oracle.model_trees[plan["version"]["version_id"]] = normalized_tree(tree_state(source))
                self.crash(root, plan, "transaction:staged" if ready else "transaction:prepared", batch=False)
                if ready:
                    oracle.watch_tree(plan["version"]["snapshot"]["path"], oracle.model_trees[plan["version"]["version_id"]])
                self.retarget_candidate(root, source, oracle)
                oracle.check()
                if ready:
                    result = manager.recover("crashed-core", mode="resume", approve_plan_id=plan["plan_id"])
                    self.assertEqual(result["status"], "completed")
                    self.watch_after(oracle, plan)
                    expected = "H2"
                else:
                    with self.assertRaises(LifecycleError):
                        manager.recover("crashed-core", mode="resume", approve_plan_id=plan["plan_id"])
                    manager.recover("crashed-core", mode="compensate", approve_plan_id=plan["plan_id"])
                    expected = "H1"
                oracle.expect_installation(first["installation_id"], state="active", payload=expected)
                self.assertEqual(oracle.pending(), [])

    def test_s02_batch_history_and_mixed_children_do_not_depend_on_completed_candidate(self):
        cases = (("batch:committed", False), ("batch:child:1:transaction:staged", False),
                 ("batch:child:0:transaction:committed", True), ("batch:child:1:transaction:staged", True))
        for boundary, compensate in cases:
            with self.subTest(boundary=boundary, compensate=compensate), fixture(self) as (root, source, manager, oracle):
                _, first, second = self.dual(root, source, manager, oracle)
                plans = self.saved_rollout(root, source, manager, first, second, oracle=oracle)
                batch = BatchManager(manager)
                parent = batch.plan(plans)
                self.crash(root, parent, boundary)
                self.watch_after(oracle, plans[0])
                if boundary == "batch:committed":
                    self.watch_after(oracle, plans[1])
                self.retarget_candidate(root, source, oracle)
                oracle.check()
                if compensate:
                    if boundary == "batch:child:0:transaction:committed":
                        with self.assertRaises(LifecycleError):
                            batch.recover("crashed-rollout", mode="resume", approve_plan_id=parent["plan_id"])
                    inverse = batch.plan_compensation("crashed-rollout")
                    result = batch.apply(inverse, approve_plan_id=inverse["plan_id"])
                    for child in inverse["children"]:
                        if child["kind"] == "compensate":
                            for step in child["plan"]["steps"]:
                                oracle.watch_pointer(step["path"], step["before"].get("link"))
                        else:
                            self.watch_after(oracle, child["plan"])
                    expected = "H1"
                else:
                    result = batch.recover("crashed-rollout", mode="resume", approve_plan_id=parent["plan_id"])
                    for child in plans:
                        self.watch_after(oracle, child)
                    if boundary == "batch:committed":
                        self.assertEqual(batch.apply(parent, approve_plan_id=parent["plan_id"], batch_id="crashed-rollout"), result)
                    expected = "H2"
                self.assertEqual(result["status"], "completed")
                for original in (first, second):
                    oracle.expect_installation(original["installation_id"], state="active", payload=expected)
                self.assertEqual(oracle.pending(), [])

    def test_s03_nested_inverse_history_survives_later_actions_and_gc_traversal(self):
        with fixture(self) as (root, source, manager, oracle):
            _, first = self.action(oracle, manager, "install", source=source, target=root / "host-a" / "skill", payload="H1")
            batch = BatchManager(manager)
            plan = batch.plan([manager.plan("rename", installation_id=first["installation_id"], name="renamed")])
            batch.apply(plan, approve_plan_id=plan["plan_id"], batch_id="ancestor-a")
            oracle.check()
            for original, identifier in (("ancestor-a", "inverse-b"), ("inverse-b", "inverse-c")):
                inverse = batch.plan_compensation(original)
                batch.apply(inverse, approve_plan_id=inverse["plan_id"], batch_id=identifier)
                oracle.expect_installation(first["installation_id"], state="active", payload="H1")
                for ancestor in ("ancestor-a",) if identifier == "inverse-b" else ("ancestor-a", "inverse-b", "inverse-c"):
                    historical = batch.inspect(ancestor)
                    self.assertIsNotNone(historical["receipt_id"])
                self.assertEqual(plan_retention(manager, "collect")["objects"], [])
            self.action(oracle, manager, "rename", installation_id=first["installation_id"], name="later", payload="H1")
            for ancestor in ("ancestor-a", "inverse-b", "inverse-c"):
                self.assertIsNotNone(batch.inspect(ancestor)["receipt_id"])
            self.assertEqual(plan_retention(manager, "collect")["objects"], [])

    def test_s03_inverse_chain_bound_refuses_before_effects_or_preserves_every_ancestor(self):
        with fixture(self) as (root, source, manager, oracle):
            _, first = self.action(oracle, manager, "install", source=source, target=root / "host-a" / "skill", payload="H1")
            batch = BatchManager(manager)
            plan = batch.plan([manager.plan("rename", installation_id=first["installation_id"], name="renamed")])
            batch.apply(plan, approve_plan_id=plan["plan_id"], batch_id="chain-0")
            ancestors = ["chain-0"]
            for depth in range(1, 54):
                before = oracle.check()["records"]
                try:
                    inverse = batch.plan_compensation(ancestors[-1])
                    result = batch.apply(inverse, approve_plan_id=inverse["plan_id"], batch_id="chain-" + str(depth))
                except LifecycleError as error:
                    if depth < 48:
                        raise
                    self.assertEqual(error.code, "batch_history_limit")
                    after = oracle.check()["records"]
                    for kind in ("installation", "transaction", "receipt", "batch", "batch-receipt", "grant"):
                        self.assertEqual(after.get(kind), before.get(kind), "chain bound must refuse before new effects or intents")
                    break
                self.assertEqual(result["status"], "completed")
                ancestors.append("chain-" + str(depth))
                oracle.check()
                for ancestor in ancestors:
                    self.assertIsNotNone(batch.inspect(ancestor)["receipt_id"])
            self.assertGreaterEqual(len(ancestors), 49)
            self.action(oracle, manager, "rename", installation_id=first["installation_id"], name="ordinary-after-bound", payload="H1")
            for ancestor in ancestors:
                self.assertIsNotNone(batch.inspect(ancestor)["receipt_id"])
            self.assertEqual(plan_retention(manager, "collect")["objects"], [])

    def test_s03_interrupted_nested_inverse_keeps_ancestor_completion_readable(self):
        with fixture(self) as (root, source, manager, oracle):
            _, first = self.action(oracle, manager, "install", source=source, target=root / "host-a" / "skill", payload="H1")
            batch = BatchManager(manager)
            plan = batch.plan([manager.plan("rename", installation_id=first["installation_id"], name="renamed")])
            batch.apply(plan, approve_plan_id=plan["plan_id"], batch_id="ancestor")
            inverse = batch.plan_compensation("ancestor")
            batch.apply(inverse, approve_plan_id=inverse["plan_id"], batch_id="first-inverse")
            nested = batch.plan_compensation("first-inverse")
            oracle.check()
            self.crash(root, nested, "batch:prepared")
            oracle.expect_pending("batch", "crashed-rollout")
            self.assertIsNotNone(batch.inspect("ancestor")["receipt_id"])
            self.assertIsNotNone(batch.inspect("first-inverse")["receipt_id"])
            result = batch.recover("crashed-rollout", mode="resume", approve_plan_id=nested["plan_id"])
            self.assertEqual(result["status"], "completed")
            for identifier in ("ancestor", "first-inverse", "crashed-rollout"):
                self.assertIsNotNone(batch.inspect(identifier)["receipt_id"])
            oracle.expect_installation(first["installation_id"], state="active", authorization="valid", payload="H1")

    def test_s03_missing_completion_proof_rejects_new_inverse_before_effects(self):
        with fixture(self) as (root, source, manager, oracle):
            _, first = self.action(oracle, manager, "install", source=source, target=root / "host-a" / "skill", payload="H1")
            batch = BatchManager(manager)
            plan = batch.plan([manager.plan("rename", installation_id=first["installation_id"], name="renamed")])
            receipt = batch.apply(plan, approve_plan_id=plan["plan_id"], batch_id="proof-original")
            before = oracle.check()["records"]
            original_get = manager.repository.get
            def missing(kind, identifier):
                return None if (kind, identifier) == ("batch-receipt", receipt["receipt_id"]) else original_get(kind, identifier)
            with patch.object(manager.repository, "get", side_effect=missing):
                with self.assertRaises(LifecycleError):
                    batch.plan_compensation("proof-original")
            after = oracle.check()["records"]
            for kind in ("installation", "transaction", "receipt", "batch", "batch-receipt", "grant"):
                self.assertEqual(after.get(kind), before.get(kind))
            oracle.expect_installation(first["installation_id"], state="active", authorization="valid", payload="H1")

    def test_s04_candidate_drift_and_observed_active_failure_have_distinct_authority(self):
        with fixture(self) as (root, source, manager, oracle):
            _, first = self.action(oracle, manager, "install", source=source, target=root / "host-a" / "skill", payload="H1")
            identifier, target = first["installation_id"], root / "host-a" / "skill"
            (source / "payload").write_text("H2 candidate only")
            oracle.watch_tree(source, tree_state(source))
            self.assertTrue(manager.verify(identifier)["valid"])
            oracle.expect_installation(identifier, state="active", authorization="valid", grant_id=first["grant_id"], payload="H1")
            renewal = manager.plan("renew", installation_id=identifier)
            link = os.readlink(target)
            target.unlink()
            oracle.watch_pointer(target, None)
            with self.assertRaises(LifecycleError):
                manager.apply(renewal, approve_plan_id=renewal["plan_id"], transaction_id="denied-before-intent")
            oracle.check()
            self.assertNotIn("denied-before-intent", oracle.check()["records"]["transaction"])
            target.symlink_to(link)
            oracle.watch_pointer(target, link)
            self.assertFalse(manager.verify(identifier)["valid"])
            self.assertEqual(preflight(manager, identifier, refresh=False)["decision"], "block")
            oracle.expect_installation(identifier, state="active", authorization="invalidated", grant_id=first["grant_id"], payload="H1")
            _, fresh = self.action(oracle, manager, "renew", installation_id=identifier, payload="H1")
            self.assertNotEqual(fresh["grant_id"], first["grant_id"])
            self.assertEqual(preflight(manager, identifier, refresh=True)["decision"], "proceed")
            oracle.check()

    def test_s04_revoke_during_pending_work_survives_cancellation_and_historical_retry(self):
        with fixture(self) as (root, source, manager, oracle):
            _, first = self.action(oracle, manager, "install", source=source, target=root / "host-a" / "skill", payload="H1")
            identifier = first["installation_id"]
            pending = manager.plan("renew", installation_id=identifier)
            oracle.check()
            self.crash(root, pending, "transaction:prepared", batch=False)
            oracle.expect_pending("transaction", "crashed-core")
            _, revoked = self.action(oracle, manager, "revoke", installation_id=identifier, payload="H1")
            with self.assertRaises(LifecycleError):
                manager.recover("crashed-core", mode="resume", approve_plan_id=pending["plan_id"])
            manager.recover("crashed-core", mode="compensate", approve_plan_id=pending["plan_id"])
            oracle.expect_installation(identifier, state="active", authorization="revoked", grant_id=first["grant_id"], payload="H1")
            self.assertEqual(oracle.check()["records"]["installation"][identifier]["receipt_id"], revoked["receipt_id"])
            self.assertEqual(oracle.pending(), [])
            _, fresh = self.action(oracle, manager, "renew", installation_id=identifier, payload="H1")
            before = oracle.check()["records"]["installation"][identifier]
            with self.assertRaises(LifecycleError):
                manager.recover(first["transaction_id"], mode="resume", approve_plan_id=first["plan_id"])
            self.assertEqual(oracle.check()["records"]["installation"][identifier], before)
            self.assertNotEqual(fresh["grant_id"], first["grant_id"])

    def test_s04_observation_real_death_preserves_uncertainty_fence_until_new_approval(self):
        with fixture(self) as (root, source, manager, oracle):
            _, first = self.action(oracle, manager, "install", source=source, target=root / "host-a" / "skill", payload="H1")
            identifier, target = first["installation_id"], root / "host-a" / "skill"
            manager.verify(identifier)
            initial = oracle.check()["records"]
            link = os.readlink(target)
            target.unlink()
            oracle.watch_pointer(target, None)
            self.crash(root, {"installation_id": identifier}, "verification:observed", batch=False)
            interrupted = oracle.check()["records"]
            marker = interrupted["verification-run"][identifier]
            self.assertEqual(marker["state"], "in_progress")
            self.assertEqual(marker["grant_id"], first["grant_id"])
            # observed precedes the atomic completed-observation publication;
            # the durable run fence, not a fabricated result, carries uncertainty.
            self.assertEqual(interrupted["verification"], initial["verification"])
            self.assertEqual(preflight(manager, identifier, refresh=False)["decision"], "block")
            target.symlink_to(link)
            oracle.watch_pointer(target, link)
            self.assertFalse(manager.verify(identifier)["valid"])
            oracle.expect_installation(identifier, state="active", authorization="invalidated", grant_id=first["grant_id"], payload="H1")
            self.action(oracle, manager, "renew", installation_id=identifier, payload="H1")
            self.assertEqual(preflight(manager, identifier, refresh=True)["decision"], "proceed")
            oracle.check()

    def test_s08_negative_admission_preserves_existing_state_and_has_no_false_receipt(self):
        for negative in ("approval", "checksum", "source", "shared-definition", "overlap", "stale", "reused-key"):
            with self.subTest(negative=negative), fixture(self) as (root, source, manager, oracle):
                _, first = self.action(oracle, manager, "install", source=source, target=root / "host-a" / "skill", payload="H1")
                plan = manager.plan("renew", installation_id=first["installation_id"])
                approve, transaction_id = plan["plan_id"], "rejected"
                if negative == "approval":
                    approve = "wrong-exact-plan"
                elif negative == "checksum":
                    plan["after"]["name"] = "changed-without-approval"
                elif negative == "source":
                    (source / "payload").write_text("H2")
                    plan = manager.plan("update", installation_id=first["installation_id"], source=source)
                    approve = plan["plan_id"]
                    (source / "payload").write_text("H3")
                    oracle.watch_tree(source, tree_state(source))
                elif negative == "shared-definition":
                    plan["version"]["snapshot"]["source_tree_sha256"] = "0" * 64
                    plan["plan_id"] = digest({key: value for key, value in plan.items() if key != "plan_id"})
                    approve = plan["plan_id"]
                elif negative == "overlap":
                    plan["steps"][0]["path"] = str(source / "nested")
                    plan["after"]["target"] = str(source / "nested")
                    plan["plan_id"] = digest({key: value for key, value in plan.items() if key != "plan_id"})
                    approve = plan["plan_id"]
                elif negative == "stale":
                    self.action(oracle, manager, "renew", installation_id=first["installation_id"], payload="H1")
                elif negative == "reused-key":
                    transaction_id = first["transaction_id"]
                before = oracle.check()["records"]
                with self.assertRaises(LifecycleError):
                    manager.apply(plan, approve_plan_id=approve, transaction_id=transaction_id)
                after = oracle.check()["records"]
                for kind in ("installation", "transaction", "receipt", "grant"):
                    self.assertEqual(after.get(kind), before.get(kind))
                oracle.expect_installation(first["installation_id"], state="active", authorization="valid", payload="H1")

    def test_s08_shared_content_compatibility_does_not_relax_origin_shape_validation(self):
        mutations = (("source", []), ("source", "relative/path"), ("created_at", None),
                     ("parent_version_id", []), ("provenance", {"operation": [], "legacy_receipt_id": None}))
        for field, value in mutations:
            with self.subTest(field=field, value=value), fixture(self) as (root, source, manager, oracle):
                _, first = self.action(oracle, manager, "install", source=source, target=root / "host-a" / "skill", payload="H1")
                plan = manager.plan("renew", installation_id=first["installation_id"])
                plan["version"][field] = value
                plan["plan_id"] = digest({key: item for key, item in plan.items() if key != "plan_id"})
                before = oracle.check()["records"]
                with self.assertRaises(LifecycleError):
                    manager.apply(plan, approve_plan_id=plan["plan_id"], transaction_id="invalid-origin-shape")
                after = oracle.check()["records"]
                for kind in ("installation", "transaction", "receipt", "version", "grant"):
                    self.assertEqual(after.get(kind), before.get(kind))
                oracle.expect_installation(first["installation_id"], state="active", authorization="valid", payload="H1")

    def test_s08_conflicting_unregistered_shared_skill_definitions_reject_whole_batch(self):
        with fixture(self) as (root, source, manager, oracle):
            first = manager.plan("install", source=source, target=root / "host-a" / "skill")
            second = manager.plan("install", source=source, target=root / "host-b" / "skill")
            second["skill"] = {**first["skill"], "name": "conflicting-original-name"}
            second["after"]["skill_id"] = first["skill"]["skill_id"]
            second["version"]["skill_id"] = first["skill"]["skill_id"]
            second["version"]["version_id"] = first["version"]["version_id"]
            second["after"]["version_id"] = first["version"]["version_id"]
            second["plan_id"] = digest({key: item for key, item in second.items() if key != "plan_id"})
            batch = BatchManager(manager)
            with self.assertRaises(LifecycleError):
                plan = batch.plan([first, second])
                batch.apply(plan, approve_plan_id=plan["plan_id"])
            records = oracle.check()["records"]
            for kind in ("installation", "transaction", "receipt", "batch", "batch-receipt", "version", "grant"):
                self.assertEqual(records.get(kind, {}), {}, "conflicting definitions must fail before the first effect")
            self.assertFalse(os.path.lexists(root / "host-a" / "skill"))
            self.assertFalse(os.path.lexists(root / "host-b" / "skill"))

    def test_oracle_mutation_controls_detect_fact_loss_bad_binding_and_pointer_damage(self):
        for mutation in ("receipt", "grant", "intent-loss", "pointer"):
            with self.subTest(mutation=mutation), fixture(self) as (root, source, manager, oracle):
                _, receipt = self.action(oracle, manager, "install", source=source, target=root / "host-a" / "skill", payload="H1")
                if mutation in {"receipt", "grant"}:
                    record = manager.repository.get(mutation, receipt[mutation + "_id"])
                    manager.repository.put(mutation, record["id"], {**record["data"], "installation_id": "foreign"}, expected_revision=record["revision"])
                elif mutation == "intent-loss":
                    with closing(sqlite3.connect(str(oracle.database))) as connection, connection:
                        connection.execute("DELETE FROM records WHERE kind='transaction' AND id=?", (receipt["transaction_id"],))
                else:
                    target = root / "host-a" / "skill"
                    target.unlink()
                    target.symlink_to(source)
                with self.assertRaises(AssertionError):
                    if mutation == "pointer":
                        oracle.expect_installation(receipt["installation_id"], state="active", payload="H1")
                    else:
                        oracle.check()
                if mutation in {"receipt", "grant"}:
                    # A fresh observer must catch the broken cross-record binding
                    # even without a previously captured immutable envelope.
                    with self.assertRaises(AssertionError):
                        ModelOracle(self, manager).check()

    def test_oracle_fresh_observer_rejects_orphan_history_and_missing_completion_events(self):
        for mutation in ("orphan-receipt", "orphan-grant", "completion-event"):
            with self.subTest(mutation=mutation), fixture(self) as (root, source, manager, oracle):
                _, receipt = self.action(oracle, manager, "install", source=source, target=root / "host-a" / "skill", payload="H1")
                if mutation == "orphan-receipt":
                    manager.repository.put("receipt", "orphan", {**receipt, "receipt_id": "orphan"})
                elif mutation == "orphan-grant":
                    grant = manager.repository.get("grant", receipt["grant_id"])["data"]
                    manager.repository.put("grant", "orphan", {**grant, "grant_id": "orphan"})
                else:
                    # The test-only copied fixture deliberately violates its event
                    # history; the model must detect it without production readers.
                    with closing(sqlite3.connect(str(oracle.database))) as connection, connection:
                        connection.execute("DROP TRIGGER events_no_delete")
                        connection.execute("DELETE FROM events WHERE event_type='transaction_completed'")
                with self.assertRaises(AssertionError):
                    ModelOracle(self, manager).check()

    def test_oracle_full_tree_control_checks_non_payload_bytes_modes_and_link_topology(self):
        for mutation in ("document", "mode", "link"):
            with self.subTest(mutation=mutation), fixture(self) as (root, source, manager, oracle):
                (source / "payload-link").symlink_to("payload")
                expected = normalized_tree(tree_state(source))
                plan, receipt = self.action(oracle, manager, "install", source=source, target=root / "host-a" / "skill", payload="H1")
                snapshot = Path(plan["version"]["snapshot"]["path"])
                oracle.watch_tree(snapshot, expected)
                oracle.check()
                if mutation == "document":
                    document = snapshot / "SKILL.md"
                    mode = document.stat().st_mode & 0o777
                    document.chmod(mode | 0o200)
                    document.write_text("CHANGED NON-PAYLOAD DOCUMENT")
                    document.chmod(mode)
                elif mutation == "mode":
                    (snapshot / "SKILL.md").chmod(0o644)
                else:
                    snapshot.chmod(0o755)
                    (snapshot / "payload-link").unlink()
                    (snapshot / "payload-link").symlink_to("SKILL.md")
                    snapshot.chmod(0o555)
                with self.assertRaises(AssertionError):
                    oracle.check()

    def test_oracle_retention_false_success_controls_require_progress_and_completion_event(self):
        for mutation in ("orphan", "unfinished", "missing-event"):
            with self.subTest(mutation=mutation), fixture(self) as (root, source, manager, oracle):
                _, first = self.action(oracle, manager, "install", source=source, target=root / "host-a" / "skill", payload="H1")
                _, last = self.action(oracle, manager, "uninstall", installation_id=first["installation_id"])
                for operation, options in (("policy", {"keep_recent": 0}), ("expire", {"receipt_ids": [first["receipt_id"], last["receipt_id"]]})):
                    plan = plan_retention(manager, operation, **options)
                    apply_retention(manager, plan, approve_plan_id=plan["plan_id"])
                    oracle.check()
                plan = plan_retention(manager, "collect")
                def stop(name, tx):
                    if name == "retention:prepared":
                        raise OSError("model fixture prepared interruption")
                with self.assertRaises(LifecycleError):
                    apply_retention(manager, plan, approve_plan_id=plan["plan_id"], transaction_id="incomplete", checkpoint=stop)
                oracle.check()
                fake = {"schema_version": "skills-auditor-lifecycle-retention-receipt/v1", "receipt_id": "false-success",
                        "transaction_id": "incomplete", "plan_id": plan["plan_id"], "operation": "collect", "status": "completed",
                        "completed_at": "2026-09-07T00:00:00Z", "permanently_deleted": False, "objects": plan["objects"], "expires": plan["expires"]}
                manager.repository.put("retention-receipt", "false-success", fake)
                if mutation != "orphan":
                    record = manager.repository.get("retention-transaction", "incomplete")
                    tx = record["data"]
                    tx["receipt_id"] = "false-success"
                    if mutation == "missing-event":
                        tx["state"] = "completed"
                        tx["completion_event_sequence"] = 999999
                        for step in tx["objects"]:
                            step["state"] = "completed"
                    manager.repository.put("retention-transaction", "incomplete", tx, expected_revision=record["revision"])
                with self.assertRaises(AssertionError):
                    ModelOracle(self, manager).check()

    def test_oracle_batch_false_success_requires_terminal_parent_and_real_completed_children(self):
        for mutation in ("unfinished-parent", "pending-child", "missing-core"):
            with self.subTest(mutation=mutation), fixture(self) as (root, source, manager, oracle):
                child = manager.plan("install", source=source, target=root / "host-a" / "skill")
                plan = BatchManager(manager).plan([child])

                def stop(name, tx):
                    if name == "batch:prepared":
                        raise OSError("model fixture prepared interruption")

                with self.assertRaises(LifecycleError):
                    BatchManager(manager).apply(plan, approve_plan_id=plan["plan_id"], batch_id="incomplete", checkpoint=stop)
                oracle.check()
                record = manager.repository.get("batch", "incomplete")
                tx = record["data"]
                self.assertEqual(tx["state"], "recovery_needed")
                self.assertEqual(manager.repository.list("transaction"), [])
                self.assertFalse(os.path.lexists(root / "host-a" / "skill"))
                if mutation != "unfinished-parent":
                    tx["state"] = "completed"
                if mutation == "missing-core":
                    for reference in tx["children"]:
                        reference.update(state="completed", receipt_id="missing-child-receipt")
                fake = {"schema_version": "skills-auditor-lifecycle-batch-receipt/v1", "receipt_id": "false-success",
                        "batch_id": "incomplete", "plan_id": plan["plan_id"], "status": "completed",
                        "children": copy.deepcopy(tx["children"]), "compensates_batch_id": None, "uncompensated": [],
                        "completed_at": "2026-09-07T00:00:00Z"}
                manager.repository.put("batch-receipt", "false-success", fake)
                tx["receipt_id"] = "false-success"
                manager.repository.put("batch", "incomplete", tx, expected_revision=record["revision"])
                manager.repository.append_event("incomplete", "batch_completed", {"receipt_id": "false-success"}, "fixture", "oracle-test")
                with self.assertRaises(AssertionError):
                    ModelOracle(self, manager).check()

    def test_oracle_retargeted_candidate_keeps_original_moved_tree_under_observation(self):
        with fixture(self) as (root, source, manager, oracle):
            oracle.watch_tree(source, tree_state(source))
            self.retarget_candidate(root, source, oracle)
            oracle.check()
            (root / "candidate-moved" / "SKILL.md").write_text("CHANGED ORIGINAL CANDIDATE")
            with self.assertRaises(AssertionError):
                oracle.check()

    def test_oracle_preserves_completed_history_when_inverse_reports_uncompensated_work(self):
        with fixture(self) as (root, source, manager, oracle):
            _, receipt = self.action(oracle, manager, "install", source=source, target=root / "host-a" / "skill", payload="H1")
            self.action(oracle, manager, "disable", installation_id=receipt["installation_id"])
            child = manager.plan("uninstall", installation_id=receipt["installation_id"])
            batch = BatchManager(manager)
            plan = batch.plan([child])
            completed = batch.apply(plan, approve_plan_id=plan["plan_id"], batch_id="inactive-removal")
            oracle.check()
            inverse = batch.plan_compensation(completed["batch_id"])
            self.assertEqual(inverse["children"], [])
            self.assertTrue(inverse["uncompensated"])
            outcome = batch.apply(inverse, approve_plan_id=inverse["plan_id"])
            self.assertEqual(outcome["status"], "completed")
            self.assertEqual(batch.inspect(completed["batch_id"])["state"], "recovery_needed")
            oracle.check()
            ModelOracle(self, manager).check()
