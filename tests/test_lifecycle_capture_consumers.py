"""Optional capture observations are bounded diagnostic leaves, not authority."""

import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import patch

import jsonschema

from skills_auditor.lifecycle.common import LifecycleError
from skills_auditor.lifecycle.engine import Manager
from skills_auditor.lifecycle import incidents, retention


PROJECT = Path(__file__).resolve().parents[1]


class TestCaptureConsumers(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="skills-capture-consumers-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        self.task = self.root / "task"
        self.task.mkdir()
        self.logs = self.task / "logs"
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "SKILL.md").write_text("# Example\n")
        (self.source / "payload").write_bytes(b"H1")
        self.target = self.root / "installed"
        self.manager = Manager(self.project)
        self.addCleanup(self.manager.repository.close)
        plan = self.manager.plan("install", source=self.source, target=self.target)
        self.receipt = self.manager.apply(plan, approve_plan_id=plan["plan_id"])
        self.installation_id = self.receipt["installation_id"]
        self.link = os.readlink(self.target)
        self.target.unlink()
        self.verification = self.manager.verify(self.installation_id)
        self.incident_id = incidents.list_incidents(self.manager)[0]["incident_id"]

    def capture(self, identifier="capture-one", manager=None):
        from skills_auditor.lifecycle import capture
        with patch.dict(os.environ, {"SKILLS_AUDITOR_SKILL_TRACE": "0",
                                     "SKILLS_AUDITOR_SKILL_TRACE_CONFIG": str(self.task / "settings.json"),
                                     "CODEX_THREAD_ID": "consumer-task"}), patch.object(Path, "cwd", return_value=self.task):
            return capture.record(manager or self.manager, evidence_id=identifier, log_dir=str(self.logs),
                                  actor="investigator", tool="test")

    def note(self, identifier="capture-one", **kwargs):
        return incidents.append_note(self.manager, self.incident_id, "Optional capture context only.",
                                     actor="investigator", tool="test",
                                     evidence_refs=[{"kind": "capture-evidence", "id": identifier}], **kwargs)

    def authority(self):
        return {kind: self.manager.repository.list(kind) for kind in
                ("installation", "authorization", "grant", "version", "receipt", "transaction", "verification")}

    def retain(self, operation, **kwargs):
        plan = retention.plan_retention(self.manager, operation, **kwargs)
        return retention.apply_retention(self.manager, plan, approve_plan_id=plan["plan_id"])

    def test_old_incident_gets_empty_additive_capture_page_without_other_changes(self):
        before = self.authority()
        packet = incidents.investigate(self.manager, self.incident_id)
        self.assertEqual(packet["capture_evidence"], [])
        self.assertEqual(packet["limits"]["evidence_refs_per_event"], 20)
        self.assertEqual(packet["limits"]["capture_evidence_max_records"], 1000)
        self.assertEqual(self.authority(), before)

    def test_append_explicit_reference_and_investigate_immutable_local_snapshot(self):
        evidence = self.capture()
        before = self.authority()
        event = self.note(event_id="attach-once")
        self.assertEqual(self.note(event_id="attach-once"), event)
        self.assertEqual(event["payload"]["evidence_refs"], [{"kind": "capture-evidence", "id": "capture-one"}])
        from skills_auditor import skill_trace
        with patch.object(skill_trace, "check_health", side_effect=AssertionError("must not resample")), \
                patch.object(Path, "open", side_effect=AssertionError("must not read logs or referenced paths")):
            packet = incidents.investigate(self.manager, self.incident_id)
        self.assertEqual(packet["capture_evidence"], [evidence])
        self.assertEqual(packet["capture_evidence"][0]["task_cwd"], str(self.task))
        self.assertEqual(packet["capture_evidence"][0]["project_root"], str(self.project))
        self.assertEqual(packet["incident"]["state"], "investigating")
        self.assertIsNone(packet["incident"]["resolution"])
        self.assertEqual(self.authority(), before)
        self.assertFalse(self.logs.exists())
        self.assertFalse((self.task / "settings.json").exists())
        self.assertEqual((self.source / "payload").read_bytes(), b"H1")

    def test_page_deduplicates_only_selected_references_and_enforces_note_bounds(self):
        first = self.capture("first")
        second = self.capture("second")
        self.note("first", event_id="note-one")
        self.note("first", event_id="note-two")
        self.note("second", event_id="note-three")
        packet = incidents.investigate(self.manager, self.incident_id, limit=3)
        self.assertEqual(packet["capture_evidence"], [first])
        self.assertTrue(packet["has_more"])
        following = incidents.investigate(self.manager, self.incident_id, limit=3,
                                         after_sequence=packet["continuation"]["after_sequence"])
        self.assertEqual(following["capture_evidence"], [second])
        with self.assertRaises(LifecycleError):
            incidents.append_note(self.manager, self.incident_id, "too many refs", actor="reviewer", tool="test",
                                  evidence_refs=[{"kind": "capture-evidence", "id": "first"}] * 21)
        self.assertEqual(len(incidents.investigate(self.manager, self.incident_id)["events"]), 4)

    def test_each_capture_state_stays_advisory_for_denied_and_valid_installations(self):
        from skills_auditor import skill_trace
        for authorized in (False, True):
            if authorized:
                self.target.symlink_to(self.link)
                renewal = self.manager.plan("renew", installation_id=self.installation_id)
                self.manager.apply(renewal, approve_plan_id=renewal["plan_id"])
                self.assertTrue(self.manager.verify(self.installation_id)["valid"])
            for state in ("disabled", "healthy", "unverified", "stale", "error"):
                identifier = "{}-{}".format(authorized, state)
                now = datetime.now(timezone.utc).isoformat()
                hooks = {"PreToolUse": now, "PostToolUse": now} if state == "healthy" else {"PreToolUse": now} if state == "stale" else {}
                sample = {"status": state, "session_id": "consumer-task", "log_dir": str(self.logs),
                          "observed_hooks": hooks, "enabled": state != "disabled", "source": "environment", "updated_at": "",
                          "detail": "PRIVATE raw tool output", "settings_path": "/PRIVATE/settings"}
                if state == "error":
                    for key in ("enabled", "source", "updated_at"):
                        sample.pop(key)
                before = self.authority()
                with self.subTest(authorized=authorized, state=state), patch.object(skill_trace, "check_health", return_value=sample):
                    evidence = self.capture(identifier)
                    self.note(identifier)
                    packet = incidents.investigate(self.manager, self.incident_id)
                    self.assertIn(evidence, packet["capture_evidence"])
                    self.assertEqual(self.authority(), before)
                    self.assertIsNone(packet["incident"]["resolution"])
                    self.assertNotIn("PRIVATE", json.dumps(packet))
                    with self.assertRaises(LifecycleError):
                        incidents.resolve(self.manager, self.incident_id, actor="reviewer", tool="test", verification_id=identifier)

    def test_drifted_owner_refuses_capture_attachment_before_note_commit(self):
        self.capture()
        before = self.manager.repository.events("incident:" + self.incident_id)
        moved = self.root / "original-project"
        self.project.rename(moved)
        self.project.symlink_to(self.task, target_is_directory=True)
        try:
            with self.assertRaises(LifecycleError) as caught:
                self.note()
            self.assertEqual(caught.exception.code, "project_context_changed")
        finally:
            self.project.unlink()
            moved.rename(self.project)
        self.assertEqual(self.manager.repository.events("incident:" + self.incident_id), before)

    def test_missing_malformed_and_cross_project_references_reject_without_note(self):
        before = incidents.get_incident(self.manager, self.incident_id)
        before_events = self.manager.repository.events("incident:" + self.incident_id)
        with self.assertRaises(LifecycleError):
            self.note("missing")
        self.manager.repository.put("capture-evidence", "forged", {"evidence_id": "forged"})
        with self.assertRaises(LifecycleError):
            self.note("forged")
        other_project = self.root / "other"
        other_project.mkdir()
        other = Manager(other_project)
        self.addCleanup(other.repository.close)
        foreign = self.capture("foreign", manager=other)
        self.manager.repository.put("capture-evidence", "foreign", foreign)
        with self.assertRaises(LifecycleError):
            self.note("foreign")
        self.assertEqual(incidents.get_incident(self.manager, self.incident_id), before)
        self.assertEqual(self.manager.repository.events("incident:" + self.incident_id), before_events)

    def test_full_event_page_has_bounded_deduplicated_metadata_reads(self):
        expected = [self.capture("capture-{}".format(index)) for index in range(20)]
        references = [{"kind": "capture-evidence", "id": item["evidence_id"]} for item in expected]
        for index in range(51):
            incidents.append_note(self.manager, self.incident_id, "Bounded page {}".format(index),
                                  actor="reviewer", tool="test", evidence_refs=references)
        from skills_auditor.lifecycle import capture
        repository = self.manager.repository
        with patch.object(capture, "get_record", wraps=capture.get_record) as reader, \
                patch.object(repository, "events", wraps=repository.events) as events:
            packet = incidents.investigate(self.manager, self.incident_id)
        self.assertEqual(packet["returned_events"], 50)
        self.assertTrue(packet["has_more"])
        self.assertEqual(packet["capture_evidence"], expected)
        self.assertEqual(reader.call_count, 20)
        self.assertTrue(all(call.kwargs["limit"] == (51 if call.args[0].startswith("incident:") else 2)
                            for call in events.call_args_list))

    def test_owner_drift_during_attachment_rolls_back_the_note(self):
        self.capture()
        repository = self.manager.repository
        before = repository.get("incident", self.incident_id)
        events = repository.events("incident:" + self.incident_id)
        moved = self.root / "moved-owner"
        original_put = repository.put
        def drift(kind, identifier, data, **kwargs):
            result = original_put(kind, identifier, data, **kwargs)
            if kind == "incident":
                self.project.rename(moved)
                self.project.symlink_to(self.task, target_is_directory=True)
            return result
        try:
            with patch.object(repository, "put", side_effect=drift), self.assertRaises(LifecycleError) as caught:
                self.note(event_id="drift-note")
            self.assertEqual(caught.exception.code, "project_context_changed")
        finally:
            if self.project.is_symlink():
                self.project.unlink()
                moved.rename(self.project)
        self.assertEqual(repository.get("incident", self.incident_id), before)
        self.assertEqual(repository.events("incident:" + self.incident_id), events)
        self.assertIsNone(repository.get("incident-event", "drift-note"))

    def test_corrupt_attached_evidence_is_rejected_by_investigate_and_retention(self):
        evidence = self.capture()
        self.note()
        corrupt = copy.deepcopy(evidence)
        corrupt["health"]["status"] = "healthy"
        self.manager.repository.put("capture-evidence", "capture-one", corrupt, expected_revision=1)
        before = self.authority()
        for operation in (lambda: incidents.investigate(self.manager, self.incident_id),
                          lambda: retention.plan_retention(self.manager, "collect"),
                          lambda: retention.plan_retention(self.manager, "policy", keep_recent=0)):
            with self.subTest(operation=operation), self.assertRaises(LifecycleError):
                operation()
        self.assertEqual(self.authority(), before)
        self.assertEqual(self.manager.repository.list("retention-transaction"), [])

    def test_missing_attached_completion_event_fails_closed_for_both_consumers(self):
        evidence = self.capture()
        self.note()
        repository = self.manager.repository
        original = repository.events
        def without_completion(stream, **kwargs):
            return [event for event in original(stream, **kwargs)
                    if event["sequence"] != evidence["completion_event_sequence"]]
        with patch.object(repository, "events", side_effect=without_completion):
            with self.assertRaises(LifecycleError):
                incidents.investigate(self.manager, self.incident_id)
            with self.assertRaises(LifecycleError):
                retention.plan_retention(self.manager, "collect")
        self.assertEqual(incidents.get_incident(self.manager, self.incident_id)["state"], "investigating")

    def test_capture_note_does_not_change_incident_payload_roots_or_create_new_roots(self):
        evidence = self.capture()
        self.note()
        before = retention.plan_retention(self.manager, "collect")
        self.assertEqual(before["objects"], [])
        self.assertIn("incident:" + self.incident_id, str(before["protected"]))
        with self.assertRaises(LifecycleError):
            retention.plan_retention(self.manager, "expire", incident_ids=[self.incident_id])
        self.target.symlink_to(self.link)
        uninstall = self.manager.plan("uninstall", installation_id=self.installation_id)
        last = self.manager.apply(uninstall, approve_plan_id=uninstall["plan_id"])
        self.retain("policy", keep_recent=0)
        self.retain("expire", receipt_ids=[self.receipt["receipt_id"], last["receipt_id"]])
        self.assertEqual(retention.plan_retention(self.manager, "collect")["objects"], [])
        incidents.resolve(self.manager, self.incident_id, actor="reviewer", tool="test", disposition="retired",
                          explanation="No remediation or authorization claim.")
        self.retain("expire", incident_ids=[self.incident_id])
        collected = self.retain("collect")
        self.assertEqual(len(collected["objects"]), 1)
        self.assertFalse(Path(self.link).exists())
        self.assertEqual(incidents.investigate(self.manager, self.incident_id)["capture_evidence"], [evidence])
        self.assertEqual(self.manager.repository.get("capture-evidence", "capture-one")["data"], evidence)
        self.assertEqual((self.source / "payload").read_bytes(), b"H1")

    def test_note_write_failure_rolls_back_attachment_and_preserves_evidence(self):
        evidence = self.capture()
        repository = self.manager.repository
        before_incident = repository.get("incident", self.incident_id)
        before_events = repository.events("incident:" + self.incident_id)
        put = repository.put
        def fail_incident(kind, identifier, data, **kwargs):
            if kind == "incident":
                raise OSError("injected note projection failure")
            return put(kind, identifier, data, **kwargs)
        with patch.object(repository, "put", side_effect=fail_incident), self.assertRaises(OSError):
            self.note(event_id="failed-note")
        self.assertEqual(repository.get("incident", self.incident_id), before_incident)
        self.assertEqual(repository.events("incident:" + self.incident_id), before_events)
        self.assertIsNone(repository.get("incident-event", "failed-note"))
        self.assertEqual(repository.get("capture-evidence", "capture-one")["data"], evidence)

    def test_investigation_schema_accepts_additive_and_historical_packets_and_rejects_raw_data(self):
        self.capture()
        self.note()
        schema = json.loads((PROJECT / "skills_auditor/schemas/lifecycle-investigation-v1.schema.json").read_text())
        evidence_schema = json.loads((PROJECT / "skills_auditor/schemas/lifecycle-capture-evidence-v1.schema.json").read_text())
        self.assertEqual(schema["$defs"]["captureEvidence"], evidence_schema)
        packet = incidents.investigate(self.manager, self.incident_id)
        jsonschema.validate(packet, schema)
        historical = copy.deepcopy(packet)
        historical.pop("capture_evidence")
        historical["limits"].pop("evidence_refs_per_event")
        historical["limits"].pop("capture_evidence_max_records")
        historical["events"] = historical["events"][:1]
        historical["returned_events"] = 1
        jsonschema.validate(historical, schema)
        for mutate in (lambda value: value["capture_evidence"][0].update(raw_log="private"),
                       lambda value: value.update(capture_evidence=value["capture_evidence"] * 1001)):
            wrong = copy.deepcopy(packet)
            mutate(wrong)
            with self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate(wrong, schema)


class TestCaptureConsumerDistribution(unittest.TestCase):
    def test_distribution_inventory_carries_capture_module_schema_and_alignment_evidence(self):
        contract = runpy.run_path(str(PROJECT / "scripts/check_distribution.py"))
        self.assertTrue({"skills_auditor/lifecycle/capture.py",
                         "skills_auditor/schemas/lifecycle-capture-evidence-v1.schema.json"} <= contract["PACKAGE_FILES"])
        source = (PROJECT / "scripts/check_distribution.py").read_text()
        self.assertIn('prefix + "docs/lifecycle-trace-alignment.md"', source)
        self.assertIn('prefix + "tests/test_lifecycle_capture_consumers.py"', source)
        self.assertIn('prefix + "tests/test_lifecycle_trace_cli.py"', source)
        self.assertIn('prefix + "e2e_tests/test_installed_lifecycle_trace.py"', source)


if __name__ == "__main__":
    unittest.main()
