"""Edge contracts for sensor parsing, log loading, and audit rules."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from skills_auditor.observability import (
    LogSummary,
    _command_path_candidate,
    _event_date,
    _extract_command,
    _extract_path,
    _extract_read_path_from_shell_command,
    _infer_operation,
    _infer_skill_from_path,
    aggregate_sensor_claims,
    audit_sensor_events,
    audit_trigger_logs,
    load_sensor_events,
    load_trigger_logs,
    sensor_event_from_payload,
)


class TestSensorExtractionEdges(unittest.TestCase):
    def test_recursive_path_and_command_extractors(self) -> None:
        self.assertEqual(_extract_path({"nested": [{"file_path": "/tmp/a"}]}), "/tmp/a")
        self.assertEqual(_extract_path([{"path": "relative"}]), "relative")
        self.assertEqual(_extract_path({"path": 7}), "")
        self.assertEqual(_extract_command({"nested": [{"cmd": "cat /tmp/a"}]}), "cat /tmp/a")
        self.assertEqual(_extract_command([{"command": "head /tmp/a"}]), "head /tmp/a")
        self.assertEqual(_extract_command({"cmd": 7}), "")

    def test_shell_read_extraction_is_conservative(self) -> None:
        for command in ("", "cat `pwd`/SKILL.md", "cat $(pwd)/SKILL.md", "cat 'unterminated", "cat a | wc"):
            with self.subTest(command=command):
                self.assertEqual(_extract_read_path_from_shell_command(command), ("", ""))
        self.assertEqual(_extract_read_path_from_shell_command("python SKILL.md"), ("", ""))
        self.assertEqual(_extract_read_path_from_shell_command("cat --number"), ("", ""))
        self.assertEqual(_extract_read_path_from_shell_command("sed -n 1p path/SKILL.md"), ("path/SKILL.md", "sed"))

        for token in ("", "-n", "12", ".", ".."):
            with self.subTest(token=token):
                self.assertFalse(_command_path_candidate(token))
        self.assertTrue(_command_path_candidate("skills/alpha/SKILL.md"))
        self.assertTrue(_command_path_candidate("SKILL.md"))

    def test_operation_and_skill_inference_cover_all_categories(self) -> None:
        expected = {
            "Read": "read",
            "Edit": "write",
            "List": "list",
            "Grep": "search",
            "Bash": "command",
        }
        for tool, operation in expected.items():
            with self.subTest(tool=tool):
                self.assertEqual(_infer_operation(tool, "tool_call"), operation)
        self.assertEqual(_infer_operation("Custom", "pre_tool_use"), "tool")
        self.assertEqual(_infer_operation("Custom", "unknown"), "")

        self.assertEqual(_infer_skill_from_path(""), ("", ""))
        self.assertEqual(_infer_skill_from_path("/repo/skills/alpha/SKILL.md")[0], "alpha")
        self.assertEqual(_infer_skill_from_path("/repo/skills/alpha/payload.txt")[0], "alpha")
        self.assertEqual(_infer_skill_from_path("/tmp/plain.txt"), ("", ""))

    def test_sensor_payload_resolves_relative_paths_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            skill = root / "skills" / "alpha" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("payload\n", encoding="utf-8")
            event = sensor_event_from_payload(
                {
                    "event_type": "posttooluse",
                    "cwd": str(root),
                    "tool": {"name": "Read", "input": {"file_path": "skills/alpha/SKILL.md"}},
                    "model": "probe",
                    "turn_id": 7,
                    "tool_response": {},
                },
                provider="CLAUDE_CODE",
                resolve_path=True,
                hash_path=True,
            )
            self.assertEqual(event.provider, "claude-code")
            self.assertEqual(event.event_type, "skill_file_access")
            self.assertEqual(event.realpath, str(skill.resolve()))
            self.assertTrue(event.content_hash.startswith("sha256:"))
            self.assertEqual(event.metadata["model"], "probe")
            self.assertEqual(event.metadata["turn_id"], 7)
            self.assertTrue(event.metadata["has_tool_response"])

            shell = sensor_event_from_payload(
                {"tool_name": "exec_command", "tool_input": {"command": "nl skills/alpha/SKILL.md"}},
                provider="codex",
            )
            self.assertEqual(shell.operation, "read")
            self.assertEqual(shell.metadata["command_verb"], "nl")


class TestLogLoadingAndAuditEdges(unittest.TestCase):
    def write_jsonl(self, path: Path, lines: list[str]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_loaders_reject_malformed_and_non_object_records(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            self.assertEqual(load_trigger_logs(root / "missing"), ([], []))
            self.assertEqual(load_sensor_events(root / "missing"), ([], []))
            trigger_path = root / "logs" / "2026-01-01" / "skill_trigger.jsonl"
            sensor_path = root / "sensors" / "2026-01-01" / "codex.jsonl"
            lines = ["", "{", "[]", json.dumps({"event_id": "ok"})]
            self.write_jsonl(trigger_path, lines)
            self.write_jsonl(sensor_path, lines)

            trigger_events, trigger_findings = load_trigger_logs(root)
            sensor_events, sensor_findings = load_sensor_events(root)
            self.assertEqual([event["event_id"] for event in trigger_events], ["ok"])
            self.assertEqual([event["event_id"] for event in sensor_events], ["ok"])
            self.assertEqual(
                {finding.check for finding in trigger_findings},
                {"invalid_json", "invalid_event"},
            )
            self.assertEqual(
                {finding.check for finding in sensor_findings},
                {"invalid_json", "invalid_event"},
            )
            self.assertEqual(load_trigger_logs(root, kind="trace"), ([], []))
            self.assertEqual(load_sensor_events(root, provider="claude-code"), ([], []))

    def test_sensor_and_trigger_audits_report_all_rule_families(self) -> None:
        sensor = {
            "event_id": "duplicate",
            "provider": "",
            "event_type": "mystery",
            "raw_prompt": "secret",
        }
        access = {
            "event_id": "duplicate",
            "provider": "codex",
            "event_type": "file_access",
            "path": "",
        }
        sensor_checks = {finding.check for finding in audit_sensor_events([sensor, access])}
        self.assertEqual(
            sensor_checks,
            {
                "duplicate_event_id",
                "missing_provider",
                "unknown_sensor_event_type",
                "missing_access_path",
                "raw_prompt_present",
            },
        )

        trigger = {
            "event_id": "duplicate",
            "kind": "mystery",
            "verdict": "surprising",
            "raw_prompt": "secret",
        }
        skill = {"event_id": "duplicate", "kind": "skill-trigger", "verdict": "unknown"}
        trace = {"event_id": "trace", "kind": "trace", "verdict": "unknown"}
        checks = {finding.check for finding in audit_trigger_logs([trigger, skill, trace])}
        self.assertEqual(
            checks,
            {
                "duplicate_event_id",
                "unknown_kind",
                "unknown_verdict",
                "raw_prompt_present",
                "missing_prompt_reference",
                "missing_skill_reference",
                "missing_trace_path",
            },
        )

    def test_summary_and_manual_claim_confidence_edges(self) -> None:
        summary = LogSummary(0, {}, {}, 0, 0, 0, 0)
        self.assertIsNone(summary.accuracy)
        self.assertRegex(_event_date("short"), r"^\d{4}-\d{2}-\d{2}$")
        claims = aggregate_sensor_claims(
            [
                {
                    "event_type": "user_prompt",
                    "source": "hook",
                },
                {
                    "event_type": "file_access",
                    "provider": "generic",
                    "source": "manual",
                    "path": "/tmp/file",
                    "operation": "read",
                },
            ]
        )
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0].confidence, "manual")


if __name__ == "__main__":
    unittest.main()
