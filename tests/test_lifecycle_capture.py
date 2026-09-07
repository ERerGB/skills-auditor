"""Explicit local capture evidence is diagnostic, immutable and atomically proven."""

import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

from skills_auditor.lifecycle import capture
from skills_auditor.lifecycle.common import LifecycleError
from skills_auditor.lifecycle.engine import Manager


class TestLifecycleCapture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="lifecycle-capture-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.project = self.root / "managed A"
        self.task = self.root / "task B"
        self.project.mkdir()
        self.task.mkdir()
        self.manager = Manager(self.project)
        self.addCleanup(self.manager.repository.close)
        self.environment = patch.dict(os.environ, {"CODEX_THREAD_ID": "task-one", "CODEX_SESSION_ID": "fallback",
            "SKILLS_AUDITOR_LOG_DIR": "task-logs", "SKILLS_AUDITOR_SKILL_TRACE": "0",
            "SKILLS_AUDITOR_SKILL_TRACE_CONFIG": str(self.root / "no-settings.json")})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.cwd = patch("pathlib.Path.cwd", return_value=self.task)
        self.cwd.start()
        self.addCleanup(self.cwd.stop)

    def sample(self, status="healthy"):
        now = datetime.now(timezone.utc).isoformat()
        hooks = {"PreToolUse": now, "PostToolUse": now} if status == "healthy" else {"PreToolUse": now} if status == "stale" else {}
        value = {"status": status, "session_id": "task-one", "log_dir": str(self.task / "task-logs"),
                 "observed_hooks": hooks, "enabled": status != "disabled", "source": "environment", "updated_at": "",
                 "settings_path": "/PRIVATE/setting", "detail": "PRIVATE raw error prompt and tool data"}
        if status == "error":
            for key in ("enabled", "source", "updated_at"):
                value.pop(key)
        return value

    def recorded(self, **kwargs):
        with patch("skills_auditor.skill_trace.check_health", return_value=self.sample()):
            return capture.record(self.manager, **kwargs)

    def test_each_capture_state_is_sanitized_and_has_no_authorization_records(self):
        for state in ("disabled", "healthy", "unverified", "stale", "error"):
            with self.subTest(state=state), patch("skills_auditor.skill_trace.check_health", return_value=self.sample(state)):
                evidence = capture.record(self.manager, evidence_id=state)
            self.assertEqual(evidence["health"]["status"], state)
            self.assertEqual(evidence["project_root"], str(self.project))
            self.assertEqual(evidence["task_cwd"], str(self.task))
            self.assertEqual(evidence["log_root"], str(self.task / "task-logs"))
            self.assertEqual(evidence["session_id"], "task-one")
            self.assertEqual(evidence["host_trust"], "unverified")
            self.assertNotIn("PRIVATE", json.dumps(evidence))
            self.assertNotIn("settings_path", json.dumps(evidence))
            self.assertEqual(capture.get(self.manager, state), evidence)
            self.assertEqual(self.manager.repository.get("capture-evidence", state)["revision"], 1)
        for kind in ("installation", "version", "grant", "authorization", "receipt", "verification", "transaction"):
            self.assertEqual(self.manager.repository.list(kind), [])
        self.assertFalse((self.task / "task-logs").exists())
        self.assertFalse((self.root / "no-settings.json").exists())

    def test_same_id_retry_returns_original_without_sampling_changed_health(self):
        original = self.recorded(evidence_id="retry", actor="analyst", tool="adapter")
        with patch("skills_auditor.skill_trace.check_health", side_effect=AssertionError("retry resampled")):
            self.assertEqual(capture.record(self.manager, evidence_id="retry", actor="analyst", tool="adapter"), original)
        self.assertEqual(len(self.manager.repository.events("capture-evidence:retry")), 1)
        self.assertEqual(self.manager.repository.get("capture-evidence", "retry")["revision"], 1)

    def test_scope_changes_conflict_before_sampling_or_writing(self):
        original = self.recorded(evidence_id="retry")
        for change in ({"actor": "other"}, {"tool": "other"}, {"log_dir": "other-logs"}):
            with self.subTest(change=change), patch("skills_auditor.skill_trace.check_health", side_effect=AssertionError("conflict resampled")):
                with self.assertRaises(LifecycleError) as caught:
                    capture.record(self.manager, evidence_id="retry", **change)
                self.assertEqual(caught.exception.code, "capture_evidence_conflict")
        for context in (patch.dict(os.environ, {"CODEX_THREAD_ID": "other-task"}),
                        patch("pathlib.Path.cwd", return_value=self.project)):
            with context, self.assertRaises(LifecycleError) as caught:
                capture.record(self.manager, evidence_id="retry")
            self.assertEqual(caught.exception.code, "capture_evidence_conflict")
        self.assertEqual(capture.get(self.manager, "retry"), original)

    def test_event_and_record_write_failures_roll_back_both(self):
        for method in ("append_event", "put"):
            with self.subTest(method=method), patch.object(self.manager.repository, method, side_effect=OSError("PRIVATE storage error")):
                with self.assertRaises(LifecycleError) as caught:
                    self.recorded(evidence_id="failure")
                self.assertEqual(caught.exception.code, "capture_evidence_write_failed")
                self.assertNotIn("PRIVATE", json.dumps(caught.exception.to_dict()))
            self.assertIsNone(self.manager.repository.get("capture-evidence", "failure"))
            self.assertEqual(self.manager.repository.events("capture-evidence:failure"), [])

    def test_malformed_health_cannot_create_success_or_copy_private_values(self):
        mutations = ({"status": "trusted"}, {"enabled": 1}, {"source": "arbitrary"}, {"session_id": "foreign"},
                     {"log_dir": "/foreign"}, {"observed_hooks": {"Execute": "PRIVATE"}},
                     {"observed_hooks": {"PreToolUse": "not-a-time"}}, {"observed_hooks": {}},
                     {"enabled": False}, {"updated_at": False}, {"updated_at": []})
        for changed in mutations:
            with self.subTest(changed=changed), patch("skills_auditor.skill_trace.check_health", return_value={**self.sample(), **changed}):
                with self.assertRaises(LifecycleError) as caught:
                    capture.record(self.manager, evidence_id="malformed")
                self.assertEqual(caught.exception.code, "invalid_capture_result")
                self.assertNotIn("PRIVATE", json.dumps(caught.exception.to_dict()))
            self.assertIsNone(self.manager.repository.get("capture-evidence", "malformed"))
            self.assertEqual(self.manager.repository.events("capture-evidence:malformed"), [])

    def test_read_requires_immutable_revision_and_exact_event_proof(self):
        original = self.recorded(evidence_id="proof")
        row = self.manager.repository.get("capture-evidence", "proof")
        with self.assertRaises(LifecycleError) as caught:
            capture.get(self.manager, "missing")
        self.assertEqual(caught.exception.code, "capture_evidence_missing")
        for changed in ({**original, "host_trust": "verified"}, original):
            current = self.manager.repository.get("capture-evidence", "proof")
            self.manager.repository.put("capture-evidence", "proof", changed, expected_revision=current["revision"])
            with self.assertRaises(LifecycleError) as caught:
                capture.get(self.manager, "proof")
            self.assertEqual(caught.exception.code, "capture_evidence_invalid")
        with patch.object(self.manager.repository, "get", return_value=row):
            for events in ([], [{**self.manager.repository.events("capture-evidence:proof")[0], "actor": "foreign"}]):
                with patch.object(self.manager.repository, "events", return_value=events), self.assertRaises(LifecycleError) as caught:
                    capture.get(self.manager, "proof")
                self.assertEqual(caught.exception.code, "capture_evidence_invalid")

    def test_cross_project_metadata_reader_and_drifted_manager_refuse(self):
        self.recorded(evidence_id="owner")
        with self.assertRaises(LifecycleError) as caught:
            capture.get_record(self.manager.repository, "owner", self.task)
        self.assertEqual(caught.exception.code, "capture_evidence_invalid")
        moved = self.root / "moved A"
        self.project.rename(moved)
        self.project.symlink_to(self.task, target_is_directory=True)
        try:
            for call in (lambda: capture.get(self.manager, "owner"), lambda: capture.record(self.manager, evidence_id="new")):
                with self.assertRaises(LifecycleError) as caught:
                    call()
                self.assertEqual(caught.exception.code, "project_context_changed")
        finally:
            self.project.unlink()
            moved.rename(self.project)
        self.assertIsNone(self.manager.repository.get("capture-evidence", "new"))

    def test_first_revision_data_and_event_mutations_cannot_break_their_binding(self):
        self.recorded(evidence_id="binding")
        original = self.manager.repository.get("capture-evidence", "binding")
        original_events = self.manager.repository.events("capture-evidence:binding")
        for mutation in ("data", "payload", "sequence", "duplicate", "wrong-project", "invalid-time"):
            row, events = copy.deepcopy(original), copy.deepcopy(original_events)
            if mutation == "data":
                row["data"]["actor"] = "another-observer"
            elif mutation == "payload":
                events[0]["payload"]["evidence_sha256"] = "0" * 64
            elif mutation == "sequence":
                events[0]["sequence"] += 1
            elif mutation == "duplicate":
                events.append(copy.deepcopy(events[0]))
            elif mutation == "wrong-project":
                row["data"]["project_root"] = str(self.task)
            else:
                row["data"]["observed_at"] = "not-a-time"
            with self.subTest(mutation=mutation), patch.object(self.manager.repository, "get", return_value=row), \
                    patch.object(self.manager.repository, "events", return_value=events), self.assertRaises(LifecycleError) as caught:
                capture.get(self.manager, "binding")
            self.assertEqual(caught.exception.code, "capture_evidence_invalid")

    def test_read_does_not_sample_logs_or_depend_on_task_context(self):
        original = self.recorded(evidence_id="historical")
        with patch("skills_auditor.skill_trace.check_health", side_effect=AssertionError("read sampled")), \
                patch("pathlib.Path.cwd", side_effect=AssertionError("read used task context")), \
                patch.dict(os.environ, {"CODEX_THREAD_ID": "new-task"}):
            self.assertEqual(capture.get(self.manager, "historical"), original)

    def test_orphan_completion_event_cannot_be_replaced_by_a_new_observation(self):
        self.recorded(evidence_id="orphan")
        with sqlite3.connect(str(self.manager.repository.path)) as connection:
            connection.execute("DELETE FROM records WHERE kind='capture-evidence' AND id='orphan'")
        with patch("skills_auditor.skill_trace.check_health", side_effect=AssertionError("orphan evidence resampled")):
            with self.assertRaises(LifecycleError) as caught:
                capture.record(self.manager, evidence_id="orphan")
            self.assertEqual(caught.exception.code, "capture_evidence_invalid")
        self.assertIsNone(self.manager.repository.get("capture-evidence", "orphan"))
        self.assertEqual(len(self.manager.repository.events("capture-evidence:orphan")), 1)

    def test_serialized_evidence_bound_applies_even_to_checksum_valid_metadata(self):
        from skills_auditor.lifecycle.common import digest
        evidence = self.recorded(evidence_id="bound")
        changed = copy.deepcopy(evidence)
        changed.update(task_cwd="/" + "界" * 4000, log_root="/" + "界" * 4000,
                       project_root="/" + "界" * 4000)
        body = {key: value for key, value in changed.items() if key != "completion_event_sequence"}
        events = self.manager.repository.events("capture-evidence:bound")
        events[0]["payload"] = {"evidence_id": "bound", "evidence_sha256": digest(body)}
        row = {"kind": "capture-evidence", "id": "bound", "revision": 1, "data": changed}
        with patch.object(self.manager.repository, "root", Path(changed["project_root"]) / ".skills-auditor-local/lifecycle"), \
                patch.object(self.manager.repository, "get", return_value=row), \
                patch.object(self.manager.repository, "events", return_value=events), self.assertRaises(LifecycleError) as caught:
            capture.get_record(self.manager.repository, "bound", changed["project_root"])
        self.assertEqual(caught.exception.code, "capture_evidence_invalid")

    def test_task_context_changes_during_sampling_roll_back_without_evidence(self):
        for field in ("session", "log-root", "task-cwd"):
            sample = self.sample()

            def change_context(*args, **kwargs):
                if field == "session":
                    os.environ["CODEX_THREAD_ID"] = "changed-task"
                elif field == "log-root":
                    os.environ["SKILLS_AUDITOR_LOG_DIR"] = "changed-logs"
                else:
                    cwd.return_value = self.project
                return sample

            with self.subTest(field=field), patch.dict(os.environ), patch("pathlib.Path.cwd", return_value=self.task) as cwd, \
                    patch("skills_auditor.skill_trace.check_health", side_effect=change_context):
                with self.assertRaises(LifecycleError) as caught:
                    capture.record(self.manager, evidence_id="changing")
                self.assertEqual(caught.exception.code, "capture_context_changed")
            self.assertIsNone(self.manager.repository.get("capture-evidence", "changing"))
            self.assertEqual(self.manager.repository.events("capture-evidence:changing"), [])

    def test_manager_binding_failure_after_event_rolls_back_event_and_record(self):
        append = self.manager.repository.append_event
        original_identity = self.manager._state_identity

        def changed_binding(*args, **kwargs):
            event = append(*args, **kwargs)
            self.manager._state_identity = [-1, -1]
            return event

        try:
            with patch.object(self.manager.repository, "append_event", side_effect=changed_binding), self.assertRaises(LifecycleError) as caught:
                self.recorded(evidence_id="drifted")
            self.assertEqual(caught.exception.code, "project_context_changed")
        finally:
            self.manager._state_identity = original_identity
        self.assertIsNone(self.manager.repository.get("capture-evidence", "drifted"))
        self.assertEqual(self.manager.repository.events("capture-evidence:drifted"), [])

    def test_malformed_completion_result_is_not_persisted_as_success(self):
        append = self.manager.repository.append_event

        def invalid_sequence(*args, **kwargs):
            return {**append(*args, **kwargs), "sequence": True}

        with patch.object(self.manager.repository, "append_event", side_effect=invalid_sequence), self.assertRaises(LifecycleError) as caught:
            self.recorded(evidence_id="bad-event")
        self.assertEqual(caught.exception.code, "capture_evidence_write_failed")
        self.assertIsNone(self.manager.repository.get("capture-evidence", "bad-event"))
        self.assertEqual(self.manager.repository.events("capture-evidence:bad-event"), [])

    def test_shape_valid_but_wrong_completion_sequence_rolls_back_before_success(self):
        append = self.manager.repository.append_event

        def wrong_sequence(*args, **kwargs):
            event = append(*args, **kwargs)
            return {**event, "sequence": event["sequence"] + 1}

        # The native adapter returns the correct sequence. This exercises a
        # corrupted adapter response after its real write, not a native defect.
        with patch.object(self.manager.repository, "append_event", side_effect=wrong_sequence), self.assertRaises(LifecycleError) as caught:
            self.recorded(evidence_id="wrong-sequence")
        self.assertEqual(caught.exception.code, "capture_evidence_invalid")
        self.assertIsNone(self.manager.repository.get("capture-evidence", "wrong-sequence"))
        self.assertEqual(self.manager.repository.events("capture-evidence:wrong-sequence"), [])
        retried = self.recorded(evidence_id="wrong-sequence")
        self.assertEqual(capture.get(self.manager, "wrong-sequence"), retried)
        self.assertEqual(len(self.manager.repository.events("capture-evidence:wrong-sequence")), 1)

    def test_native_commit_denial_rolls_back_record_and_event_then_same_id_can_retry(self):
        connection = self.manager.repository._connection

        def deny_commit(action, argument, _second, _database, _source):
            if action == sqlite3.SQLITE_TRANSACTION and argument == "COMMIT":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(deny_commit)
        try:
            with self.assertRaises(LifecycleError) as caught:
                self.recorded(evidence_id="commit-failure")
            self.assertEqual(caught.exception.code, "capture_evidence_write_failed")
            self.assertEqual(caught.exception.to_dict()["details"], {"evidence_id": "commit-failure"})
        finally:
            connection.set_authorizer(None)
        self.assertFalse(connection.in_transaction)
        self.assertIsNone(self.manager.repository.get("capture-evidence", "commit-failure"))
        self.assertEqual(self.manager.repository.events("capture-evidence:commit-failure"), [])
        retried = self.recorded(evidence_id="commit-failure")
        self.assertEqual(capture.get(self.manager, "commit-failure"), retried)
        self.assertEqual(len(self.manager.repository.events("capture-evidence:commit-failure")), 1)

    def test_process_exit_after_event_or_record_write_cannot_leave_successful_evidence(self):
        script = textwrap.dedent("""
            import os
            from pathlib import Path
            import sys
            from skills_auditor.lifecycle import capture
            from skills_auditor.lifecycle.engine import Manager
            manager = Manager(Path(sys.argv[1]))
            original = getattr(manager.repository, sys.argv[2])
            def exit_after_write(*args, **kwargs):
                original(*args, **kwargs)
                os._exit(73)
            setattr(manager.repository, sys.argv[2], exit_after_write)
            capture.record(manager, evidence_id="process-death")
        """)
        environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
                       "PYTHONDONTWRITEBYTECODE": "1", "SKILLS_AUDITOR_SKILL_TRACE": "0"}
        for method in ("append_event", "put"):
            with self.subTest(method=method):
                project = self.root / ("crash after " + method)
                project.mkdir()
                completed = subprocess.run([sys.executable, "-c", script, str(project), method],
                                           cwd=self.task, env=environment, text=True, capture_output=True, timeout=30)
                self.assertEqual(completed.returncode, 73, completed.stdout + completed.stderr)
                reopened = Manager(project, create=False)
                try:
                    self.assertIsNone(reopened.repository.get("capture-evidence", "process-death"))
                    self.assertEqual(reopened.repository.events("capture-evidence:process-death"), [])
                    retried = capture.record(reopened, evidence_id="process-death")
                    self.assertEqual(capture.get(reopened, "process-death"), retried)
                    self.assertEqual(len(reopened.repository.events("capture-evidence:process-death")), 1)
                finally:
                    reopened.repository.close()

    def test_real_disabled_capture_uses_task_relative_logs_without_creating_them(self):
        evidence = capture.record(self.manager, log_dir="relative logs")
        self.assertEqual(evidence["health"], {"status": "disabled", "observed_hooks": {}})
        self.assertEqual(evidence["log_root"], str(self.task / "relative logs"))
        self.assertFalse((self.task / "relative logs").exists())

    def test_diagnostics_never_change_valid_or_denied_managed_authorization(self):
        source = self.project / "candidate"
        source.mkdir()
        (source / "SKILL.md").write_text("# H1 managed fixture\n")
        target = self.project / "installed"
        plan = self.manager.plan("install", source=source, target=target)
        receipt = self.manager.apply(plan, approve_plan_id=plan["plan_id"])
        self.manager.verify(receipt["installation_id"])
        for approval in ("valid", "invalidated", "revoked"):
            if approval == "invalidated":
                target.unlink()
                self.manager.verify(receipt["installation_id"])
            elif approval == "revoked":
                plan = self.manager.plan("revoke", installation_id=receipt["installation_id"])
                self.manager.apply(plan, approve_plan_id=plan["plan_id"])
            kinds = ("installation", "version", "grant", "authorization", "receipt", "transaction", "verification", "verification-run", "status")
            before = {kind: self.manager.repository.list(kind) for kind in kinds}
            events = self.manager.repository.events(receipt["installation_id"])
            for health in ("disabled", "healthy", "unverified", "stale", "error"):
                with self.subTest(approval=approval, health=health), patch("skills_auditor.skill_trace.check_health", return_value=self.sample(health)):
                    evidence = capture.record(self.manager)
                    self.assertEqual(evidence["health"]["status"], health)
                self.assertEqual({kind: self.manager.repository.list(kind) for kind in kinds}, before)
                self.assertEqual(self.manager.repository.events(receipt["installation_id"]), events)
                self.assertEqual(self.manager.get_installation(receipt["installation_id"])["authorization"]["state"], approval)

    def test_session_fallback_and_missing_session_are_explicit_local_context(self):
        for variables, expected in (({"CODEX_THREAD_ID": "", "CODEX_SESSION_ID": "fallback"}, "fallback"),
                                    ({"CODEX_THREAD_ID": "", "CODEX_SESSION_ID": ""}, None)):
            with self.subTest(variables=variables), patch.dict(os.environ, variables):
                evidence = capture.record(self.manager)
                self.assertEqual(evidence["session_id"], expected)
                self.assertEqual(evidence["health"]["status"], "disabled")

    def test_invalid_request_inputs_fail_before_sampling_or_records(self):
        for options in ({"evidence_id": "../escape"}, {"evidence_id": []}, {"actor": " "}, {"tool": "line\nbreak"},
                        {"log_dir": "line\nbreak"}, {"log_dir": False}):
            with self.subTest(options=options), patch("skills_auditor.skill_trace.check_health", side_effect=AssertionError("invalid input sampled")):
                with self.assertRaises(LifecycleError) as caught:
                    capture.record(self.manager, **options)
                self.assertEqual(caught.exception.code, "invalid_capture_input")
                self.assertEqual(caught.exception.exit_code, 2)
        self.assertEqual(self.manager.repository.list("capture-evidence"), [])

    def test_evidence_schema_accepts_real_record_and_rejects_extra_or_invalid_fields(self):
        from jsonschema import Draft202012Validator, FormatChecker
        schema = json.loads((Path(__file__).parents[1] / "skills_auditor/schemas/lifecycle-capture-evidence-v1.schema.json").read_text())
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        evidence = self.recorded(evidence_id="schema")
        validator.validate(evidence)
        for changed in ({"host_trust": "verified"}, {"context_verified": False}, {"raw_log": "private"},
                        {"completion_event_sequence": 0}, {"health": {"status": "healthy", "observed_hooks": {}}}):
            self.assertFalse(validator.is_valid({**evidence, **changed}))


if __name__ == "__main__":
    unittest.main()
