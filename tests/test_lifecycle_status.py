"""Durable, host-neutral status never substitutes for continuous enforcement."""

import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import jsonschema

from skills_auditor.lifecycle.common import LifecycleError, digest
from skills_auditor.lifecycle.repository import Repository
from skills_auditor.lifecycle.status import preflight, publish_status, read_status, render_status, unknown_status


class TestLifecycleStatus(unittest.TestCase):
    def test_incident_links_come_from_current_durable_index_not_cached_payload(self):
        from skills_auditor.lifecycle.engine import Manager
        project = self.root / "managed-project"
        project.mkdir()
        source = project / "candidate"
        source.mkdir()
        (source / "SKILL.md").write_text("# Example\n")
        manager = Manager(project)
        self.addCleanup(manager.repository.close)
        target = project / "installed"
        plan = manager.plan("install", source=source, target=target)
        receipt = manager.apply(plan, approve_plan_id=plan["plan_id"])
        target.unlink()
        verification = manager.verify(receipt["installation_id"])
        index = manager.repository.get("verification-incidents", verification["verification_id"])["data"]
        status = read_status(manager, receipt["installation_id"])
        self.assertEqual(status["incident_ids"], index["incident_ids"])
        cached = manager.repository.get("status", receipt["installation_id"])
        manager.repository.put("status", receipt["installation_id"], {**cached["data"], "incident_ids": ["foreign-reference"]}, expected_revision=cached["revision"])
        self.assertEqual(read_status(manager, receipt["installation_id"])["incident_ids"], index["incident_ids"])

    def test_cached_success_requires_each_core_check_exactly_once(self):
        self.store()
        record = self.repository.get("verification", self.verification["verification_id"])
        checks = self.verification["integrity"]["checks"]
        for malformed in ([{"code": "arbitrary", "valid": True}], checks[:-1], checks + [checks[0]]):
            with self.subTest(checks=malformed):
                changed = copy.deepcopy(self.verification)
                changed["integrity"]["checks"] = malformed
                record = self.repository.put("verification", self.verification["verification_id"], changed, expected_revision=record["revision"])
                self.assertEqual(read_status(self.manager, self.installation["installation_id"], now=self.now)["severity"], "error")
                with self.assertRaises(LifecycleError):
                    publish_status(self.repository, self.installation, changed)

    def test_completed_marker_must_bind_current_grant_version_and_generation(self):
        self.store()
        original = self.repository.get("verification-run", "installation-one")["data"]
        for field, value in (("grant_id", "foreign-grant"), ("version_id", "foreign-version"), ("generation", 999)):
            with self.subTest(field=field):
                record = self.repository.get("verification-run", "installation-one")
                self.repository.put("verification-run", "installation-one", {**original, field: value}, expected_revision=record["revision"])
                self.assertEqual(self.status()["severity"], "error")
                self.assertEqual(preflight(self.manager, "installation-one", now=self.now)["decision"], "block")

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="skills-auditor-status-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repository = Repository(self.root / "state")
        self.addCleanup(self.repository.close)
        self.manager = SimpleNamespace(repository=self.repository)
        self.now = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)
        self.installation = {
            "installation_id": "installation-one", "skill_id": "skill-one", "name": "Example",
            "version_id": "version-one", "target": str(self.root / "host"), "state": "active",
            "generation": 1, "receipt_id": "receipt-one", "last_transaction_id": "transaction-one",
            "authorization": {"state": "valid", "grant_id": "grant-one", "reason_codes": []},
        }
        self.verification = {
            "schema_version": "skills-auditor-lifecycle-verification/v1", "verification_id": "verification-one",
            "observed_at": "2026-09-07T12:00:00Z", "installation_id": "installation-one", "skill_id": "skill-one",
            "version_id": "version-one", "receipt_id": "receipt-one", "grant_id": "grant-one",
            "integrity": {"valid": True, "checks": [{"code": code, "valid": True} for code in ("snapshot_tree", "target_link", "receipt_record", "transaction_record", "grant_binding")]},
            "approval": {"state": "valid", "requires_reapproval": False, "reason_codes": []}, "valid": True,
        }
        self.repository.put("installation", "installation-one", self.installation)
        self.repository.put("grant", "grant-one", {
            "grant_id": "grant-one", "installation_id": "installation-one", "skill_id": "skill-one",
            "version_id": "version-one", "target": self.installation["target"], "installation_generation": 1,
            "plan_id": "plan-one",
        })
        self.repository.put("authorization", "grant-one", self.installation["authorization"])
        self.repository.put("receipt", "receipt-one", {
            "receipt_id": "receipt-one", "installation_id": "installation-one", "skill_id": "skill-one",
            "version_id": "version-one", "grant_id": "grant-one", "plan_id": "plan-one", "status": "completed",
        })

    def store(self):
        with self.repository.atomic():
            plan = {"schema_version": "skills-auditor-lifecycle-plan/v1", "operation": "rename", "installation_id": self.installation["installation_id"],
                    "after": {key: self.installation[key] for key in ("version_id", "target", "state")}}
            plan["plan_id"] = digest(plan)
            self.plan_id = plan["plan_id"]
            previous_receipt = self.repository.get("receipt", "receipt-one")
            if previous_receipt:
                self.repository.put("receipt", "receipt-one", {**previous_receipt["data"], "schema_version": "skills-auditor-lifecycle-receipt/v1", "transaction_id": "transaction-one", "operation": "rename", "plan_id": self.plan_id}, expected_revision=previous_receipt["revision"])
            previous_grant = self.repository.get("grant", "grant-one")
            if previous_grant:
                self.repository.put("grant", "grant-one", {**previous_grant["data"], "transaction_id": "transaction-one", "plan_id": self.plan_id}, expected_revision=previous_grant["revision"])
            previous_tx = self.repository.get("transaction", "transaction-one")
            self.repository.put("transaction", "transaction-one", {"transaction_id": "transaction-one", "state": "completed", "receipt_id": "receipt-one", "approved_plan_id": self.plan_id, "plan": plan}, expected_revision=previous_tx["revision"] if previous_tx else 0)
            self.repository.put("verification", self.verification["verification_id"], self.verification)
            latest = self.repository.get("latest-verification", "installation-one")
            self.repository.put("latest-verification", "installation-one", {"verification_id": self.verification["verification_id"]}, expected_revision=latest["revision"] if latest else 0)
            marker = self.repository.get("verification-run", "installation-one")
            self.repository.put("verification-run", "installation-one", {"state": "completed", "verification_id": self.verification["verification_id"], "grant_id": self.installation["authorization"]["grant_id"], "version_id": self.installation["version_id"], "generation": self.installation["generation"]}, expected_revision=marker["revision"] if marker else 0)
            return publish_status(self.repository, self.installation, self.verification)

    def change_installation(self, **changes):
        self.installation.update(changes)
        previous = self.repository.get("installation", "installation-one")
        self.repository.put("installation", "installation-one", self.installation, expected_revision=previous["revision"])

    def status(self, **kwargs):
        return read_status(self.manager, "installation-one", now=kwargs.pop("now", self.now), **kwargs)

    def test_fresh_status_is_durable_schema_valid_and_pure_read(self):
        saved = self.store()
        old = self.repository.get("status", "installation-one")
        with patch.object(self.repository, "put", side_effect=AssertionError("reader wrote")):
            status = self.status()
        self.assertEqual(status["approval"]["state"], "valid")
        self.assertEqual(status["freshness"]["state"], "fresh")
        self.assertEqual(status["severity"], "ok")
        self.assertEqual(status["plan_id"], self.plan_id)
        self.assertEqual(saved, old["data"])
        self.assertEqual(self.repository.get("status", "installation-one"), old)
        with Repository(self.root / "state", create=False) as restarted:
            self.assertEqual(read_status(SimpleNamespace(repository=restarted), "installation-one", now=self.now), status)
        schema_path = Path(__file__).resolve().parents[1] / "skills_auditor/schemas/lifecycle-status-v1.schema.json"
        schema = json.loads(schema_path.read_text())
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.validate(status, schema, format_checker=jsonschema.FormatChecker())
        jsonschema.validate({**status, "future_optional_field": {"anything": True}}, schema)
        for malformed in ({**status, "severity": "great"}, {**status, "freshness": {**status["freshness"], "max_age_seconds": True}}):
            with self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate(malformed, schema)

    def test_preflight_deterministic_fresh_stale_and_future_policies(self):
        self.store()
        for delta, freshness, decision, exit_code in ((0, "fresh", "proceed", 0), (300, "fresh", "proceed", 0), (301, "stale", "warn", 4), (-1, "unknown", "block", 3)):
            with self.subTest(seconds=delta):
                result = preflight(self.manager, "installation-one", now=self.now + timedelta(seconds=delta))
                self.assertEqual(result["status"]["freshness"]["state"], freshness)
                self.assertEqual((result["decision"], result["exit_code"]), (decision, exit_code))
        self.assertIn("observation_in_future", self.status(now=self.now - timedelta(seconds=1))["reason_codes"])

    def test_never_verified_and_unknown_installation_fail_closed_without_creating_state(self):
        status = self.status()
        self.assertEqual(status["approval"]["state"], "unknown")
        self.assertIn("never_verified", status["reason_codes"])
        self.assertIsNone(self.repository.get("status", "installation-one"))
        absent = read_status(self.manager, "absent", now=self.now)
        self.assertEqual(absent["lifecycle_state"], "unknown")
        self.assertEqual(absent["severity"], "error")
        self.assertIn("installation_unknown", absent["reason_codes"])

    def test_current_revocation_cannot_be_hidden_by_fresh_old_projection(self):
        self.store()
        self.change_installation(authorization={"state": "revoked", "grant_id": "grant-one", "reason_codes": ["explicit_revocation"]})
        status = self.status()
        self.assertEqual(status["authorization_state"], "revoked")
        self.assertEqual(status["approval"]["state"], "invalidated")
        self.assertTrue(status["approval"]["requires_reapproval"])
        self.assertIn("explicit_revocation", status["reason_codes"])
        self.assertEqual(preflight(self.manager, "installation-one", now=self.now)["decision"], "block")

    def test_invalidated_observation_preserves_stable_deduplicated_reasons(self):
        authorization = {"state": "invalidated", "grant_id": "grant-one", "reason_codes": ["target_link"]}
        self.change_installation(authorization=authorization)
        self.repository.put("authorization", "grant-one", authorization, expected_revision=1)
        self.verification["integrity"]["valid"] = False
        next(check for check in self.verification["integrity"]["checks"] if check["code"] == "target_link")["valid"] = False
        self.verification["approval"] = {"state": "invalidated", "requires_reapproval": True, "reason_codes": ["target_link", "approval_invalidated"]}
        self.verification["valid"] = False
        self.store()
        status = self.status()
        self.assertEqual(status["approval"]["state"], "invalidated")
        self.assertEqual(status["reason_codes"].count("target_link"), 1)
        self.assertEqual(status["severity"], "error")
        self.assertIn("renew", status["recommended_next_action"]["command"])

    def test_nonactive_lifecycle_blocks_even_with_valid_snapshot_and_approval(self):
        for state in ("disabled", "archived", "uninstalled"):
            with self.subTest(state=state):
                self.change_installation(state=state)
                self.verification["verification_id"] = "verification-" + state
                self.store()
                result = preflight(self.manager, "installation-one", now=self.now)
                self.assertEqual(result["status"]["approval"]["state"], "valid")
                self.assertEqual(result["decision"], "block")
                self.assertIn("lifecycle_" + state, result["status"]["reason_codes"])

    def test_new_grant_or_receipt_requires_matching_verification(self):
        self.store()
        for change in ({"receipt_id": "new-receipt"}, {"authorization": {"state": "valid", "grant_id": "new-grant", "reason_codes": []}}, {"version_id": "new-version"}):
            with self.subTest(change=change):
                original = copy.deepcopy(self.installation)
                self.change_installation(**change)
                status = self.status()
                self.assertEqual(status["approval"]["state"], "unknown")
                self.assertEqual(status["severity"], "error")
                self.change_installation(**original)

    def test_corrupt_and_unreadable_projection_are_explicit_unknown_not_fresh(self):
        self.store()
        original_get = self.repository.get
        for error in (LifecycleError("repository_corrupt", "broken checksum"), OSError("cannot read")):
            with self.subTest(error=error):
                def fail_status(kind, identifier):
                    if kind == "status":
                        raise error
                    return original_get(kind, identifier)
                with patch.object(self.repository, "get", side_effect=fail_status):
                    status = self.status()
                self.assertEqual(status["approval"]["state"], "unknown")
                self.assertEqual(status["freshness"]["state"], "unknown")
                self.assertIn("status_unreadable", status["reason_codes"])
        record = self.repository.get("status", "installation-one")
        self.repository.put("status", "installation-one", {**record["data"], "approval": {"state": "invented"}}, expected_revision=record["revision"])
        self.assertIn("status_invalid", self.status()["reason_codes"])

    def test_projection_cannot_contradict_latest_verification_or_grant_record(self):
        self.store()
        original_get = self.repository.get
        for kind in ("verification", "latest-verification", "grant", "authorization", "receipt"):
            with self.subTest(missing=kind):
                with patch.object(self.repository, "get", side_effect=lambda requested, identifier: None if requested == kind else original_get(requested, identifier)):
                    result = self.status()
                self.assertEqual(result["approval"]["state"], "unknown")
                self.assertEqual(result["severity"], "error")

    def test_projection_write_failure_rolls_back_grouped_verification_and_authorization(self):
        original_put = self.repository.put
        def fail_status(kind, identifier, data, expected_revision=0):
            if kind == "status":
                raise LifecycleError("repository_io_error", "injected disk full")
            return original_put(kind, identifier, data, expected_revision=expected_revision)
        before = self.repository.get("installation", "installation-one")
        with self.assertRaises(LifecycleError), patch.object(self.repository, "put", side_effect=fail_status):
            with self.repository.atomic():
                self.change_installation(authorization={"state": "invalidated", "grant_id": "grant-one", "reason_codes": ["target_link"]})
                self.verification["approval"] = {"state": "invalidated", "requires_reapproval": True, "reason_codes": ["target_link"]}
                self.verification["valid"] = False
                self.store()
        self.assertEqual(self.repository.get("installation", "installation-one"), before)
        self.assertIsNone(self.repository.get("verification", "verification-one"))
        self.assertIsNone(self.repository.get("latest-verification", "installation-one"))
        self.assertIsNone(self.repository.get("status", "installation-one"))

    def test_refresh_explicitly_verifies_and_reading_never_does(self):
        self.manager.verify = lambda identifier: self.store()
        with patch.object(self.manager, "verify", wraps=self.manager.verify) as verify:
            self.status()
            verify.assert_not_called()
            result = preflight(self.manager, "installation-one", refresh=True, now=self.now)
            verify.assert_called_once_with("installation-one")
        self.assertEqual(result["decision"], "proceed")
        with patch.object(self.manager, "verify", side_effect=OSError("failed verification write")):
            result = preflight(self.manager, "installation-one", refresh=True, now=self.now)
        self.assertEqual(result["decision"], "block")
        self.assertIn("verification_failed", result["status"]["reason_codes"])

    def test_exact_typed_policy_inputs_reject_invalid_bounds_before_reads(self):
        for maximum in (True, False, -1, 0, 1.5, "300", 86401, None):
            with self.subTest(maximum=maximum), self.assertRaises(LifecycleError) as caught:
                self.status(max_age_seconds=maximum)
            self.assertEqual(caught.exception.exit_code, 2)
        for now in ("today", datetime(2026, 9, 7), 1, True):
            with self.subTest(now=now), self.assertRaises(LifecycleError):
                self.status(now=now)
        with self.assertRaises(LifecycleError):
            preflight(self.manager, "installation-one", refresh=1, now=self.now)

    def test_malformed_timestamps_and_verification_boolean_fail_closed(self):
        self.store()
        original_get = self.repository.get
        for changes in ({"observed_at": "not-a-time"}, {"observed_at": None}, {"observed_at": "2026-09-07T12:00:00"}, {"valid": 1}, {"approval": []}, {"valid": False}, {"integrity": {"valid": True, "checks": []}}):
            with self.subTest(changes=changes):
                def corrupt_verification(kind, identifier):
                    record = original_get(kind, identifier)
                    if kind == "verification":
                        return {**record, "data": {**record["data"], **changes}}
                    return record
                with patch.object(self.repository, "get", side_effect=corrupt_verification):
                    status = self.status()
                self.assertEqual(status["freshness"]["state"], "unknown")
                self.assertEqual(status["severity"], "error")

    def test_human_warning_is_prominent_actionable_and_explicitly_point_in_time(self):
        self.store()
        for delta, marker in ((0, "[OK]"), (301, "[WARN]"), (-1, "[BLOCK]")):
            status = self.status(now=self.now + timedelta(seconds=delta))
            text = render_status(status)
            self.assertTrue(text.startswith(marker))
            for phrase in ("installation-one", "approval=", "authorization=", "freshness=", "lifecycle=", "point-in-time", "skills-audit lifecycle"):
                self.assertIn(phrase, text)

    def test_unknown_repository_helper_and_invalid_inputs_do_not_create_state(self):
        missing_root = self.root / "missing"
        with self.assertRaises(LifecycleError) as caught:
            Repository(missing_root, create=False)
        status = unknown_status(reason=caught.exception.code, now=self.now)
        self.assertFalse(missing_root.exists())
        self.assertIsNone(status["installation_id"])
        self.assertIn("repository_missing", status["reason_codes"])
        self.assertEqual(status["severity"], "error")
        for identifier in (None, "", 1, "line\nbreak"):
            with self.subTest(identifier=identifier), self.assertRaises(LifecycleError):
                read_status(self.manager, identifier)
        with self.assertRaises(LifecycleError):
            unknown_status(reason="")
        self.assertIn("[BLOCK] unknown installation", render_status(status))

    def test_invalid_publication_does_not_replace_previous_projection(self):
        self.store()
        before = self.repository.get("status", "installation-one")
        for changes in ({"observed_at": "bad"}, {"approval": {"state": "valid", "requires_reapproval": False, "reason_codes": ["contradiction"]}, "valid": False}):
            with self.subTest(changes=changes), self.assertRaises(LifecycleError) as caught:
                publish_status(self.repository, self.installation, {**self.verification, **changes})
            self.assertEqual(caught.exception.code, "invalid_status")
        self.assertEqual(self.repository.get("status", "installation-one"), before)

    def test_negative_observation_with_missing_receipt_persists_invalidation_evidence(self):
        self.change_installation(authorization={"state": "invalidated", "grant_id": "grant-one", "reason_codes": ["receipt_record"]})
        self.verification["approval"] = {"state": "invalidated", "requires_reapproval": True, "reason_codes": ["receipt_record", "approval_invalidated"]}
        self.verification["valid"] = False
        original_get = self.repository.get
        with patch.object(self.repository, "get", side_effect=lambda kind, identifier: None if kind == "receipt" else original_get(kind, identifier)):
            published = self.store()
            status = self.status()
        self.assertIsNone(published["plan_id"])
        self.assertEqual(status["approval"]["state"], "invalidated")
        self.assertEqual(status["verification_id"], self.verification["verification_id"])
        self.assertIn("receipt_record", status["reason_codes"])
        self.assertEqual(status["severity"], "error")

    def test_positive_observation_missing_receipt_cannot_publish_success(self):
        original_get = self.repository.get
        with patch.object(self.repository, "get", side_effect=lambda kind, identifier: None if kind == "receipt" else original_get(kind, identifier)):
            with self.assertRaises(LifecycleError) as caught:
                self.store()
        self.assertEqual(caught.exception.code, "invalid_status")
        self.assertIsNone(self.repository.get("status", "installation-one"))
        self.assertIsNone(self.repository.get("verification", "verification-one"))

    def test_malformed_record_payloads_fail_closed_and_cannot_publish(self):
        self.store()
        original_get = self.repository.get
        for kind, data in (("installation", {**self.installation, "generation": True}), ("verification", [])):
            with self.subTest(kind=kind):
                def corrupt_record(requested, identifier):
                    record = original_get(requested, identifier)
                    return {**record, "data": data} if requested == kind else record
                with patch.object(self.repository, "get", side_effect=corrupt_record):
                    result = self.status()
                self.assertEqual(result["approval"]["state"], "unknown")
                self.assertEqual(result["severity"], "error")
        with self.assertRaises(LifecycleError):
            publish_status(self.repository, [], self.verification)

    def test_recovery_guidance_matches_lifecycle_and_failure_kind(self):
        authorization = {"state": "invalidated", "grant_id": "grant-one", "reason_codes": ["snapshot_tree"]}
        for state, action in (("active", "update"), ("disabled", "enable"), ("archived", "enable"), ("uninstalled", "install")):
            with self.subTest(state=state):
                self.change_installation(state=state, authorization=authorization)
                self.verification.update(verification_id="verification-" + state, valid=False)
                self.verification["approval"] = {"state": "invalidated", "requires_reapproval": True, "reason_codes": ["snapshot_tree", "approval_invalidated"]}
                self.store()
                status = self.status()
                self.assertIn("plan " + action + " ", status["recommended_next_action"]["command"])
                self.assertEqual(status["severity"], "error")

    def test_additive_projection_fields_are_ignored_but_core_evidence_is_not(self):
        self.store()
        before = self.status()
        projection = self.repository.get("status", "installation-one")
        self.repository.put("status", "installation-one", {**projection["data"], "future_optional_field": {"format": 2}}, expected_revision=projection["revision"])
        self.assertEqual(self.status(), before)


