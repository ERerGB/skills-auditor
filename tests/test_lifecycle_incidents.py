"""Durable investigation evidence uses only isolated managed installations."""

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import jsonschema

from skills_auditor.lifecycle.common import LifecycleError, utc_now
from skills_auditor.lifecycle.engine import Manager
from skills_auditor.lifecycle.incidents import (
    append_note, get_incident, investigate, list_incidents, record_verification, resolve, supersede,
)


class TestLifecycleIncidents(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="skills-auditor-incidents-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "candidate"
        self.source.mkdir()
        (self.source / "SKILL.md").write_text("# Example\n")
        (self.source / "payload").write_text("H1")
        self.target = self.root / "installed"
        self.manager = Manager(self.root)
        self.addCleanup(self.manager.repository.close)
        plan = self.manager.plan("install", source=self.source, target=self.target)
        self.receipt = self.manager.apply(plan, approve_plan_id=plan["plan_id"])
        self.installation_id = self.receipt["installation_id"]
        self.original_link = os.readlink(self.target)

    def capture(self):
        verification = self.manager.verify(self.installation_id)
        installation = self.manager.get_installation(self.installation_id)
        references = record_verification(self.manager.repository, installation, verification)
        return verification, references

    def fail(self, target=None):
        self.target.unlink(missing_ok=True)
        if target is not None:
            self.target.symlink_to(target)
        return self.capture()

    def renew(self):
        self.target.unlink(missing_ok=True)
        self.target.symlink_to(self.original_link)
        plan = self.manager.plan("renew", installation_id=self.installation_id)
        self.manager.apply(plan, approve_plan_id=plan["plan_id"])
        verification, _ = self.capture()
        return verification

    def test_repeated_signature_deduplicates_and_same_observation_replay_is_idempotent(self):
        first, references = self.fail()
        incident_id = references[0]["incident_id"]
        second, again = self.capture()
        self.assertEqual(again[0]["incident_id"], incident_id)
        installation = self.manager.get_installation(self.installation_id)
        record_verification(self.manager.repository, installation, second)
        incident = get_incident(self.manager, incident_id)
        self.assertEqual(incident["observation_count"], 2)
        self.assertEqual(incident["opening_verification_id"], first["verification_id"])
        self.assertEqual(incident["latest_verification_id"], second["verification_id"])
        self.assertEqual(len(list_incidents(self.manager)), 1)
        packet = investigate(self.manager, incident_id)
        self.assertEqual(len(packet["events"]), 2)
        self.assertEqual(incident["state"], "open")
        with Manager(self.root).repository as repository:
            self.assertEqual(repository.get("incident", incident_id)["data"], incident)

    def test_distinct_actual_target_and_failure_check_create_distinct_incidents(self):
        _, first = self.fail("foreign-a")
        _, second = self.fail("foreign-b")
        self.assertNotEqual(first[0]["incident_id"], second[0]["incident_id"])
        self.assertEqual(len(list_incidents(self.manager, installation_id=self.installation_id)), 2)

    def test_restored_bytes_link_original_incident_without_new_fault_or_resolution(self):
        _, first = self.fail()
        self.target.symlink_to(self.original_link)
        verification, second = self.capture()
        self.assertTrue(verification["integrity"]["valid"])
        self.assertEqual(verification["approval"]["state"], "invalidated")
        self.assertEqual(second[0]["incident_id"], first[0]["incident_id"])
        self.assertEqual(len(list_incidents(self.manager)), 1)
        self.assertEqual(get_incident(self.manager, first[0]["incident_id"])["state"], "open")
        with self.assertRaises(LifecycleError):
            resolve(self.manager, first[0]["incident_id"], verification_id=verification["verification_id"], actor="reviewer", tool="test")

    def test_clean_verification_and_governance_only_revocation_do_not_create_incident(self):
        _, refs = self.capture()
        self.assertEqual(refs, [])
        plan = self.manager.plan("revoke", installation_id=self.installation_id)
        self.manager.apply(plan, approve_plan_id=plan["plan_id"])
        verification, refs = self.capture()
        self.assertEqual(verification["approval"]["state"], "revoked")
        self.assertEqual(refs, [])
        self.assertEqual(list_incidents(self.manager), [])

    def test_notes_have_local_attribution_references_and_idempotent_event_ids(self):
        verification, refs = self.fail()
        identifier = refs[0]["incident_id"]
        note = append_note(self.manager, identifier, "Reproduced using a missing target.", actor="investigator", tool="local-cli", evidence_refs=[{"kind": "verification", "id": verification["verification_id"]}], event_id="consumer-retry-key")
        duplicate = append_note(self.manager, identifier, "Reproduced using a missing target.", actor="investigator", tool="local-cli", evidence_refs=[{"kind": "verification", "id": verification["verification_id"]}], event_id="consumer-retry-key")
        self.assertEqual(note, duplicate)
        packet = investigate(self.manager, identifier)
        self.assertEqual(packet["incident"]["state"], "investigating")
        self.assertEqual(len(packet["events"]), 2)
        self.assertEqual(packet["events"][-1]["actor"], "investigator")
        self.assertEqual(packet["events"][-1]["tool"], "local-cli")
        with self.assertRaises(LifecycleError):
            append_note(self.manager, identifier, "different", actor="investigator", tool="local-cli", event_id="consumer-retry-key")
        self.assertEqual(len(investigate(self.manager, identifier)["events"]), 2)

    def test_unapproved_or_arbitrary_file_references_and_oversize_notes_rejected(self):
        _, refs = self.fail()
        identifier = refs[0]["incident_id"]
        cases = ({"text": "x" * 4097}, {"actor": ""}, {"tool": "bad\nlabel"}, {"evidence_refs": [{"kind": "file", "id": "/etc/passwd"}]}, {"evidence_refs": [{"kind": "receipt", "id": "missing"}]}, {"evidence_refs": ["../../private"]})
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(LifecycleError):
                append_note(self.manager, identifier, **{**{"text": "bounded note", "actor": "reviewer", "tool": "test"}, **changes})
        self.assertEqual(get_incident(self.manager, identifier)["state"], "open")
        self.assertEqual(len(investigate(self.manager, identifier)["events"]), 1)

    def test_new_explicit_grant_completed_transaction_and_latest_clean_verification_resolve(self):
        _, refs = self.fail()
        identifier = refs[0]["incident_id"]
        old = copy.deepcopy(get_incident(self.manager, identifier))
        verification = self.renew()
        before_installation = self.manager.repository.get("installation", self.installation_id)
        resolved = resolve(self.manager, identifier, verification_id=verification["verification_id"], actor="reviewer", tool="test")
        self.assertEqual(resolved["state"], "resolved")
        self.assertEqual(resolved["resolution"]["kind"], "remediated")
        self.assertNotEqual(resolved["resolution"]["grant_id"], old["grant_id"])
        self.assertEqual(resolved["opening_verification_id"], old["opening_verification_id"])
        self.assertEqual(self.manager.repository.get("installation", self.installation_id), before_installation)
        self.assertEqual(resolve(self.manager, identifier, verification_id=verification["verification_id"], actor="reviewer", tool="test"), resolved)

    def test_stale_wrong_or_uncommitted_proof_does_not_resolve(self):
        _, refs = self.fail()
        identifier = refs[0]["incident_id"]
        stale = self.renew()
        latest, _ = self.capture()
        for proof in ("missing", stale["verification_id"], self.receipt["receipt_id"]):
            with self.subTest(proof=proof), self.assertRaises(LifecycleError):
                resolve(self.manager, identifier, verification_id=proof, actor="reviewer", tool="test")
        original_get = self.manager.repository.get
        for kind in ("grant", "receipt", "transaction", "latest-verification"):
            with self.subTest(kind=kind), patch.object(self.manager.repository, "get", side_effect=lambda requested, record_id: None if requested == kind else original_get(requested, record_id)):
                with self.assertRaises(LifecycleError):
                    resolve(self.manager, identifier, verification_id=latest["verification_id"], actor="reviewer", tool="test")
        self.assertEqual(get_incident(self.manager, identifier)["state"], "open")

    def test_explicit_nonremediation_disposition_requires_explanation_and_never_authorizes(self):
        _, refs = self.fail()
        identifier = refs[0]["incident_id"]
        before = self.manager.repository.get("installation", self.installation_id)
        with self.assertRaises(LifecycleError):
            resolve(self.manager, identifier, disposition="obsolete", actor="reviewer", tool="test")
        resolved = resolve(self.manager, identifier, disposition="obsolete", explanation="This test installation is retired; no remediation is asserted.", actor="reviewer", tool="test")
        self.assertEqual(resolved["resolution"]["kind"], "non_remediation")
        self.assertEqual(self.manager.repository.get("installation", self.installation_id), before)
        self.assertEqual(self.manager.verify(self.installation_id)["approval"]["state"], "invalidated")

    def test_failed_note_and_resolution_writes_leave_append_only_history_and_state_unchanged(self):
        _, refs = self.fail()
        identifier = refs[0]["incident_id"]
        before = get_incident(self.manager, identifier)
        events = investigate(self.manager, identifier)["events"]
        original_put = self.manager.repository.put
        def fail_incident(kind, record_id, data, expected_revision=0):
            if kind == "incident":
                raise LifecycleError("repository_io_error", "injected incident publication failure")
            return original_put(kind, record_id, data, expected_revision=expected_revision)
        with patch.object(self.manager.repository, "put", side_effect=fail_incident):
            with self.assertRaises(LifecycleError):
                append_note(self.manager, identifier, "not committed", actor="reviewer", tool="test")
            with self.assertRaises(LifecycleError):
                resolve(self.manager, identifier, disposition="obsolete", explanation="explicit decision", actor="reviewer", tool="test")
        self.assertEqual(get_incident(self.manager, identifier), before)
        self.assertEqual(investigate(self.manager, identifier)["events"], events)

    def test_incident_failure_cannot_erase_previously_durable_core_invalidation(self):
        self.target.unlink()
        # Isolate consumer retry even after Manager gains its automatic hook.
        with patch("skills_auditor.lifecycle.incidents.record_verification", return_value=[]):
            verification = self.manager.verify(self.installation_id)
        installation = self.manager.get_installation(self.installation_id)
        original_put = self.manager.repository.put
        def fail_incident(kind, record_id, data, expected_revision=0):
            if kind == "incident":
                raise LifecycleError("repository_io_error", "injected incident failure")
            return original_put(kind, record_id, data, expected_revision=expected_revision)
        with patch.object(self.manager.repository, "put", side_effect=fail_incident):
            with self.assertRaises(LifecycleError):
                record_verification(self.manager.repository, installation, verification)
        self.assertEqual(self.manager.get_installation(self.installation_id)["authorization"]["state"], "invalidated")
        self.assertIsNotNone(self.manager.repository.get("verification", verification["verification_id"]))

    def test_packet_is_bounded_schema_valid_and_does_not_collect_private_prose(self):
        verification, refs = self.fail()
        identifier = refs[0]["incident_id"]
        for index in range(55):
            append_note(self.manager, identifier, "Explicit research note " + str(index), actor="reviewer", tool="test")
        packet = investigate(self.manager, identifier)
        self.assertEqual(len(packet["events"]), 50)
        self.assertTrue(packet["truncated"])
        self.assertTrue(packet["has_more"])
        self.assertEqual(packet["returned_events"], 50)
        second_page = investigate(self.manager, identifier, after_sequence=packet["continuation"]["after_sequence"])
        self.assertEqual(second_page["returned_events"], 6)
        self.assertFalse(second_page["has_more"])
        self.assertIsNone(second_page["continuation"])
        self.assertLess(packet["events"][-1]["sequence"], second_page["events"][0]["sequence"])
        with patch.object(self.manager.repository, "events", wraps=self.manager.repository.events) as events:
            investigate(self.manager, identifier, limit=2, after_sequence=1)
        events.assert_called_once_with("incident:" + identifier, limit=3, after_sequence=1)
        self.assertIn("skills-audit lifecycle", packet["next_action"])
        for limit in (True, 0, -1, 51, "50"):
            with self.subTest(limit=limit), self.assertRaises(LifecycleError):
                investigate(self.manager, identifier, limit=limit)
        for cursor in (True, -1, "1"):
            with self.subTest(cursor=cursor), self.assertRaises(LifecycleError):
                investigate(self.manager, identifier, after_sequence=cursor)
        schemas = Path(__file__).resolve().parents[1] / "skills_auditor/schemas"
        for filename, value in (("lifecycle-incident-v1.schema.json", packet["incident"]), ("lifecycle-investigation-v1.schema.json", packet)):
            schema = json.loads((schemas / filename).read_text())
            jsonschema.Draft202012Validator.check_schema(schema)
            jsonschema.validate(value, schema, format_checker=jsonschema.FormatChecker())
        self.assertNotIn("error", json.dumps(packet["incident"]["evidence"]))

    def test_superseding_links_related_incidents_without_erasing_history(self):
        _, first = self.fail("foreign-a")
        _, second = self.fail("foreign-b")
        old, new = first[0]["incident_id"], second[0]["incident_id"]
        superseded = supersede(self.manager, old, new, explanation="The second target change is the current investigation.", actor="reviewer", tool="test")
        self.assertEqual(superseded["state"], "superseded")
        self.assertEqual(superseded["superseded_by"], new)
        self.assertEqual(len(investigate(self.manager, old)["events"]), 2)
        with self.assertRaises(LifecycleError):
            supersede(self.manager, new, old, explanation="Cannot form a cycle", actor="reviewer", tool="test")

    def test_signature_ignores_error_prose_random_fields_and_check_order(self):
        first, refs = self.fail("foreign-target")
        copied = copy.deepcopy(first)
        copied["verification_id"] = "another-verification"
        copied["observed_at"] = first["observed_at"]
        copied["integrity"]["checks"].reverse()
        for check in copied["integrity"]["checks"]:
            check["random_id"] = "new-random-id"
            check["checked_at"] = "2099-01-01T00:00:00Z"
            if "error" in check:
                check["error"]["message"] = "PRIVATE_PROMPT_AND_CREDENTIAL_SHOULD_NOT_BE_COPIED"
                check["error"]["details"]["prompt"] = "PRIVATE_UNRELATED_PROMPT"
                check["error"]["details"]["actual"]["credential"] = "PRIVATE_NESTED_CREDENTIAL"
        copied["environment"] = {"PASSWORD": "PRIVATE_ENVIRONMENT"}
        self.manager.repository.put("verification", copied["verification_id"], copied)
        again = record_verification(self.manager.repository, self.manager.get_installation(self.installation_id), copied)
        self.assertEqual(refs[0]["incident_id"], again[0]["incident_id"])
        packet = investigate(self.manager, refs[0]["incident_id"])
        self.assertNotIn("PRIVATE", json.dumps(packet))
        self.assertNotIn("random_id", json.dumps(packet))
        self.assertEqual(packet["incident"]["observation_count"], 2)

    def test_uncommitted_or_malformed_observation_and_corrupt_projection_fail_closed(self):
        verification, refs = self.fail()
        copied = {**verification, "verification_id": "not-committed"}
        with self.assertRaises(LifecycleError):
            record_verification(self.manager.repository, self.manager.get_installation(self.installation_id), copied)
        for changes in ({"integrity": {"checks": []}}, {"approval": {}}, {"grant_id": "wrong-grant"}, {"observed_at": "naive-time"}):
            with self.subTest(changes=changes), self.assertRaises(LifecycleError):
                record_verification(self.manager.repository, self.manager.get_installation(self.installation_id), {**verification, **changes})
        identifier = refs[0]["incident_id"]
        original = get_incident(self.manager, identifier)
        for changes in ({"state": "resolved", "resolution": None}, {"signature": "forged"}, {"evidence": []}, {"observation_count": True}):
            previous = self.manager.repository.get("incident", identifier)
            self.manager.repository.put("incident", identifier, {**original, **changes}, expected_revision=previous["revision"])
            with self.subTest(changes=changes), self.assertRaises(LifecycleError) as caught:
                investigate(self.manager, identifier)
            self.assertEqual(caught.exception.code, "incident_corrupt")

    def test_corrupt_event_payload_cannot_escape_packet_bounds(self):
        _, refs = self.fail()
        identifier = refs[0]["incident_id"]
        self.manager.repository.append_event("incident:" + identifier, "note", {"event_id": "malformed-event", "incident_id": identifier, "text": "x" * 4097, "evidence_refs": []}, "reviewer", "test")
        with self.assertRaises(LifecycleError) as caught:
            investigate(self.manager, identifier)
        self.assertEqual(caught.exception.code, "incident_corrupt")

    def test_append_event_failure_rolls_back_all_incident_side_effects(self):
        verification = self.manager.verify(self.installation_id)
        installation = self.manager.get_installation(self.installation_id)
        failed = copy.deepcopy(verification)
        failed.update(verification_id="failed-observation", valid=False)
        failed["approval"] = {"state": "invalidated", "requires_reapproval": True, "reason_codes": ["target_link"]}
        failed["integrity"] = {"valid": False, "checks": [{"code": "target_link", "valid": False}]}
        self.manager.repository.put("verification", failed["verification_id"], failed)
        with patch.object(self.manager.repository, "append_event", side_effect=LifecycleError("repository_io_error", "event write failed")):
            with self.assertRaises(LifecycleError):
                record_verification(self.manager.repository, installation, failed)
        self.assertEqual(list_incidents(self.manager), [])
        self.assertIsNone(self.manager.repository.get("verification-incidents", failed["verification_id"]))

    def test_note_after_resolution_preserves_disposition_and_mutually_exclusive_proofs(self):
        _, refs = self.fail()
        identifier = refs[0]["incident_id"]
        resolved = resolve(self.manager, identifier, disposition="obsolete", explanation="Explicit non-remediation decision.", actor="reviewer", tool="test")
        append_note(self.manager, identifier, "Historical follow-up only.", actor="reviewer", tool="test")
        self.assertEqual(get_incident(self.manager, identifier)["state"], "resolved")
        self.assertEqual(get_incident(self.manager, identifier)["resolution"], resolved["resolution"])
        for kwargs in ({}, {"disposition": "obsolete", "verification_id": "anything", "explanation": "both"}, {"disposition": "different", "explanation": "must not replace history"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(LifecycleError):
                resolve(self.manager, identifier, actor="reviewer", tool="test", **kwargs)
        self.assertEqual(resolve(self.manager, identifier, disposition="obsolete", explanation="Explicit non-remediation decision.", actor="reviewer", tool="test")["resolution"], resolved["resolution"])

    def test_invalid_lookup_filter_attribution_and_unicode_notes_are_rejected(self):
        _, refs = self.fail()
        identifier = refs[0]["incident_id"]
        for value in ("", "../foreign"):
            with self.subTest(value=value), self.assertRaises(LifecycleError):
                get_incident(self.manager, value)
        with self.assertRaises(LifecycleError) as caught:
            get_incident(self.manager, "missing")
        self.assertEqual(caught.exception.code, "incident_missing")
        for kwargs in ({"installation_id": "../../foreign"}, {"state": []}, {"state": "invalid"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(LifecycleError):
                list_incidents(self.manager, **kwargs)
        self.assertEqual(list_incidents(self.manager, state="resolved"), [])
        for text in ("好" * 1400, "\ud800", "", "\x00"):
            with self.subTest(text_length=len(text)), self.assertRaises(LifecycleError):
                append_note(self.manager, identifier, text, actor="reviewer", tool="test")
        with self.assertRaises(LifecycleError):
            append_note(self.manager, identifier, "note", actor="reviewer", tool="test", evidence_refs=[{}] * 21)

    def test_invalid_verification_guards_reject_before_event_or_index_writes(self):
        verification, _ = self.fail()
        installation = self.manager.get_installation(self.installation_id)
        mutations = (
            {"verification_id": ""}, {"skill_id": "other-skill"}, {"observed_at": None},
            {"observed_at": "2026-09-07T12:00:00"}, {"valid": True},
            {"integrity": {"valid": False, "checks": [{"code": "target_link", "valid": "false"}]}},
            {"integrity": {"valid": True, "checks": [{"code": "target_link", "valid": False}]}},
            {"integrity": {"valid": False, "checks": [{"code": "failure-" + str(index), "valid": False, "expected": "x" * 1000} for index in range(20)]}},
        )
        events_before = self.manager.repository.list("incident-event")
        for changes in mutations:
            with self.subTest(changes=changes), self.assertRaises(LifecycleError):
                record_verification(self.manager.repository, installation, {**verification, **changes})
        self.assertEqual(self.manager.repository.list("incident-event"), events_before)

    def test_unsupported_actual_fact_types_are_not_serialized_as_private_objects(self):
        verification, refs = self.fail()
        copy_value = copy.deepcopy(verification)
        copy_value["verification_id"] = "bounded-evidence"
        for check in copy_value["integrity"]["checks"]:
            if not check["valid"]:
                check["actual"] = {"exists": False, "generation": 3, "kind": ["unsupported-list"], "prompt": "PRIVATE"}
        self.manager.repository.put("verification", copy_value["verification_id"], copy_value)
        references = record_verification(self.manager.repository, self.manager.get_installation(self.installation_id), copy_value)
        incident = get_incident(self.manager, references[0]["incident_id"])
        self.assertEqual(incident["evidence"][0]["actual"], {"exists": False, "generation": 3, "kind": None})
        self.assertNotEqual(references[0]["incident_id"], refs[0]["incident_id"])

    def test_malformed_events_of_each_type_fail_closed(self):
        _, refs = self.fail()
        identifier = refs[0]["incident_id"]
        original_events = self.manager.repository.events
        baseline = original_events("incident:" + identifier)[0]
        malformed = (
            ("note", {"event_id": "bad", "incident_id": "wrong", "text": "note", "evidence_refs": []}),
            ("note", {"event_id": "bad", "incident_id": identifier, "text": "note", "evidence_refs": [{"kind": "file", "id": "arbitrary"}]}),
            ("observation", {"event_id": "bad", "incident_id": identifier, "observed_at": "2026-09-07T12:00:00Z", "verification_id": "valid-id", "checks_failed": "false", "reason_codes": []}),
            ("resolved", {"event_id": "bad", "incident_id": identifier, "resolution": {"kind": "remediated", "resolved_at": "2026-09-07T12:00:00Z"}}),
            ("superseded", {"event_id": "bad", "incident_id": identifier, "superseded_by": "new", "explanation": ""}),
            ("unknown", {"event_id": "bad", "incident_id": identifier}),
        )
        for event_type, payload in malformed:
            with self.subTest(event_type=event_type), patch.object(self.manager.repository, "events", return_value=[{**baseline, "event_type": event_type, "payload": payload}]):
                with self.assertRaises(LifecycleError) as caught:
                    investigate(self.manager, identifier)
            self.assertEqual(caught.exception.code, "incident_corrupt")

    def test_resolution_rechecks_core_proof_even_if_cached_status_claims_healthy(self):
        from skills_auditor.lifecycle.status import read_status
        _, refs = self.fail()
        identifier = refs[0]["incident_id"]
        verification = self.renew()
        healthy = read_status(self.manager, self.installation_id)
        original_get = self.manager.repository.get
        with patch("skills_auditor.lifecycle.status.read_status", return_value=healthy):
            with patch.object(self.manager, "_evidence_checks", return_value=[{"code": "transaction_record", "valid": False}]):
                with self.assertRaises(LifecycleError):
                    resolve(self.manager, identifier, verification_id=verification["verification_id"], actor="reviewer", tool="test")
            for kind, changes in (("verification", {"valid": False}), ("transaction", {"state": "recovery_needed"})):
                def altered(requested, record_id):
                    record = original_get(requested, record_id)
                    return {**record, "data": {**record["data"], **changes}} if requested == kind else record
                with self.subTest(kind=kind), patch.object(self.manager.repository, "get", side_effect=altered), patch.object(self.manager, "_evidence_checks", return_value=[{"code": "transaction_record", "valid": True}]):
                    with self.assertRaises(LifecycleError):
                        resolve(self.manager, identifier, verification_id=verification["verification_id"], actor="reviewer", tool="test")
        with self.assertRaises(LifecycleError):
            resolve(self.manager, identifier, verification_id="", actor="reviewer", tool="test")
        self.assertEqual(get_incident(self.manager, identifier)["state"], "open")

    def test_superseded_incident_cannot_resolve_or_supersede_itself(self):
        _, first = self.fail("foreign-a")
        _, second = self.fail("foreign-b")
        old, new = first[0]["incident_id"], second[0]["incident_id"]
        with self.assertRaises(LifecycleError):
            supersede(self.manager, old, old, explanation="same", actor="reviewer", tool="test")
        supersede(self.manager, old, new, explanation="follow current failure", actor="reviewer", tool="test")
        with self.assertRaises(LifecycleError):
            resolve(self.manager, old, disposition="obsolete", explanation="cannot replace supersession", actor="reviewer", tool="test")

    def test_corrupt_retry_index_cannot_hide_a_fault_or_link_another_grant(self):
        first, refs = self.fail()
        original_installation = self.manager.get_installation(self.installation_id)
        self.renew()
        _, other = self.fail()
        self.assertNotEqual(refs[0]["incident_id"], other[0]["incident_id"])
        for identifiers in ([], "not-a-list", [other[0]["incident_id"]]):
            record = self.manager.repository.get("verification-incidents", first["verification_id"])
            self.manager.repository.put("verification-incidents", first["verification_id"], {"incident_ids": identifiers}, expected_revision=record["revision"])
            with self.subTest(identifiers=identifiers), self.assertRaises(LifecycleError) as caught:
                record_verification(self.manager.repository, original_installation, first)
            self.assertEqual(caught.exception.code, "incident_corrupt")

    def test_same_grant_retry_index_must_match_failure_signature_and_durable_observation(self):
        _, first = self.fail("foreign-a")
        second, references = self.fail("foreign-b")
        record = self.manager.repository.get("verification-incidents", second["verification_id"])
        self.manager.repository.put("verification-incidents", second["verification_id"], {"incident_ids": [first[0]["incident_id"]]}, expected_revision=record["revision"])
        with self.assertRaises(LifecycleError) as caught:
            record_verification(self.manager.repository, self.manager.get_installation(self.installation_id), second)
        self.assertEqual(caught.exception.code, "incident_corrupt")
        latest = self.manager.repository.get("verification-incidents", second["verification_id"])
        self.manager.repository.put("verification-incidents", second["verification_id"], {"incident_ids": [references[0]["incident_id"]]}, expected_revision=latest["revision"])
        original_events = self.manager.repository.events
        with patch.object(self.manager.repository, "events", return_value=[]):
            with self.assertRaises(LifecycleError):
                record_verification(self.manager.repository, self.manager.get_installation(self.installation_id), second)
        self.assertEqual(len(original_events("incident:" + references[0]["incident_id"])), 1)

    def test_resolution_extra_fields_cannot_escape_projection_or_event_packet_bounds(self):
        _, refs = self.fail()
        identifier = refs[0]["incident_id"]
        resolve(self.manager, identifier, disposition="obsolete", explanation="fixture", actor="reviewer", tool="test")
        record = self.manager.repository.get("incident", identifier)
        changed = copy.deepcopy(record["data"])
        changed["resolution"]["private_extra"] = "SECRET" * 200000
        schema_root = Path(__file__).parents[1] / "skills_auditor/schemas"
        incident_schema = json.loads((schema_root / "lifecycle-incident-v1.schema.json").read_text())
        self.assertFalse(jsonschema.Draft202012Validator(incident_schema).is_valid(changed))
        self.manager.repository.put("incident", identifier, changed, expected_revision=record["revision"])
        with self.assertRaises(LifecycleError) as caught:
            investigate(self.manager, identifier)
        self.assertEqual(caught.exception.code, "incident_corrupt")
        self.manager.repository.put("incident", identifier, record["data"], expected_revision=record["revision"] + 1)
        packet = investigate(self.manager, identifier)
        packet["events"][-1]["payload"]["resolution"] = changed["resolution"]
        packet_schema = json.loads((schema_root / "lifecycle-investigation-v1.schema.json").read_text())
        self.assertFalse(jsonschema.Draft202012Validator(packet_schema).is_valid(packet))
        self.manager.repository.append_event("incident:" + identifier, "resolved", {"event_id": "oversized-resolution", "incident_id": identifier, "resolution": changed["resolution"]}, "reviewer", "test")
        with self.assertRaises(LifecycleError):
            investigate(self.manager, identifier)

    def test_false_resolved_projection_without_committed_resolution_event_is_corrupt(self):
        _, refs = self.fail()
        identifier = refs[0]["incident_id"]
        record = self.manager.repository.get("incident", identifier)
        resolution = {"kind": "remediated", "resolved_at": utc_now(), "verification_id": "nonexistent-verification",
                      "grant_id": "nonexistent-grant", "receipt_id": "nonexistent-receipt", "transaction_id": "nonexistent-transaction"}
        self.manager.repository.put("incident", identifier, {**record["data"], "state": "resolved", "resolution": resolution}, expected_revision=record["revision"])
        with self.assertRaises(LifecycleError) as caught:
            investigate(self.manager, identifier)
        self.assertEqual(caught.exception.code, "incident_corrupt")

    def test_historical_resolution_remains_readable_after_later_update_and_revocation(self):
        _, refs = self.fail()
        identifier = refs[0]["incident_id"]
        verification = self.renew()
        resolved = resolve(self.manager, identifier, verification_id=verification["verification_id"], actor="reviewer", tool="test")
        (self.source / "payload").write_text("H2")
        update = self.manager.plan("update", installation_id=self.installation_id, source=self.source)
        self.manager.apply(update, approve_plan_id=update["plan_id"])
        revoke = self.manager.plan("revoke", installation_id=self.installation_id)
        self.manager.apply(revoke, approve_plan_id=revoke["plan_id"])
        packet = investigate(self.manager, identifier)
        self.assertEqual(packet["incident"]["resolution"], resolved["resolution"])
        self.assertEqual(packet["incident"]["state"], "resolved")
        original_get = self.manager.repository.get
        for kind, proof_id in (("verification", verification["verification_id"]), ("receipt", resolved["resolution"]["receipt_id"]), ("grant", resolved["resolution"]["grant_id"])):
            def missing(requested_kind, requested_id):
                return None if (requested_kind, requested_id) == (kind, proof_id) else original_get(requested_kind, requested_id)
            with self.subTest(kind=kind), patch.object(self.manager.repository, "get", side_effect=missing), self.assertRaises(LifecycleError):
                investigate(self.manager, identifier)

    def test_historical_resolution_requires_complete_unique_core_checks(self):
        _, refs = self.fail()
        identifier = refs[0]["incident_id"]
        verification = self.renew()
        resolve(self.manager, identifier, verification_id=verification["verification_id"], actor="reviewer", tool="test")
        original = self.manager.repository.get("verification", verification["verification_id"])["data"]
        for checks in ([{"code": "arbitrary", "valid": True}], original["integrity"]["checks"][:-1], original["integrity"]["checks"] + [original["integrity"]["checks"][0]]):
            row = self.manager.repository.get("verification", verification["verification_id"])
            changed = {**original, "integrity": {"valid": True, "checks": checks}}
            self.manager.repository.put("verification", verification["verification_id"], changed, expected_revision=row["revision"])
            with self.subTest(checks=checks), self.assertRaises(LifecycleError) as caught:
                investigate(self.manager, identifier)
            self.assertEqual(caught.exception.code, "incident_corrupt")

    def test_investigation_schema_enforces_complete_incident_and_event_payload_contracts(self):
        _, refs = self.fail()
        identifier = refs[0]["incident_id"]
        append_note(self.manager, identifier, "bounded note", actor="reviewer", tool="test")
        packet = investigate(self.manager, identifier)
        path = Path(__file__).parents[1] / "skills_auditor/schemas/lifecycle-investigation-v1.schema.json"
        validator = jsonschema.Draft202012Validator(json.loads(path.read_text()))
        self.assertTrue(validator.is_valid(packet))
        mutations = [
            {"event_id": [], "incident_id": {}, "text": "x" * 1000000, "evidence_refs": "not-a-list"},
            {"event_id": "bounded", "incident_id": identifier},
            {"event_id": "bounded", "incident_id": identifier, "text": "x" * 4097, "evidence_refs": []},
            {"event_id": "bounded", "incident_id": identifier, "text": "note", "evidence_refs": [{"kind": "file", "id": "arbitrary"}]},
        ]
        for payload in mutations:
            changed = copy.deepcopy(packet)
            changed["events"][-1]["payload"] = payload
            with self.subTest(payload_keys=list(payload)):
                self.assertFalse(validator.is_valid(changed))
        changed = copy.deepcopy(packet)
        changed["incident"] = {key: packet["incident"][key] for key in ("schema_version", "incident_id", "state")}
        self.assertFalse(validator.is_valid(changed))
        for event_type in ("observation", "resolved", "superseded"):
            changed = copy.deepcopy(packet)
            changed["events"][-1]["event_type"] = event_type
            with self.subTest(event_type=event_type):
                self.assertFalse(validator.is_valid(changed))

    def test_note_cannot_smuggle_another_event_types_unvalidated_fields(self):
        _, refs = self.fail()
        identifier = refs[0]["incident_id"]
        self.manager.repository.append_event("incident:" + identifier, "note", {"event_id": "cross-type-fields", "incident_id": identifier,
            "text": "valid note", "evidence_refs": [], "resolution": {"private_extra": "SECRET" * 200000}}, "reviewer", "test")
        with self.assertRaises(LifecycleError) as caught:
            investigate(self.manager, identifier)
        self.assertEqual(caught.exception.code, "incident_corrupt")

    def test_false_superseded_projection_needs_related_incident_and_committed_event(self):
        _, refs = self.fail()
        identifier = refs[0]["incident_id"]
        record = self.manager.repository.get("incident", identifier)
        self.manager.repository.put("incident", identifier, {**record["data"], "state": "superseded", "superseded_by": "incident-" + "f" * 64}, expected_revision=record["revision"])
        with self.assertRaises(LifecycleError) as caught:
            get_incident(self.manager, identifier)
        self.assertEqual(caught.exception.code, "incident_corrupt")

    def test_supersession_history_survives_replacement_resolution_but_missing_link_does_not(self):
        _, first = self.fail("foreign-a")
        _, second = self.fail("foreign-b")
        old, new = first[0]["incident_id"], second[0]["incident_id"]
        supersede(self.manager, old, new, explanation="follow new evidence", actor="reviewer", tool="test")
        resolve(self.manager, new, disposition="obsolete", explanation="historical disposition", actor="reviewer", tool="test")
        self.assertEqual(get_incident(self.manager, old)["superseded_by"], new)
        original_get = self.manager.repository.get

        def missing(kind, identifier):
            return None if kind == "incident" and identifier == new else original_get(kind, identifier)

        with patch.object(self.manager.repository, "get", side_effect=missing), self.assertRaises(LifecycleError):
            investigate(self.manager, old)


if __name__ == "__main__":
    unittest.main()
