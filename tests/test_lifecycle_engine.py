"""Managed lifecycle invariants using only isolated fixture installations."""

import copy
import os
import tempfile
import unittest
import unicodedata
from pathlib import Path
from unittest.mock import patch

from skills_auditor.lifecycle.common import LifecycleError, digest
from skills_auditor.lifecycle.engine import Manager


class LifecycleFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="skills-auditor-managed-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "candidate"
        self.source.mkdir()
        (self.source / "SKILL.md").write_text("# Example\n", encoding="utf-8")
        (self.source / "payload").write_text("H1", encoding="utf-8")
        self.host = self.root / "host"
        self.host.mkdir()
        self.target = self.host / "example"
        self.manager = Manager(self.root)

    def install(self):
        plan = self.manager.plan("install", source=self.source, target=self.target, name="example")
        return plan, self.manager.apply(plan, approve_plan_id=plan["plan_id"])

    def approve(self, operation, installation_id, **kwargs):
        plan = self.manager.plan(operation, installation_id=installation_id, **kwargs)
        return self.manager.apply(plan, approve_plan_id=plan["plan_id"])


class TestLifecycleEngine(LifecycleFixture):
    def test_saved_apply_missing_current_parent_records_denial_but_new_destination_does_not(self):
        _, installed = self.install()
        identifier = installed["installation_id"]
        saved = self.manager.plan("renew", installation_id=identifier)
        self.host.rename(self.root / "displaced")
        with self.assertRaises(LifecycleError):
            self.manager.apply(saved, approve_plan_id=saved["plan_id"], transaction_id="missing-parent")
        self.assertEqual(self.manager.get_installation(identifier)["authorization"]["state"], "invalidated")
        self.assertIsNone(self.manager.repository.get("transaction", "missing-parent"))
        (self.root / "displaced").rename(self.host)
        self.assertFalse(self.manager.verify(identifier)["valid"])
        self.approve("renew", identifier)
        destination_parent = self.root / "destination-parent"
        destination_parent.mkdir()
        move = self.manager.plan("move", installation_id=identifier, target=destination_parent / "skill")
        before = self.manager.get_installation(identifier)
        destination_parent.rmdir()
        with self.assertRaises(LifecycleError):
            self.manager.apply(move, approve_plan_id=move["plan_id"])
        self.assertEqual(self.manager.get_installation(identifier), before)
        self.assertTrue(self.manager.verify(identifier)["valid"])

    def test_saved_apply_active_boundary_failure_is_sticky_before_new_intent(self):
        for damage in ("target", "snapshot"):
            with self.subTest(damage=damage):
                source = self.root / ("source-" + damage)
                source.mkdir()
                (source / "SKILL.md").write_text("# " + damage)
                (source / "payload").write_text("H1")
                target = self.host / ("saved-" + damage)
                install = self.manager.plan("install", source=source, target=target)
                installed = self.manager.apply(install, approve_plan_id=install["plan_id"])
                saved = self.manager.plan("renew", installation_id=installed["installation_id"])
                original_link = os.readlink(target)
                payload = target / "payload"
                mode = payload.stat().st_mode & 0o777
                if damage == "target":
                    target.unlink()
                else:
                    payload.chmod(mode | 0o200)
                    payload.write_text("BROKEN ACTIVE")
                    payload.chmod(mode)
                with self.assertRaises(LifecycleError):
                    self.manager.apply(saved, approve_plan_id=saved["plan_id"], transaction_id="preflight-" + damage)
                self.assertEqual(self.manager.get_installation(installed["installation_id"])["authorization"]["state"], "invalidated")
                self.assertIsNone(self.manager.repository.get("transaction", "preflight-" + damage))
                if damage == "target":
                    target.symlink_to(original_link)
                else:
                    payload.chmod(mode | 0o200)
                    payload.write_text("H1")
                    payload.chmod(mode)
                self.assertFalse(self.manager.verify(installed["installation_id"])["valid"])
                self.assertEqual(self.manager.get_installation(installed["installation_id"])["authorization"]["grant_id"], installed["grant_id"])

    def test_completed_retry_observation_is_sticky_for_apply_and_recover(self):
        for method in ("apply", "recover"):
            with self.subTest(method=method):
                target = self.host / method
                plan = self.manager.plan("install", source=self.source, target=target)
                receipt = self.manager.apply(plan, approve_plan_id=plan["plan_id"], transaction_id=method)
                self.manager.verify(receipt["installation_id"])
                marker = self.manager.repository.get("verification-run", receipt["installation_id"])["data"]
                def retry():
                    if method == "apply":
                        return self.manager.apply(plan, approve_plan_id=plan["plan_id"], transaction_id=method)
                    return self.manager.recover(method, mode="resume", approve_plan_id=plan["plan_id"])
                self.assertEqual(retry(), receipt)
                self.assertEqual(self.manager.repository.get("verification-run", receipt["installation_id"])["data"], marker)
                link = os.readlink(target)
                target.unlink()
                with self.assertRaises(LifecycleError):
                    retry()
                authorization = self.manager.get_installation(receipt["installation_id"])["authorization"]
                self.assertEqual(authorization["state"], "invalidated")
                self.assertIn("target_link", authorization["reason_codes"])
                target.symlink_to(link)
                self.assertFalse(self.manager.verify(receipt["installation_id"])["valid"])
                self.assertEqual(self.manager.get_installation(receipt["installation_id"])["authorization"]["grant_id"], receipt["grant_id"])
                self.assertEqual(self.manager.repository.get("receipt", receipt["receipt_id"])["data"], receipt)

    def test_completed_retry_records_evidence_failure_and_does_not_revive_restored_records(self):
        for kind in ("receipt", "grant"):
            with self.subTest(kind=kind):
                plan = self.manager.plan("install", source=self.source, target=self.host / kind)
                receipt = self.manager.apply(plan, approve_plan_id=plan["plan_id"], transaction_id=kind)
                identifier = receipt[kind + "_id"]
                original = self.manager.repository.get(kind, identifier)
                altered = {**original["data"], "installation_id": "unrelated"}
                self.manager.repository.put(kind, identifier, altered, expected_revision=original["revision"])
                with self.assertRaises(LifecycleError):
                    self.manager.apply(plan, approve_plan_id=plan["plan_id"], transaction_id=kind)
                self.assertEqual(self.manager.get_installation(receipt["installation_id"])["authorization"]["state"], "invalidated")
                revision = self.manager.repository.get(kind, identifier)["revision"]
                self.manager.repository.put(kind, identifier, original["data"], expected_revision=revision)
                self.assertFalse(self.manager.verify(receipt["installation_id"])["valid"])

    def test_planning_records_active_damage_but_allows_fresh_candidate_repair(self):
        _, receipt = self.install()
        identifier = receipt["installation_id"]
        payload = self.target / "payload"
        mode = payload.stat().st_mode & 0o777
        payload.chmod(mode | 0o200)
        payload.write_text("DAMAGED")
        payload.chmod(mode)
        (self.source / "payload").write_text("H2")
        repair = self.manager.plan("update", installation_id=identifier, source=self.source)
        self.assertEqual(repair["before"]["authorization"]["state"], "invalidated")
        self.assertIn("snapshot_tree", repair["before"]["authorization"]["reason_codes"])
        self.assertEqual(repair["expected_revision"], self.manager.repository.get("installation", identifier)["revision"])
        self.manager.apply(repair, approve_plan_id=repair["plan_id"])
        self.assertTrue(self.manager.verify(identifier)["valid"])
        self.assertEqual((self.target / "payload").read_text(), "H2")

    def test_planning_missing_active_parent_leaves_sticky_denial(self):
        _, receipt = self.install()
        self.host.rename(self.root / "displaced")
        with self.assertRaises(LifecycleError):
            self.manager.plan("renew", installation_id=receipt["installation_id"])
        self.assertEqual(self.manager.get_installation(receipt["installation_id"])["authorization"]["state"], "invalidated")
        (self.root / "displaced").rename(self.host)
        self.assertFalse(self.manager.verify(receipt["installation_id"])["valid"])

    def test_failed_candidate_destination_or_stale_plan_do_not_invalidate_intact_active_version(self):
        _, receipt = self.install()
        identifier = receipt["installation_id"]
        saved = self.manager.plan("renew", installation_id=identifier)
        with self.assertRaises(LifecycleError):
            self.manager.plan("update", installation_id=identifier, source=self.root / "absent-candidate")
        occupied = self.host / "occupied"
        occupied.write_text("foreign")
        with self.assertRaises(LifecycleError):
            self.manager.plan("move", installation_id=identifier, target=occupied)
        (self.source / "payload").write_text("H2")
        stale = self.manager.plan("update", installation_id=identifier, source=self.source)
        (self.source / "payload").write_text("H3")
        with self.assertRaises(LifecycleError):
            self.manager.apply(stale, approve_plan_id=stale["plan_id"])
        self.assertEqual(self.manager.get_installation(identifier)["authorization"]["state"], "valid")
        self.manager.apply(saved, approve_plan_id=saved["plan_id"])
        self.assertEqual((self.target / "payload").read_text(), "H1")

    def test_corrupt_selected_historical_version_does_not_invalidate_current_version(self):
        _, first = self.install()
        old_snapshot = self.manager.repository.get("version", first["version_id"])["data"]["snapshot"]
        (self.source / "payload").write_text("H2")
        current = self.approve("update", first["installation_id"], source=self.source)
        old_payload = Path(old_snapshot["path"]) / "payload"
        mode = old_payload.stat().st_mode & 0o777
        old_payload.chmod(mode | 0o200)
        old_payload.write_text("DAMAGED H1")
        old_payload.chmod(mode)
        with self.assertRaises(LifecycleError):
            self.manager.plan("rollback", installation_id=first["installation_id"], version_id=first["version_id"])
        with self.assertRaises(LifecycleError):
            self.manager.plan("install-retained", version_id=first["version_id"], target=self.host / "another")
        self.assertTrue(self.manager.verify(first["installation_id"])["valid"])
        self.assertEqual(self.manager.get_installation(first["installation_id"])["authorization"]["grant_id"], current["grant_id"])
        self.assertEqual((self.target / "payload").read_text(), "H2")

    def test_stale_historical_retry_and_revoke_do_not_probe_current_files(self):
        plan, receipt = self.install()
        renewal = self.approve("renew", receipt["installation_id"])
        with patch("skills_auditor.lifecycle.engine.verify_snapshot", side_effect=AssertionError("no snapshot read")), patch("skills_auditor.lifecycle.engine._entry", side_effect=AssertionError("no target read")):
            with self.assertRaises(LifecycleError) as caught:
                self.manager.apply(plan, approve_plan_id=plan["plan_id"], transaction_id=receipt["transaction_id"])
            self.assertEqual(caught.exception.code, "stale_transaction")
            revoked = self.manager.plan("revoke", installation_id=receipt["installation_id"])
            denial = self.manager.apply(revoked, approve_plan_id=revoked["plan_id"], transaction_id="revoked-read-free")
            self.assertEqual(self.manager.apply(revoked, approve_plan_id=revoked["plan_id"], transaction_id="revoked-read-free"), denial)
        self.assertEqual(denial["grant_id"], renewal["grant_id"])

    def test_install_retained_creates_new_identity_and_grant_without_mutable_source(self):
        import shutil
        _, receipt = self.install()
        old_version = self.manager.repository.get("version", receipt["version_id"])
        self.approve("uninstall", receipt["installation_id"])
        shutil.rmtree(self.source)
        plan = self.manager.plan("install-retained", version_id=receipt["version_id"], target=self.target)
        self.assertIsNone(plan["source"])
        self.assertEqual(plan["version"], old_version["data"])
        self.assertNotEqual(plan["installation_id"], receipt["installation_id"])
        with self.assertRaises(LifecycleError):
            self.manager.apply(plan, approve_plan_id=None)
        installed = self.manager.apply(plan, approve_plan_id=plan["plan_id"])
        self.assertEqual(installed["skill_id"], receipt["skill_id"])
        self.assertNotEqual(installed["grant_id"], receipt["grant_id"])
        self.assertEqual(self.manager.repository.get("version", receipt["version_id"]), old_version)
        self.assertEqual(self.manager.get_installation(receipt["installation_id"])["state"], "uninstalled")
        self.assertEqual((self.target / "payload").read_text(), "H1")
        self.assertTrue(self.manager.verify(installed["installation_id"])["valid"])

    def test_source_target_case_and_unicode_alias_overlap_rejected_before_intent(self):
        sources = [(self.source, self.root / "CANDIDATE")]
        accented = self.root / "caf\u00e9"
        accented.mkdir()
        (accented / "SKILL.md").write_text("# Unicode\n")
        sources.append((accented, self.root / unicodedata.normalize("NFD", accented.name)))
        for source, alias in sources:
            with self.subTest(alias=alias):
                before = sorted(source.iterdir())
                with self.assertRaises(LifecycleError) as caught:
                    self.manager.plan("install", source=source, target=alias / "nested-install")
                self.assertEqual(caught.exception.code, "unsafe_overlap")
                self.assertEqual(sorted(source.iterdir()), before)
                self.assertEqual(self.manager.repository.list("transaction"), [])
                self.assertEqual(self.manager.repository.list("receipt"), [])

    def test_rehashed_nullable_and_container_field_types_fail_before_any_intent(self):
        plan = self.manager.plan("install", source=self.source, target=self.target)
        mutations = [(field, value) for field in ("legacy_receipt", "operation", "source", "skill", "version", "after", "steps")
                     for value in ([], {}, "", False, 0)]
        for field, value in mutations:
            with self.subTest(field=field, value=value):
                changed = copy.deepcopy(plan)
                changed[field] = value
                changed["plan_id"] = digest({key: item for key, item in changed.items() if key != "plan_id"})
                with self.assertRaises(LifecycleError) as caught:
                    self.manager.apply(changed, approve_plan_id=changed["plan_id"])
                self.assertEqual(caught.exception.code, "invalid_plan")
                self.assertFalse(self.target.is_symlink())
                self.assertEqual(self.manager.repository.list("transaction"), [])
                self.assertEqual(self.manager.repository.list("receipt"), [])

    def test_retained_operations_require_none_not_falsey_source_and_receipt(self):
        _, receipt = self.install()
        before = self.target.lstat()
        for operation in ("renew", "revoke"):
            plan = self.manager.plan(operation, installation_id=receipt["installation_id"])
            for field in ("source", "legacy_receipt"):
                for value in ([], {}, "", False, 0):
                    with self.subTest(operation=operation, field=field, value=value):
                        changed = copy.deepcopy(plan)
                        changed[field] = value
                        changed["plan_id"] = digest({key: item for key, item in changed.items() if key != "plan_id"})
                        with self.assertRaises(LifecycleError) as caught:
                            self.manager.apply(changed, approve_plan_id=changed["plan_id"])
                        self.assertEqual(caught.exception.code, "invalid_plan")
                        self.assertEqual(len(self.manager.repository.list("transaction")), 1)
                        self.assertEqual(len(self.manager.repository.list("receipt")), 1)
                        self.assertEqual(self.target.lstat().st_ino, before.st_ino)

    def test_explicit_exact_approval_and_immutable_active_version(self):
        plan = self.manager.plan("install", source=self.source, target=self.target)
        for approval in (None, "other"):
            with self.assertRaises(LifecycleError) as caught:
                self.manager.apply(plan, approve_plan_id=approval)
            self.assertEqual(caught.exception.code, "approval_required")
        self.assertFalse(self.target.is_symlink())
        receipt = self.manager.apply(plan, approve_plan_id=plan["plan_id"])
        (self.source / "payload").write_text("H2", encoding="utf-8")
        self.assertEqual((self.target / "payload").read_text(), "H1")
        verification = self.manager.verify(receipt["installation_id"])
        self.assertEqual(verification["approval"]["state"], "valid")
        update = self.manager.plan("update", installation_id=receipt["installation_id"], source=self.source)
        self.assertEqual((self.target / "payload").read_text(), "H1")
        updated = self.manager.apply(update, approve_plan_id=update["plan_id"])
        self.assertEqual((self.target / "payload").read_text(), "H2")
        self.assertNotEqual(receipt["version_id"], updated["version_id"])
        version = self.manager.repository.get("version", updated["version_id"])["data"]
        self.assertEqual(version["parent_version_id"], receipt["version_id"])

    def test_noop_renewal_preserves_pointer_and_historical_receipt(self):
        plan, receipt = self.install()
        before = self.target.lstat()
        old = copy.deepcopy(receipt)
        with patch("skills_auditor.lifecycle.engine.os.replace") as replace:
            renewed = self.approve("renew", receipt["installation_id"])
        replace.assert_not_called()
        self.assertEqual(before.st_ino, self.target.lstat().st_ino)
        self.assertEqual(receipt, old)
        self.assertNotEqual(receipt["grant_id"], renewed["grant_id"])
        self.assertEqual(self.manager.repository.get("receipt", receipt["receipt_id"])["data"], old)

    def test_observed_target_failure_is_sticky_until_explicit_renewal(self):
        _, receipt = self.install()
        raw = os.readlink(self.target)
        self.target.unlink()
        failed = self.manager.verify(receipt["installation_id"])
        self.assertEqual(failed["approval"]["state"], "invalidated")
        self.target.symlink_to(raw, target_is_directory=True)
        restored = self.manager.verify(receipt["installation_id"])
        self.assertTrue(restored["integrity"]["valid"])
        self.assertEqual(restored["approval"]["state"], "invalidated")
        self.approve("renew", receipt["installation_id"])
        self.assertEqual(self.manager.verify(receipt["installation_id"])["approval"]["state"], "valid")

    def test_move_disable_enable_archive_uninstall_preserve_identity_history(self):
        _, receipt = self.install()
        installation_id = receipt["installation_id"]
        destination = self.host / "renamed"
        moved = self.approve("move", installation_id, target=destination, name="renamed")
        self.assertEqual(moved["installation_id"], installation_id)
        self.assertEqual(moved["skill_id"], receipt["skill_id"])
        self.assertFalse(self.target.is_symlink())
        self.assertTrue(destination.is_symlink())
        self.approve("disable", installation_id)
        self.assertFalse(destination.is_symlink())
        self.assertEqual(self.manager.get_installation(installation_id)["state"], "disabled")
        self.approve("enable", installation_id)
        self.assertTrue(destination.is_symlink())
        self.approve("archive", installation_id)
        self.assertFalse(destination.is_symlink())
        self.approve("uninstall", installation_id)
        self.assertEqual(self.manager.get_installation(installation_id)["state"], "uninstalled")
        self.assertIsNotNone(self.manager.repository.get("receipt", receipt["receipt_id"]))
        with self.assertRaises(LifecycleError):
            self.approve("enable", installation_id)

    def test_stale_source_target_revision_and_tampered_plan_fail_before_effects(self):
        plan = self.manager.plan("install", source=self.source, target=self.target)
        changed = copy.deepcopy(plan)
        changed["operation"] = "uninstall"
        with self.assertRaises(LifecycleError):
            self.manager.apply(changed, approve_plan_id=plan["plan_id"])
        (self.source / "payload").write_text("H2", encoding="utf-8")
        with self.assertRaises(LifecycleError) as caught:
            self.manager.apply(plan, approve_plan_id=plan["plan_id"])
        self.assertEqual(caught.exception.code, "stale_plan")
        self.assertFalse(self.target.is_symlink())
        _, receipt = self.install()
        renewal = self.manager.plan("renew", installation_id=receipt["installation_id"])
        self.approve("revoke", receipt["installation_id"])
        with self.assertRaises(LifecycleError):
            self.manager.apply(renewal, approve_plan_id=renewal["plan_id"])
        self.assertEqual(self.manager.verify(receipt["installation_id"])["approval"]["state"], "revoked")

    def test_completed_transaction_id_retry_is_exact_and_conflicts_rejected(self):
        plan = self.manager.plan("install", source=self.source, target=self.target)
        first = self.manager.apply(plan, approve_plan_id=plan["plan_id"], transaction_id="fixed-key")
        stat = self.target.lstat()
        again = Manager(self.root).apply(plan, approve_plan_id=plan["plan_id"], transaction_id="fixed-key")
        self.assertEqual(first, again)
        self.assertEqual(stat.st_ino, self.target.lstat().st_ino)
        other = self.manager.plan("renew", installation_id=first["installation_id"])
        with self.assertRaises(LifecycleError) as caught:
            self.manager.apply(other, approve_plan_id=other["plan_id"], transaction_id="fixed-key")
        self.assertEqual(caught.exception.code, "transaction_conflict")

    def test_unsafe_overlap_foreign_target_and_missing_parent_rejected(self):
        for target in (self.source / "nested", self.manager.state_root / "nested", self.root / "absent" / "target"):
            with self.assertRaises(LifecycleError):
                self.manager.plan("install", source=self.source, target=target)
        self.target.write_text("foreign")
        with self.assertRaises(LifecycleError):
            self.manager.plan("install", source=self.source, target=self.target)
        self.assertEqual(self.target.read_text(), "foreign")

    def test_recomputed_checksum_does_not_authorize_impossible_plan(self):
        plan = self.manager.plan("install", source=self.source, target=self.target)
        for mutate in (
            lambda value: value["after"].update(state="uninstalled"),
            lambda value: value["after"].update(authorization={"state": "valid", "grant_id": "forged"}),
            lambda value: value["version"].update(parent_version_id="foreign"),
            lambda value: value["steps"][0].update(after={"kind": "missing"}),
            lambda value: value.update(operation="renew"),
            lambda value: value["version"]["snapshot"].update(normalization="unknown"),
        ):
            with self.subTest(mutation=mutate):
                changed = copy.deepcopy(plan)
                mutate(changed)
                changed["plan_id"] = digest({key: value for key, value in changed.items() if key != "plan_id"})
                with self.assertRaises(LifecycleError):
                    self.manager.apply(changed, approve_plan_id=changed["plan_id"])
                self.assertFalse(self.target.is_symlink())

    def test_distinct_skills_deduplicate_snapshot_not_version_or_identity(self):
        _, first = self.install()
        other_target = self.host / "same-content"
        second_plan = self.manager.plan("install", source=self.source, target=other_target, name="example")
        second = self.manager.apply(second_plan, approve_plan_id=second_plan["plan_id"])
        self.assertNotEqual(first["skill_id"], second["skill_id"])
        self.assertNotEqual(first["version_id"], second["version_id"])
        self.assertEqual(os.readlink(self.target), os.readlink(other_target))
        self.approve("rename", first["installation_id"], name="new-label")
        restarted = Manager(self.root)
        self.assertEqual(restarted.get_installation(first["installation_id"])["name"], "new-label")
        self.assertEqual(restarted.get_installation(first["installation_id"])["skill_id"], first["skill_id"])

    def test_rollback_preserves_original_version_and_grant_binds_generation_target(self):
        _, first = self.install()
        original_version = self.manager.repository.get("version", first["version_id"])
        (self.source / "payload").write_text("H2")
        self.approve("update", first["installation_id"], source=self.source)
        rolled = self.approve("rollback", first["installation_id"], version_id=first["version_id"])
        self.assertEqual((self.target / "payload").read_text(), "H1")
        self.assertEqual(self.manager.repository.get("version", first["version_id"]), original_version)
        grant = self.manager.repository.get("grant", rolled["grant_id"])["data"]
        self.assertEqual(grant["target"], str(self.target))
        installation = self.manager.repository.get("installation", first["installation_id"])
        self.assertEqual(grant["installation_generation"], installation["data"]["generation"])

    def test_target_parent_replacement_and_state_symlink_are_rejected(self):
        plan = self.manager.plan("install", source=self.source, target=self.target)
        displaced = self.root / "displaced-host"
        self.host.rename(displaced)
        self.host.mkdir()
        with self.assertRaises(LifecycleError):
            self.manager.apply(plan, approve_plan_id=plan["plan_id"])
        self.assertFalse(self.target.is_symlink())
        other_project = self.root / "other-project"
        other_project.mkdir()
        (other_project / ".skills-auditor-local").symlink_to(self.root / ".skills-auditor-local", target_is_directory=True)
        with self.assertRaises(LifecycleError):
            Manager(other_project)

    def test_clean_exact_legacy_migration_and_invalid_drift_rejection(self):
        from skills_auditor.integration import IntegrationSpec, IntegrationTarget, apply_integration_plan, build_integration_plan
        legacy_source = self.root / "legacy-sources"
        legacy_skill = legacy_source / "legacy"
        legacy_skill.mkdir(parents=True)
        (legacy_skill / "SKILL.md").write_text("---\nname: legacy\ndescription: migration fixture\n---\n# Legacy\n")
        legacy_host = self.root / "legacy-host"
        spec = IntegrationSpec(project_root=self.root, sources=(legacy_source,), targets=(IntegrationTarget("fixture", root=legacy_host),))
        receipt, _ = apply_integration_plan(build_integration_plan(spec))
        target = legacy_host / "legacy"
        plan = self.manager.plan("migrate", source=legacy_skill, target=target, legacy_receipt=receipt)
        migrated = self.manager.apply(plan, approve_plan_id=plan["plan_id"])
        self.assertEqual(self.manager.verify(migrated["installation_id"])["approval"]["state"], "valid")
        self.assertNotEqual(target.resolve(), legacy_skill)
        other = legacy_host / "unrelated"
        other.symlink_to(legacy_skill, target_is_directory=True)
        with self.assertRaises(LifecycleError):
            self.manager.plan("migrate", source=legacy_skill, target=other, legacy_receipt=receipt)

    def test_clean_observation_does_not_stale_plan_but_invalidation_does(self):
        _, receipt = self.install()
        plan = self.manager.plan("renew", installation_id=receipt["installation_id"])
        before = self.manager.repository.get("installation", receipt["installation_id"])
        self.manager.verify(receipt["installation_id"])
        self.assertEqual(self.manager.repository.get("installation", receipt["installation_id"]), before)
        self.manager.apply(plan, approve_plan_id=plan["plan_id"])
        later = self.manager.plan("renew", installation_id=receipt["installation_id"])
        raw = os.readlink(self.target)
        self.target.unlink()
        self.manager.verify(receipt["installation_id"])
        self.target.symlink_to(raw, target_is_directory=True)
        with self.assertRaises(LifecycleError) as caught:
            self.manager.apply(later, approve_plan_id=later["plan_id"])
        self.assertEqual(caught.exception.code, "stale_plan")

    def test_missing_grant_or_mismatched_authorization_cannot_verify_valid(self):
        _, receipt = self.install()
        installation = self.manager.repository.get("installation", receipt["installation_id"])
        changed = copy.deepcopy(installation["data"])
        changed["authorization"]["grant_id"] = "missing-grant"
        self.manager.repository.put("installation", receipt["installation_id"], changed, expected_revision=installation["revision"])
        result = self.manager.verify(receipt["installation_id"])
        self.assertFalse(result["valid"])
        self.assertEqual(result["approval"]["state"], "invalidated")
        self.assertIn("grant_binding", result["approval"]["reason_codes"])

    def test_source_read_error_and_target_read_error_fail_closed(self):
        plan = self.manager.plan("install", source=self.source, target=self.target)
        with patch("skills_auditor.lifecycle.engine.inspect_source", side_effect=OSError("source unreadable")):
            with self.assertRaises(LifecycleError) as caught:
                self.manager.apply(plan, approve_plan_id=plan["plan_id"])
        self.assertEqual(caught.exception.code, "stale_plan")
        self.assertFalse(self.target.is_symlink())
        _, receipt = self.install()
        original = os.readlink

        def fail(path, *args, **kwargs):
            if Path(path) == self.target:
                raise OSError("link unreadable")
            return original(path, *args, **kwargs)

        with patch("skills_auditor.lifecycle.engine.os.readlink", side_effect=fail):
            result = self.manager.verify(receipt["installation_id"])
        self.assertFalse(result["valid"])
        self.assertIn("target_link", result["approval"]["reason_codes"])

    def test_recomputed_plan_cannot_overwrite_a_foreign_retargeted_symlink(self):
        _, receipt = self.install()
        plan = self.manager.plan("renew", installation_id=receipt["installation_id"])
        foreign = self.root / "foreign"
        foreign.mkdir()
        self.target.unlink()
        self.target.symlink_to(foreign, target_is_directory=True)
        info = self.target.lstat()
        plan["steps"][0]["before"] = {"kind": "symlink", "link": str(foreign), "identity": [info.st_dev, info.st_ino, info.st_ctime_ns]}
        plan["plan_id"] = digest({key: value for key, value in plan.items() if key != "plan_id"})
        with self.assertRaises(LifecycleError):
            self.manager.apply(plan, approve_plan_id=plan["plan_id"])
        self.assertEqual(self.target.resolve(), foreign)

    def test_active_snapshot_corruption_and_restoration_do_not_restore_grant(self):
        _, receipt = self.install()
        payload = self.target / "payload"
        original_mode = payload.stat().st_mode & 0o777
        payload.chmod(original_mode | 0o200)
        payload.write_text("corrupt")
        failed = self.manager.verify(receipt["installation_id"])
        self.assertEqual(failed["approval"]["state"], "invalidated")
        self.assertIn("snapshot_tree", failed["approval"]["reason_codes"])
        payload.write_text("H1")
        payload.chmod(original_mode)
        restored = self.manager.verify(receipt["installation_id"])
        self.assertTrue(restored["integrity"]["valid"])
        self.assertEqual(restored["approval"]["state"], "invalidated")
        self.approve("renew", receipt["installation_id"])
        self.assertTrue(self.manager.verify(receipt["installation_id"])["valid"])

    def test_revoke_does_not_depend_on_healthy_target_or_snapshot(self):
        _, receipt = self.install()
        self.target.unlink()
        self.target.write_text("foreign")
        plan = self.manager.plan("revoke", installation_id=receipt["installation_id"])
        revoked = self.manager.apply(plan, approve_plan_id=plan["plan_id"], transaction_id="revoke-foreign")
        self.assertEqual(revoked["status"], "completed")
        self.assertEqual(self.target.read_text(), "foreign")
        self.assertEqual(self.manager.verify(receipt["installation_id"])["approval"]["state"], "revoked")
        self.assertEqual(self.manager.apply(plan, approve_plan_id=plan["plan_id"], transaction_id="revoke-foreign"), revoked)

    def test_missing_receipt_and_uncommitted_transaction_invalidate_approval(self):
        _, receipt = self.install()
        original_get = self.manager.repository.get

        def missing(kind, identifier):
            if kind == "receipt" and identifier == receipt["receipt_id"]:
                return None
            return original_get(kind, identifier)

        with patch.object(self.manager.repository, "get", side_effect=missing):
            missing_receipt = self.manager.verify(receipt["installation_id"])
        self.assertFalse(missing_receipt["valid"])
        self.assertIn("receipt_record", missing_receipt["approval"]["reason_codes"])
        self.assertEqual(missing_receipt["approval"]["state"], "invalidated")
        renewed = self.approve("renew", receipt["installation_id"])
        transaction = self.manager.repository.get("transaction", renewed["transaction_id"])
        damaged = copy.deepcopy(transaction["data"])
        damaged["state"] = "applying"
        self.manager.repository.put("transaction", renewed["transaction_id"], damaged, expected_revision=transaction["revision"])
        invalid_tx = self.manager.verify(receipt["installation_id"])
        self.assertFalse(invalid_tx["valid"])
        self.assertIn("transaction_record", invalid_tx["approval"]["reason_codes"])
        self.assertEqual(invalid_tx["approval"]["state"], "invalidated")

    def test_historical_evidence_check_never_requires_mutable_source(self):
        _, receipt = self.install()
        (self.source / "payload").unlink()
        (self.source / "SKILL.md").unlink()
        self.source.rmdir()
        self.assertTrue(self.manager.verify(receipt["installation_id"])["valid"])

    def test_revocation_is_a_registry_only_denial_even_when_target_cannot_be_read(self):
        _, receipt = self.install()
        with patch("skills_auditor.lifecycle.engine._entry", side_effect=OSError("target not readable")):
            plan = self.manager.plan("revoke", installation_id=receipt["installation_id"])
            self.assertEqual(plan["steps"], [])
            denied = self.manager.apply(plan, approve_plan_id=plan["plan_id"])
        self.assertEqual(denied["status"], "completed")
        self.assertEqual(self.manager.get_installation(receipt["installation_id"])["authorization"]["state"], "revoked")

    def test_planner_rejects_invalid_transitions_and_ambiguous_inputs(self):
        _, receipt = self.install()
        initial_receipts = self.manager.repository.list("receipt")
        cases = (
            ("unknown", {}), ("install", {"source": self.source}),
            ("install", {"source": self.source, "target": self.host / "new", "installation_id": receipt["installation_id"]}),
            ("enable", {}), ("move", {}), ("renew", {"source": self.source}),
            ("renew", {"target": self.host / "other"}), ("renew", {"skill_id": "wrong-skill"}),
            ("renew", {"version_id": "wrong-version"}), ("renew", {"name": " "}),
            ("update", {}), ("rollback", {}),
            ("update", {"source": self.manager.state_root}),
        )
        for operation, supplied in cases:
            with self.subTest(operation=operation, supplied=supplied):
                arguments = {"installation_id": receipt["installation_id"]} if operation != "install" else {}
                arguments.update(supplied)
                with self.assertRaises(LifecycleError):
                    self.manager.plan(operation, **arguments)
                self.assertEqual((self.target / "payload").read_text(), "H1")
                self.assertEqual(self.manager.repository.list("receipt"), initial_receipts)
        self.approve("disable", receipt["installation_id"])
        with self.assertRaises(LifecycleError):
            self.manager.plan("update", source=self.source, installation_id=receipt["installation_id"])

    def test_contract_rejects_structural_and_rehashed_semantic_corruption(self):
        plan = self.manager.plan("install", source=self.source, target=self.target)
        malformed = [None, {}, {**plan, "unexpected": True}, {**plan, "created_at": float("nan")}]
        for value in malformed:
            with self.subTest(malformed=value):
                with self.assertRaises(LifecycleError):
                    self.manager.apply(value, approve_plan_id=plan["plan_id"])
        mutations = (
            lambda p: p.update(project_root="/other"),
            lambda p: p.update(operation="other"),
            lambda p: p.update(installation_id="../escape"),
            lambda p: p["skill"].update(unexpected=True),
            lambda p: p.update(expected_revision=True),
            lambda p: p["after"].update(installation_id="other"),
            lambda p: p["version"].update(skill_id="other"),
            lambda p: p.update(source=None),
            lambda p: p.update(legacy_receipt={"receipt_id": "unrelated"}),
            lambda p: p["version"]["snapshot"].update(source_tree_sha256="invalid"),
            lambda p: p["version"]["snapshot"].update(path="/outside/tree"),
            lambda p: p.update(source=str(self.manager.state_root)),
            lambda p: p.update(steps=[]),
            lambda p: p["version"]["provenance"].update(operation="unrelated"),
            lambda p: p["steps"][0].update(unexpected=True),
            lambda p: p["steps"][0].update(parent_identity=[True, 1]),
            lambda p: p["steps"][0].update(after={"kind": "foreign"}),
            lambda p: p["steps"][0]["after"].update(link="/outside/tree"),
            lambda p: p["steps"][0].update(before={"kind": "symlink", "link": "/foreign", "identity": [1, 2, 3]}),
            lambda p: p["steps"][0].update(path=str(self.host / "other")),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                value = copy.deepcopy(plan)
                mutate(value)
                value["plan_id"] = digest({key: item for key, item in value.items() if key != "plan_id"})
                with self.assertRaises(LifecycleError):
                    self.manager.apply(value, approve_plan_id=value["plan_id"])
                self.assertFalse(self.target.is_symlink())
                self.assertEqual(self.manager.repository.list("transaction"), [])

    def test_existing_skill_can_gain_a_second_installation_without_changing_version_record(self):
        _, receipt = self.install()
        original = self.manager.repository.get("version", receipt["version_id"])
        plan = self.manager.plan("install", source=self.source, target=self.host / "second", skill_id=receipt["skill_id"])
        second = self.manager.apply(plan, approve_plan_id=plan["plan_id"])
        self.assertNotEqual(second["installation_id"], receipt["installation_id"])
        self.assertEqual(second["skill_id"], receipt["skill_id"])
        self.assertEqual(second["version_id"], receipt["version_id"])
        self.assertEqual(self.manager.repository.get("version", receipt["version_id"]), original)
        self.assertEqual(len(self.manager.list_installations()), 2)

    def test_failed_verification_retains_bounded_expected_actual_not_private_fields(self):
        _, receipt = self.install()
        error = LifecycleError("snapshot_unreadable", "cannot read", details={"path": str(self.target), "actual": None, "prompt": "private prompt", "environment": {"SECRET": "private"}})
        with patch("skills_auditor.lifecycle.engine.verify_snapshot", side_effect=error):
            result = self.manager.verify(receipt["installation_id"])
        check = next(item for item in result["integrity"]["checks"] if item["code"] == "snapshot_tree")
        self.assertEqual(len(check["expected"]), 64)
        self.assertIsNone(check["actual"])
        self.assertEqual(check["error"]["details"]["path"], str(self.target))
        self.assertNotIn("prompt", check["error"]["details"])
        self.assertNotIn("environment", check["error"]["details"])

    def test_projection_failure_cannot_undo_observed_denial_or_allow_cached_use(self):
        from skills_auditor.lifecycle.status import preflight
        _, receipt = self.install()
        self.manager.verify(receipt["installation_id"])
        self.target.unlink()
        original = self.manager.repository.put

        def fail(kind, identifier, data, **kwargs):
            if kind == "status":
                raise OSError("projection unavailable")
            return original(kind, identifier, data, **kwargs)

        with patch.object(self.manager.repository, "put", side_effect=fail):
            self.assertEqual(preflight(self.manager, receipt["installation_id"], refresh=True)["decision"], "block")
        self.assertEqual(self.manager.get_installation(receipt["installation_id"])["authorization"]["state"], "invalidated")
        self.assertEqual(preflight(self.manager, receipt["installation_id"], refresh=False)["decision"], "block")

    def test_missing_version_missing_parent_and_unknown_authorization_never_revalidate(self):
        from skills_auditor.lifecycle.status import preflight
        _, receipt = self.install()
        self.manager.verify(receipt["installation_id"])
        original = self.manager.repository.get

        def missing(kind, identifier):
            if kind == "version":
                return None
            return original(kind, identifier)

        with patch.object(self.manager.repository, "get", side_effect=missing):
            self.assertFalse(self.manager.verify(receipt["installation_id"])["valid"])
        self.assertEqual(preflight(self.manager, receipt["installation_id"])["decision"], "block")
        self.approve("renew", receipt["installation_id"])
        authorization = self.manager.get_installation(receipt["installation_id"])["authorization"]
        projection = self.manager.repository.get("authorization", authorization["grant_id"])
        self.manager.repository.put("authorization", authorization["grant_id"], {**authorization, "state": "unknown"}, expected_revision=projection["revision"])
        self.assertFalse(self.manager.verify(receipt["installation_id"])["valid"])
        self.assertEqual(self.manager.get_installation(receipt["installation_id"])["authorization"]["state"], "invalidated")
        self.approve("renew", receipt["installation_id"])
        self.manager.verify(receipt["installation_id"])
        self.host.rename(self.root / "displaced-host")
        self.assertFalse(self.manager.verify(receipt["installation_id"])["valid"])
        self.assertEqual(preflight(self.manager, receipt["installation_id"])["decision"], "block")

    def test_completed_retry_rechecks_receipt_identity_and_transaction_approval(self):
        for damage in ("receipt", "transaction"):
            with self.subTest(damage=damage):
                target = self.host / damage
                plan = self.manager.plan("install", source=self.source, target=target)
                receipt = self.manager.apply(plan, approve_plan_id=plan["plan_id"], transaction_id=damage)
                identifier = receipt["receipt_id"] if damage == "receipt" else damage
                record = self.manager.repository.get(damage, identifier)
                altered = copy.deepcopy(record["data"])
                altered["installation_id" if damage == "receipt" else "approved_plan_id"] = "unrelated"
                self.manager.repository.put(damage, identifier, altered, expected_revision=record["revision"])
                with self.assertRaises(LifecycleError):
                    self.manager.apply(plan, approve_plan_id=plan["plan_id"], transaction_id=damage)

    def test_snapshot_damage_reports_distinct_observed_hashes_without_extra_scan(self):
        _, receipt = self.install()
        payload = self.target / "payload"
        mode = payload.stat().st_mode & 0o777
        actuals = []
        for body in ("H2", "H3"):
            payload.chmod(mode | 0o200)
            payload.write_text(body)
            payload.chmod(mode)
            verification = self.manager.verify(receipt["installation_id"])
            check = next(item for item in verification["integrity"]["checks"] if item["code"] == "snapshot_tree")
            self.assertRegex(check["actual"], r"^[a-f0-9]{64}$")
            actuals.append(check["actual"])
        self.assertNotEqual(*actuals)
        with patch("skills_auditor.lifecycle.engine.verify_snapshot", side_effect=OSError("unreadable")):
            unknown = self.manager.verify(receipt["installation_id"])
        self.assertIsNone(next(item for item in unknown["integrity"]["checks"] if item["code"] == "snapshot_tree")["actual"])

    def test_rehashed_plan_rejects_invalid_names_and_timestamps_before_any_intent(self):
        plan = self.manager.plan("install", source=self.source, target=self.target)
        mutations = (
            lambda p: p.update(created_at=None),
            lambda p: p.update(created_at="2026-09-07T12:00:00"),
            lambda p: p["skill"].update(name=[]),
            lambda p: p["skill"].update(name="   "),
            lambda p: p["skill"].update(created_at="not-a-time"),
            lambda p: p["version"].update(created_at={}),
            lambda p: p["after"].update(created_at=[]),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                changed = copy.deepcopy(plan)
                mutate(changed)
                changed["plan_id"] = digest({key: value for key, value in changed.items() if key != "plan_id"})
                with self.assertRaises(LifecycleError):
                    self.manager.apply(changed, approve_plan_id=changed["plan_id"])
                self.assertFalse(self.target.is_symlink())
                self.assertEqual(self.manager.repository.list("transaction"), [])
                self.assertEqual(self.manager.repository.list("receipt"), [])

    def test_failed_core_verification_write_cannot_be_washed_by_restored_bytes(self):
        _, receipt = self.install()
        self.manager.verify(receipt["installation_id"])
        raw = os.readlink(self.target)
        self.target.unlink()
        original = self.manager.repository.put

        def fail(kind, identifier, data, **kwargs):
            if kind == "verification":
                raise LifecycleError("repository_io_error", "verification unavailable")
            return original(kind, identifier, data, **kwargs)

        with patch.object(self.manager.repository, "put", side_effect=fail):
            with self.assertRaises(LifecycleError):
                self.manager.verify(receipt["installation_id"])
        self.target.symlink_to(raw, target_is_directory=True)
        result = self.manager.verify(receipt["installation_id"])
        self.assertTrue(result["integrity"]["valid"])
        self.assertEqual(result["approval"]["state"], "invalidated")
        self.assertIn("verification_interrupted", result["approval"]["reason_codes"])
        self.assertEqual(result["grant_id"], receipt["grant_id"])
        self.assertEqual(len(self.manager.repository.list("receipt")), 1)
        self.approve("renew", receipt["installation_id"])
        self.assertTrue(self.manager.verify(receipt["installation_id"])["valid"])

    def test_explicit_new_grant_supersedes_unfinished_old_verification(self):
        _, receipt = self.install()

        def fail(name, data):
            if name == "verification:started":
                raise OSError("interrupted observation")

        with self.assertRaises(OSError):
            self.manager.verify(receipt["installation_id"], checkpoint=fail)
        renewed = self.approve("renew", receipt["installation_id"])
        self.assertNotEqual(renewed["grant_id"], receipt["grant_id"])
        self.assertTrue(self.manager.verify(receipt["installation_id"])["valid"])

    def test_cached_integrity_gate_preserves_age_on_clean_and_commits_denial_on_failure(self):
        _, receipt = self.install()
        identifier = receipt["installation_id"]
        with self.assertRaises(LifecycleError) as caught:
            self.manager.check_cached_integrity(identifier)
        self.assertEqual(caught.exception.code, "verification_required")
        self.assertIsNone(self.manager.repository.get("verification-run", identifier))
        self.manager.verify(identifier)
        marker = self.manager.repository.get("verification-run", identifier)["data"]
        status = self.manager.repository.get("status", identifier)
        observations = self.manager.repository.list("verification")
        self.assertTrue(self.manager.check_cached_integrity(identifier))
        self.assertEqual(self.manager.repository.get("verification-run", identifier)["data"], marker)
        self.assertEqual(self.manager.repository.get("status", identifier), status)
        self.assertEqual(self.manager.repository.list("verification"), observations)
        self.target.unlink()
        self.assertFalse(self.manager.check_cached_integrity(identifier))
        self.assertEqual(self.manager.get_installation(identifier)["authorization"]["state"], "invalidated")
        self.assertEqual(len(self.manager.repository.list("verification")), len(observations) + 1)

    def test_interrupted_cached_gate_cannot_reauthorize_its_old_grant(self):
        _, receipt = self.install()
        identifier = receipt["installation_id"]
        self.manager.verify(identifier)

        def fail(name, data):
            if name == "verification:observed":
                raise OSError("cached probe interrupted")

        with self.assertRaises(OSError):
            self.manager.check_cached_integrity(identifier, checkpoint=fail)
        marker = self.manager.repository.get("verification-run", identifier)["data"]
        self.assertEqual(marker["state"], "in_progress")
        self.assertEqual(marker["grant_id"], receipt["grant_id"])
        with self.assertRaises(LifecycleError):
            self.manager.check_cached_integrity(identifier)
        self.assertEqual(self.manager.verify(identifier)["approval"]["state"], "invalidated")
        self.approve("renew", identifier)
        self.assertTrue(self.manager.verify(identifier)["valid"])
        self.assertTrue(self.manager.check_cached_integrity(identifier))

    def test_incident_consumer_failure_cannot_undo_core_denial_or_return_old_status(self):
        _, receipt = self.install()
        identifier = receipt["installation_id"]
        self.manager.verify(identifier)
        self.target.unlink()
        with patch("skills_auditor.lifecycle.incidents.record_verification", side_effect=LifecycleError("incident_io_error", "consumer unavailable")):
            with self.assertRaises(LifecycleError):
                self.manager.verify(identifier)
        installation = self.manager.get_installation(identifier)
        self.assertEqual(installation["authorization"]["state"], "invalidated")
        from skills_auditor.lifecycle.status import preflight
        self.assertEqual(preflight(self.manager, identifier)["decision"], "block")
