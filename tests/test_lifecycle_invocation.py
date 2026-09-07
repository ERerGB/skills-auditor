"""Use-time selection never substitutes candidates or silently grants trust."""

import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from skills_auditor.lifecycle.common import LifecycleError, digest
from skills_auditor.lifecycle.engine import Manager
from skills_auditor.lifecycle.invocation import (
    apply_override, get_override, plan_override, revoke_override, select,
)
from skills_auditor.lifecycle.status import read_status


class TestLifecycleInvocation(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="skills-auditor-invocation-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "candidate"
        self.source.mkdir()
        (self.source / "SKILL.md").write_text("# H1\n")
        self.target = self.root / "installed"
        self.manager = Manager(self.root)
        self.addCleanup(self.manager.repository.close)
        plan = self.manager.plan("install", source=self.source, target=self.target)
        self.receipt = self.manager.apply(plan, approve_plan_id=plan["plan_id"])
        self.identifier = self.receipt["installation_id"]
        self.now = datetime.now(timezone.utc)
        with patch("skills_auditor.lifecycle.engine.utc_now", return_value=self.now.isoformat()):
            self.manager.verify(self.identifier)
        self.stale = self.now + timedelta(seconds=20)

    def override(self):
        plan = plan_override(self.manager, self.identifier, reason="Temporary disconnected adapter", ttl_seconds=60,
                             now=self.stale, max_age_seconds=1)
        result = apply_override(self.manager, plan, approve_plan_id=plan["plan_id"], actor="operator", tool="fixture", now=self.stale)
        return plan, result

    def cached(self, **options):
        return select(self.manager, self.identifier, refresh=False, now=self.stale, max_age_seconds=1, **options)

    def test_both_policies_keep_current_h1_and_never_execute_or_choose_h2(self):
        (self.source / "SKILL.md").write_text("# H2 candidate\n")
        for policy in ("strict", "last-known-good"):
            result = select(self.manager, self.identifier, policy=policy)
            self.assertEqual(result["decision"], "proceed")
            self.assertEqual(result["version_id"], self.receipt["version_id"])
            self.assertEqual((Path(result["snapshot_path"]) / "SKILL.md").read_text(), "# H1\n")
            self.assertIsNone(result["override"])
            self.assertIsNotNone(self.manager.repository.get("invocation", result["invocation_id"]))
            self.assertNotIn("H2 candidate", json.dumps(result))
        self.assertEqual(self.manager.repository.get("receipt", self.receipt["receipt_id"])["data"], self.receipt)

    def test_cached_stale_never_proceeds_without_explicit_bounded_override(self):
        for policy in ("strict", "last-known-good"):
            result = self.cached(policy=policy)
            self.assertIn(result["decision"], {"warn", "block"})
            self.assertIsNone(result["snapshot_path"])
        old_status = self.manager.repository.get("status", self.identifier)
        plan, approved = self.override()
        result = self.cached(override_id=approved["override_id"])
        self.assertEqual(result["decision"], "proceed")
        self.assertTrue(result["override"]["used"])
        self.assertEqual(result["status"]["freshness"]["state"], "stale")
        self.assertEqual(old_status, self.manager.repository.get("status", self.identifier))
        self.assertEqual(plan["version_id"], self.receipt["version_id"])

    def test_missing_wrong_approval_and_expired_apply_have_no_grant_or_receipt(self):
        plan = plan_override(self.manager, self.identifier, reason="bounded exception", ttl_seconds=1, now=self.stale, max_age_seconds=1)
        for approval, now in ((None, self.stale), ("wrong", self.stale), (plan["plan_id"], self.stale + timedelta(seconds=1))):
            with self.subTest(approval=approval, now=now), self.assertRaises(LifecycleError):
                apply_override(self.manager, plan, approve_plan_id=approval, actor="operator", tool="fixture", now=now)
        self.assertEqual(self.manager.repository.list("invocation-override"), [])
        self.assertEqual(self.manager.repository.list("invocation-override-grant"), [])
        self.assertEqual(self.manager.repository.list("invocation-override-receipt"), [])

    def test_override_does_not_waive_expiry_revocation_new_grant_or_wrong_identity(self):
        _, approved = self.override()
        identifier = approved["override_id"]
        expired = select(self.manager, self.identifier, refresh=False, now=self.stale + timedelta(seconds=60), max_age_seconds=1, override_id=identifier)
        self.assertEqual(expired["decision"], "block")
        revoked = revoke_override(self.manager, identifier, reason="Adapter recovered", actor="operator", tool="fixture", now=self.stale)
        self.assertEqual(revoked["state"], "revoked")
        self.assertEqual(self.cached(override_id=identifier)["decision"], "block")
        self.stale += timedelta(seconds=1)
        _, other = self.override()
        renewal = self.manager.plan("renew", installation_id=self.identifier)
        self.manager.apply(renewal, approve_plan_id=renewal["plan_id"])
        self.manager.verify(self.identifier)
        self.assertEqual(self.cached(override_id=other["override_id"])["decision"], "block")
        self.assertEqual(select(self.manager, "foreign", refresh=False, override_id=other["override_id"])["decision"], "block")

    def test_real_corruption_is_sticky_and_cannot_be_overridden_after_restore(self):
        _, approved = self.override()
        payload = self.target.resolve() / "SKILL.md"
        original = payload.read_bytes()
        payload.chmod(0o644)
        payload.write_text("# Corrupt active snapshot\n")
        result = self.cached(override_id=approved["override_id"])
        self.assertEqual(result["decision"], "block")
        self.assertIsNone(result["snapshot_path"])
        payload.write_bytes(original)
        payload.chmod(0o444)
        self.assertEqual(self.cached(override_id=approved["override_id"])["decision"], "block")
        self.assertEqual(self.manager.get_installation(self.identifier)["authorization"]["state"], "invalidated")
        self.assertNotEqual(read_status(self.manager, self.identifier)["severity"], "ok")

    def test_unknown_disabled_revoked_missing_target_and_unfinished_marker_block(self):
        for mutation in ("disabled", "revoked", "missing", "marker"):
            with self.subTest(mutation=mutation):
                fixture = type(self)()
                fixture.setUp()
                try:
                    if mutation in {"disabled", "revoked"}:
                        operation = "disable" if mutation == "disabled" else "revoke"
                        plan = fixture.manager.plan(operation, installation_id=fixture.identifier)
                        fixture.manager.apply(plan, approve_plan_id=plan["plan_id"])
                    elif mutation == "missing":
                        fixture.target.unlink()
                    else:
                        old = fixture.manager.repository.get("verification-run", fixture.identifier)
                        fixture.manager.repository.put("verification-run", fixture.identifier, {**old["data"], "state": "in_progress"}, expected_revision=old["revision"])
                    result = fixture.cached()
                    self.assertEqual(result["decision"], "block")
                    self.assertIn({"disabled": "lifecycle_disabled", "revoked": "approval_revoked", "missing": "target_link", "marker": "verification_incomplete"}[mutation], result["status"]["reason_codes"])
                finally:
                    fixture.doCleanups()
        self.assertEqual(select(self.manager, "unknown", refresh=False)["decision"], "block")

    def test_bad_policy_and_rehashed_malformed_override_plan_fail_closed(self):
        for value in (True, 0, 3601, "300", 1.5):
            with self.subTest(ttl=value), self.assertRaises(LifecycleError):
                plan_override(self.manager, self.identifier, reason="test", ttl_seconds=value, now=self.stale, max_age_seconds=1)
        for options in ({"policy": "automatic-fallback"}, {"refresh": 1}, {"max_age_seconds": True}, {"now": datetime.now()}):
            with self.subTest(options=options), self.assertRaises(LifecycleError):
                select(self.manager, self.identifier, **options)
        plan = plan_override(self.manager, self.identifier, reason="test", now=self.stale, max_age_seconds=1)
        for field, value in (("reason", ""), ("ttl_seconds", True), ("generation", False), ("expires_at", "yesterday"), ("implicit_approval", True)):
            changed = copy.deepcopy(plan)
            changed[field] = value
            changed["plan_id"] = digest({key: value for key, value in changed.items() if key != "plan_id"})
            with self.subTest(field=field), self.assertRaises(LifecycleError):
                apply_override(self.manager, changed, approve_plan_id=changed["plan_id"], actor="operator", tool="fixture", now=self.stale)
        self.assertEqual(self.manager.repository.list("invocation-override"), [])

    def test_override_and_invocation_audit_write_failure_never_returns_success(self):
        plan = plan_override(self.manager, self.identifier, reason="test", now=self.stale, max_age_seconds=1)
        with patch.object(self.manager.repository, "append_event", side_effect=LifecycleError("repository_io_error", "injected")):
            with self.assertRaises(LifecycleError):
                apply_override(self.manager, plan, approve_plan_id=plan["plan_id"], actor="operator", tool="fixture", now=self.stale)
        self.assertEqual(self.manager.repository.list("invocation-override"), [])
        self.assertEqual(self.manager.repository.list("invocation-override-grant"), [])
        self.assertEqual(self.manager.repository.list("invocation-override-receipt"), [])
        with patch.object(self.manager.repository, "append_event", side_effect=LifecycleError("repository_io_error", "injected")):
            result = select(self.manager, self.identifier, refresh=False, now=self.now + timedelta(seconds=1))
        self.assertEqual(result["decision"], "block")
        self.assertIsNone(result["snapshot_path"])

    def test_restart_preserves_override_history_and_no_candidate_or_environment_payload(self):
        _, approved = self.override()
        manager = Manager(self.root, create=False)
        self.addCleanup(manager.repository.close)
        self.assertEqual(get_override(manager, approved["override_id"]), approved)
        with patch.dict(os.environ, {"INVOCATION_PRIVATE_PROBE": "do-not-collect-this"}):
            result = select(manager, self.identifier, refresh=False, now=self.stale, max_age_seconds=1, override_id=approved["override_id"])
        packet = json.dumps(result) + json.dumps(manager.repository.events(self.identifier))
        self.assertNotIn("do-not-collect-this", packet)
        self.assertNotIn("INVOCATION_PRIVATE_PROBE", packet)

    def test_failed_denial_write_leaves_shared_fence_until_explicit_new_grant(self):
        _, approved = self.override()
        raw = os.readlink(self.target)
        self.target.unlink()
        original_put = self.manager.repository.put

        def fail_denial(kind, *arguments, **options):
            if kind == "authorization":
                raise LifecycleError("repository_io_error", "denial write failed")
            return original_put(kind, *arguments, **options)

        with patch.object(self.manager.repository, "put", side_effect=fail_denial):
            result = self.cached(override_id=approved["override_id"])
        self.assertEqual(result["decision"], "block")
        self.assertEqual(self.manager.repository.get("verification-run", self.identifier)["data"]["state"], "in_progress")
        self.target.symlink_to(raw, target_is_directory=True)
        self.assertEqual(self.manager.verify(self.identifier)["approval"]["state"], "invalidated")
        self.assertEqual(self.cached(override_id=approved["override_id"])["decision"], "block")
        renewal = self.manager.plan("renew", installation_id=self.identifier)
        self.manager.apply(renewal, approve_plan_id=renewal["plan_id"])
        self.assertEqual(select(self.manager, self.identifier)["decision"], "proceed")

    def test_revoke_failure_rolls_back_projection_receipt_and_retry_cannot_regrant(self):
        plan, approved = self.override()
        receipts = self.manager.repository.list("invocation-override-receipt")
        with patch.object(self.manager.repository, "append_event", side_effect=LifecycleError("repository_io_error", "injected")):
            with self.assertRaises(LifecycleError):
                revoke_override(self.manager, approved["override_id"], reason="done", actor="operator", tool="fixture", now=self.stale)
        self.assertEqual(get_override(self.manager, approved["override_id"]), approved)
        self.assertEqual(self.manager.repository.list("invocation-override-receipt"), receipts)
        revoked = revoke_override(self.manager, approved["override_id"], reason="done", actor="operator", tool="fixture", now=self.stale)
        self.assertEqual(revoke_override(self.manager, approved["override_id"], reason="done", now=self.stale), revoked)
        with self.assertRaises(LifecycleError):
            apply_override(self.manager, plan, approve_plan_id=plan["plan_id"], now=self.stale)

    def test_installation_changes_during_live_check_and_corrupt_override_evidence_block(self):
        _, approved = self.override()
        original_check = self.manager.check_cached_integrity

        def change_binding(identifier):
            result = original_check(identifier)
            plan = self.manager.plan("renew", installation_id=identifier)
            self.manager.apply(plan, approve_plan_id=plan["plan_id"])
            return result

        with patch.object(self.manager, "check_cached_integrity", side_effect=change_binding):
            self.assertEqual(self.cached(override_id=approved["override_id"])["decision"], "block")
        receipt_id = approved["approval_receipt_id"]
        receipt = self.manager.repository.get("invocation-override-receipt", receipt_id)
        self.manager.repository.put("invocation-override-receipt", receipt_id, {**receipt["data"], "plan_id": "bad"}, expected_revision=receipt["revision"])
        with self.assertRaises(LifecycleError):
            get_override(self.manager, approved["override_id"])

    def test_real_outputs_match_shipped_schema_and_unsafe_proceed_is_rejected(self):
        try:
            from jsonschema import Draft202012Validator, FormatChecker
            from referencing import Registry, Resource
        except ImportError:
            self.skipTest("install the test extra to validate schemas")
        schemas = Path(__file__).parents[1] / "skills_auditor/schemas"
        schema = json.loads((schemas / "lifecycle-invocation-v1.schema.json").read_text())
        status_schema = json.loads((schemas / "lifecycle-status-v1.schema.json").read_text())
        registry = Registry().with_resource(status_schema["$id"], Resource.from_contents(status_schema))
        validator = Draft202012Validator(schema, registry=registry, format_checker=FormatChecker())
        Draft202012Validator.check_schema(schema)
        plan, approved = self.override()
        result = self.cached(override_id=approved["override_id"])
        documents = [plan, approved, result, self.cached()]
        for kind in ("invocation-override-grant", "invocation-override-receipt"):
            documents.extend(record["data"] for record in self.manager.repository.list(kind))
        documents.append(revoke_override(self.manager, approved["override_id"], reason="done", now=self.stale))
        for document in documents:
            errors = list(validator.iter_errors(document))
            self.assertEqual(errors, [], str(errors[0])[:300] if errors else "")
        for mutation in ({"snapshot_path": None}, {"audit": {"recorded": False, "event_sequence": None}}, {"override": None}):
            self.assertFalse(validator.is_valid({**result, **mutation}))

    def test_real_clock_expiry_is_rechecked_after_slow_live_observation(self):
        plan = plan_override(self.manager, self.identifier, reason="short window", ttl_seconds=1, now=self.stale, max_age_seconds=1)
        expired = self.stale + timedelta(seconds=2)
        with patch("skills_auditor.lifecycle.invocation._clock", side_effect=[self.stale, expired]):
            with self.assertRaises(LifecycleError):
                apply_override(self.manager, plan, approve_plan_id=plan["plan_id"])
        self.assertEqual(self.manager.repository.list("invocation-override"), [])
        _, approved = self.override()
        with patch("skills_auditor.lifecycle.invocation._clock", side_effect=[self.stale, self.stale + timedelta(seconds=61)]):
            result = select(self.manager, self.identifier, refresh=False, max_age_seconds=1, override_id=approved["override_id"])
        self.assertEqual(result["decision"], "block")

    def test_malformed_approval_metadata_is_not_accepted_as_an_override(self):
        _, approved = self.override()
        for kind, identifier, field, value in (
            ("invocation-override-grant", approved["override_id"], "actor", []),
            ("invocation-override-receipt", approved["approval_receipt_id"], "created_at", "yesterday"),
        ):
            record = self.manager.repository.get(kind, identifier)
            self.manager.repository.put(kind, identifier, {**record["data"], field: value}, expected_revision=record["revision"])
            with self.subTest(kind=kind), self.assertRaises(LifecycleError):
                get_override(self.manager, approved["override_id"])
            self.manager.repository.put(kind, identifier, record["data"], expected_revision=record["revision"] + 1)

    def test_stale_active_projection_cannot_resurrect_an_immutable_revocation(self):
        _, active = self.override()
        grant = self.manager.repository.get("invocation-override-grant", active["override_id"])
        revoke_override(self.manager, active["override_id"], reason="no longer allowed", actor="operator", tool="fixture", now=self.stale)
        record = self.manager.repository.get("invocation-override", active["override_id"])
        self.manager.repository.put("invocation-override", active["override_id"], active, expected_revision=record["revision"])
        with self.assertRaises(LifecycleError):
            get_override(self.manager, active["override_id"])
        self.assertEqual(self.cached(override_id=active["override_id"])["decision"], "block")
        self.assertEqual(grant, self.manager.repository.get("invocation-override-grant", active["override_id"]))
        self.assertEqual(self.manager.get_installation(self.identifier)["authorization"]["state"], "valid")

    def test_override_requires_actual_append_only_approval_and_revocation_events(self):
        _, active = self.override()
        with patch.object(self.manager.repository, "events", return_value=[]), self.assertRaises(LifecycleError):
            get_override(self.manager, active["override_id"])
        revoke_override(self.manager, active["override_id"], reason="done", now=self.stale)
        original_events = self.manager.repository.events

        def omit_revoke(*arguments, **options):
            return [event for event in original_events(*arguments, **options) if event["event_type"] != "invocation_override_revoked"]

        with patch.object(self.manager.repository, "events", side_effect=omit_revoke), self.assertRaises(LifecycleError):
            get_override(self.manager, active["override_id"])