class TestLifecycleStatusManagerIntegration(unittest.TestCase):
    def setUp(self):
        from skills_auditor.lifecycle.engine import Manager
        temporary = tempfile.TemporaryDirectory(prefix="skills-auditor-status-manager-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        source = self.root / "candidate"
        source.mkdir()
        (source / "SKILL.md").write_text("# Example\n")
        (source / "payload").write_text("H1")
        self.target = self.root / "installed"
        self.manager = Manager(self.root)
        self.addCleanup(self.manager.repository.close)
        plan = self.manager.plan("install", source=source, target=self.target)
        self.receipt = self.manager.apply(plan, approve_plan_id=plan["plan_id"])

    def test_real_refresh_timestamp_is_sampled_after_completed_verification(self):
        # The timestamp captured before verify() cannot classify its later
        # completed observation as clock skew. This must pass without mocks.
        result = preflight(self.manager, self.receipt["installation_id"], refresh=True)
        self.assertEqual(result["decision"], "proceed")
        self.assertEqual(result["status"]["freshness"]["state"], "fresh")
        self.assertGreaterEqual(result["status"]["freshness"]["age_seconds"], 0)

    def test_failed_status_publish_preserves_durable_denial_and_never_returns_cached_success(self):
        identifier = self.receipt["installation_id"]
        self.manager.verify(identifier)
        previous_status = self.manager.repository.get("status", identifier)
        previous_latest = self.manager.repository.get("latest-verification", identifier)
        previous_installation = self.manager.repository.get("installation", identifier)
        previous_observations = self.manager.repository.list("verification")
        self.target.unlink()
        original_put = self.manager.repository.put
        def fail_status(kind, record_id, data, expected_revision=0):
            if kind == "status":
                raise LifecycleError("repository_io_error", "injected status publication failure")
            return original_put(kind, record_id, data, expected_revision=expected_revision)
        with patch.object(self.manager.repository, "put", side_effect=fail_status):
            result = preflight(self.manager, identifier, refresh=True)
        self.assertEqual(result["decision"], "block")
        self.assertIn("verification_failed", result["status"]["reason_codes"])
        self.assertFalse(self.target.is_symlink())
        self.assertEqual(self.manager.repository.get("status", identifier), previous_status)
        self.assertNotEqual(self.manager.repository.get("latest-verification", identifier), previous_latest)
        current_installation = self.manager.repository.get("installation", identifier)
        self.assertEqual(current_installation["data"]["authorization"]["state"], "invalidated")
        self.assertGreater(current_installation["revision"], previous_installation["revision"])
        self.assertEqual(len(self.manager.repository.list("verification")), len(previous_observations) + 1)
        self.assertEqual(preflight(self.manager, identifier, refresh=False)["decision"], "block")

    def test_incomplete_verification_and_changed_transaction_fence_cached_success(self):
        identifier = self.receipt["installation_id"]
        self.manager.verify(identifier)
        marker = self.manager.repository.get("verification-run", identifier)
        self.manager.repository.put("verification-run", identifier, {**marker["data"], "state": "in_progress"}, expected_revision=marker["revision"])
        self.assertEqual(preflight(self.manager, identifier)["decision"], "block")
        self.manager.verify(identifier)
        tx = self.manager.repository.get("transaction", self.receipt["transaction_id"])
        self.manager.repository.put("transaction", self.receipt["transaction_id"], {**tx["data"], "state": "applying"}, expected_revision=tx["revision"])
        self.assertEqual(preflight(self.manager, identifier)["decision"], "block")


if __name__ == "__main__":
    unittest.main()
