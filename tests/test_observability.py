"""Tests for local trigger observability logs."""

import json
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import patch

from skills_auditor.cli import main
from skills_auditor.observability import (
    TriggerLogEvent,
    aggregate_sensor_claims,
    audit_sensor_events,
    audit_trigger_logs,
    collect_storage_stats,
    load_sensor_events,
    load_trigger_logs,
    sensor_event_from_payload,
    summarize_trigger_logs,
    write_sensor_event,
    write_trigger_log,
)


class TestTriggerObservabilityLogs(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.log_dir = Path(self.tmp.name) / ".skills-auditor-local"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_write_and_load_skill_trigger_event(self) -> None:
        event = TriggerLogEvent(
            kind="skill-trigger",
            source="test",
            prompt_hash="sha256:test",
            expected_skill="read",
            actual_skill="read",
            verdict="correct",
        )

        out = write_trigger_log(event, self.log_dir)
        events, parse_findings = load_trigger_logs(self.log_dir)

        self.assertTrue(out.exists())
        self.assertEqual(out.name, "skill_trigger.jsonl")
        self.assertEqual(parse_findings, [])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "skill_trigger")
        self.assertEqual(audit_trigger_logs(events), [])

        summary = summarize_trigger_logs(events)
        self.assertEqual(summary.total_events, 1)
        self.assertEqual(summary.correct_events, 1)
        self.assertEqual(summary.accuracy, 1.0)

    def test_audit_flags_raw_prompt_storage(self) -> None:
        findings = audit_trigger_logs(
            [
                {
                    "event_id": "event-1",
                    "kind": "skill_trigger",
                    "raw_prompt": "private prompt body",
                    "actual_skill": "read",
                }
            ]
        )

        self.assertIn("raw_prompt_present", [f.check for f in findings])
        self.assertIn("missing_prompt_reference", [f.check for f in findings])

    def test_storage_stats_count_jsonl_records_and_json_traces(self) -> None:
        logs = self.log_dir / "logs" / "2026-06-22"
        traces = Path(self.tmp.name) / "traces"
        logs.mkdir(parents=True)
        traces.mkdir()
        (logs / "skill_trigger.jsonl").write_text('{"a":1}\n{"b":2}\n', encoding="utf-8")
        (traces / "trace.json").write_text(json.dumps({"run_id": "run-1"}), encoding="utf-8")

        stats = collect_storage_stats(
            [
                ("trigger_logs", self.log_dir),
                ("route_traces", traces),
            ]
        )

        by_label = {s.label: s for s in stats}
        self.assertEqual(by_label["trigger_logs"].record_count, 2)
        self.assertEqual(by_label["route_traces"].record_count, 1)
        self.assertGreater(by_label["trigger_logs"].total_bytes, 0)

    def test_record_trigger_log_cli_writes_event(self) -> None:
        stdout = StringIO()
        with patch(
            "sys.argv",
            [
                "skills-audit",
                "record-trigger-log",
                "--log-dir",
                str(self.log_dir),
                "--kind",
                "skill-trigger",
                "--prompt-hash",
                "sha256:test",
                "--expected-skill",
                "read",
                "--actual-skill",
                "read",
                "--verdict",
                "correct",
            ],
        ), redirect_stdout(stdout):
            exit_code = main()

        self.assertEqual(exit_code, 0)
        self.assertIn("log written:", stdout.getvalue())

        events, parse_findings = load_trigger_logs(self.log_dir)
        self.assertEqual(parse_findings, [])
        self.assertEqual(len(events), 1)

    def test_sensor_event_from_claude_read_skill_payload(self) -> None:
        payload = {
            "hook_event_name": "PreToolUse",
            "session_id": "sess-1",
            "cwd": "/repo",
            "tool_name": "Read",
            "tool_input": {
                "file_path": "/Users/me/.codex/skills/foo/SKILL.md",
            },
        }

        event = sensor_event_from_payload(payload, provider="claude-code")

        self.assertEqual(event.provider, "claude-code")
        self.assertEqual(event.event_type, "skill_file_access")
        self.assertEqual(event.operation, "read")
        self.assertEqual(event.path, "/Users/me/.codex/skills/foo/SKILL.md")
        self.assertEqual(event.skill_name, "foo")
        self.assertEqual(event.skill_path, "/Users/me/.codex/skills/foo/SKILL.md")
        self.assertEqual(event.session_id, "sess-1")

    def test_sensor_event_from_codex_bash_read_skill_payload(self) -> None:
        payload = {
            "hook_event_name": "PreToolUse",
            "session_id": "sess-bash",
            "cwd": str(Path.cwd()),
            "tool_name": "Bash",
            "tool_input": {
                "command": "sed -n '1,40p' /Users/me/code/skills-auditor/plugins/skill-trace/skills/skill-trace/SKILL.md",
            },
        }

        event = sensor_event_from_payload(payload, provider="codex")

        self.assertEqual(event.provider, "codex")
        self.assertEqual(event.event_type, "skill_file_access")
        self.assertEqual(event.operation, "read")
        self.assertEqual(
            event.path,
            "/Users/me/code/skills-auditor/plugins/skill-trace/skills/skill-trace/SKILL.md",
        )
        self.assertEqual(event.skill_name, "skill-trace")
        self.assertEqual(event.metadata["command_verb"], "sed")
        self.assertEqual(event.metadata["path_source"], "shell_command")
        self.assertNotIn("command", event.metadata)

    def test_sensor_event_ignores_complex_bash_command_path(self) -> None:
        payload = {
            "hook_event_name": "PreToolUse",
            "session_id": "sess-bash",
            "cwd": str(Path.cwd()),
            "tool_name": "Bash",
            "tool_input": {
                "command": "cat /Users/me/.codex/skills/foo/SKILL.md | wc -l",
            },
        }

        event = sensor_event_from_payload(payload, provider="codex")

        self.assertEqual(event.event_type, "pre_tool_use")
        self.assertEqual(event.operation, "command")
        self.assertEqual(event.path, "")

    def test_write_load_and_audit_sensor_event(self) -> None:
        event = sensor_event_from_payload(
            {
                "event": "function_call",
                "session_id": "sess-2",
                "tool": {"name": "Read", "input": {"file_path": "skills/foo/SKILL.md"}},
            },
            provider="codex",
            source="transcript",
        )

        out = write_sensor_event(event, self.log_dir)
        events, parse_findings = load_sensor_events(self.log_dir)

        self.assertTrue(out.exists())
        self.assertEqual(out.name, "codex.jsonl")
        self.assertEqual(parse_findings, [])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], "skill_file_access")
        self.assertEqual(audit_sensor_events(events), [])

    def test_record_sensor_event_cli_reads_stdin(self) -> None:
        payload = json.dumps(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "sess-3",
                "tool_name": "Read",
                "tool_input": {"file_path": "/Users/me/.claude/skills/bar/SKILL.md"},
            }
        )
        stdout = StringIO()
        with patch(
            "sys.argv",
            [
                "skills-audit",
                "record-sensor-event",
                "--log-dir",
                str(self.log_dir),
                "--provider",
                "claude-code",
            ],
        ), patch("sys.stdin", StringIO(payload)), redirect_stdout(stdout):
            exit_code = main()

        self.assertEqual(exit_code, 0)
        self.assertIn("sensor log written:", stdout.getvalue())

        events, parse_findings = load_sensor_events(self.log_dir, provider="claude-code")
        self.assertEqual(parse_findings, [])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["skill_name"], "bar")

    def test_audit_trigger_logs_cli_reports_accuracy(self) -> None:
        write_trigger_log(
            TriggerLogEvent(
                kind="skill-trigger",
                source="test",
                prompt_hash="sha256:test",
                expected_skill="read",
                actual_skill="read",
                verdict="correct",
            ),
            self.log_dir,
        )

        stdout = StringIO()
        with patch(
            "sys.argv",
            [
                "skills-audit",
                "audit-trigger-logs",
                "--log-dir",
                str(self.log_dir),
                "--fail-on-error",
            ],
        ), redirect_stdout(stdout):
            exit_code = main()

        self.assertEqual(exit_code, 0)
        self.assertIn("events analyzed: 1", stdout.getvalue())
        self.assertIn("labeled accuracy: 1.000", stdout.getvalue())

    def test_aggregate_hook_and_transcript_into_strong_claim(self) -> None:
        events = [
            {
                "event_id": "hook-1",
                "provider": "codex",
                "source": "hook",
                "event_type": "skill_file_access",
                "session_id": "sess-1",
                "call_id": "call-1",
                "operation": "read",
                "path": "/Users/me/.codex/skills/foo/SKILL.md",
                "skill_name": "foo",
                "timestamp": "2026-06-22T10:00:00+00:00",
            },
            {
                "event_id": "transcript-1",
                "provider": "codex",
                "source": "transcript",
                "event_type": "skill_file_access",
                "session_id": "sess-1",
                "call_id": "call-1",
                "operation": "read",
                "path": "/Users/me/.codex/skills/foo/SKILL.md",
                "skill_name": "foo",
                "timestamp": "2026-06-22T10:00:01+00:00",
            },
        ]

        claims = aggregate_sensor_claims(events)

        self.assertEqual(len(claims), 1)
        claim = claims[0]
        self.assertEqual(claim.confidence, "strong")
        self.assertEqual(claim.score, 0.95)
        self.assertEqual(claim.status, "supported")
        self.assertEqual(claim.evidence_sources, ["hook", "transcript"])
        self.assertEqual(claim.evidence_event_ids, ["hook-1", "transcript-1"])

    def test_aggregate_single_source_confidence_levels(self) -> None:
        base = {
            "provider": "codex",
            "event_type": "skill_file_access",
            "session_id": "sess-1",
            "operation": "read",
            "path": "/Users/me/.codex/skills/foo/SKILL.md",
            "skill_name": "foo",
            "timestamp": "2026-06-22T10:00:00+00:00",
        }

        cases = [
            ("transcript", "medium", 0.70),
            ("hook", "medium", 0.70),
            ("fs_proxy", "weak", 0.40),
            ("manual", "manual", 0.30),
        ]
        for source, confidence, score in cases:
            with self.subTest(source=source):
                claims = aggregate_sensor_claims([
                    {**base, "event_id": f"{source}-1", "source": source}
                ])
                self.assertEqual(len(claims), 1)
                self.assertEqual(claims[0].confidence, confidence)
                self.assertEqual(claims[0].score, score)

    def test_aggregate_conflicting_hashes_into_disputed_claim(self) -> None:
        events = [
            {
                "event_id": "hook-1",
                "provider": "codex",
                "source": "hook",
                "event_type": "skill_file_access",
                "session_id": "sess-1",
                "call_id": "call-1",
                "operation": "read",
                "path": "/Users/me/.codex/skills/foo/SKILL.md",
                "skill_name": "foo",
                "content_hash": "sha256:a",
                "timestamp": "2026-06-22T10:00:00+00:00",
            },
            {
                "event_id": "transcript-1",
                "provider": "codex",
                "source": "transcript",
                "event_type": "skill_file_access",
                "session_id": "sess-1",
                "call_id": "call-1",
                "operation": "read",
                "path": "/Users/me/.codex/skills/foo/SKILL.md",
                "skill_name": "foo",
                "content_hash": "sha256:b",
                "timestamp": "2026-06-22T10:00:01+00:00",
            },
        ]

        claims = aggregate_sensor_claims(events)

        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0].confidence, "disputed")
        self.assertEqual(claims[0].score, 0.10)
        self.assertEqual(claims[0].status, "disputed")
        self.assertTrue(any("content_hash" in note for note in claims[0].notes))

    def test_aggregate_sensor_claims_cli_dry_run(self) -> None:
        write_sensor_event(
            sensor_event_from_payload(
                {
                    "event": "function_call",
                    "session_id": "sess-2",
                    "tool": {"name": "Read", "input": {"file_path": "skills/foo/SKILL.md"}},
                    "tool_call_id": "call-2",
                },
                provider="codex",
                source="transcript",
            ),
            self.log_dir,
        )

        stdout = StringIO()
        with patch(
            "sys.argv",
            [
                "skills-audit",
                "aggregate-sensor-claims",
                "--log-dir",
                str(self.log_dir),
            ],
        ), redirect_stdout(stdout):
            exit_code = main()

        self.assertEqual(exit_code, 0)
        self.assertIn("claims: 1", stdout.getvalue())
        self.assertIn("medium", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
