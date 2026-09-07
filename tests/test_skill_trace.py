"""Plugin preference, runtime evidence, and automatic preflight contracts."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from skills_auditor.cli import main
from skills_auditor.observability import SensorEvent, write_sensor_event
from skills_auditor.skill_trace import (
    ENABLE_ENV, SETTINGS_ENV, _tail_events, check_health, log_root,
    parse_time, preflight_warning, read_settings, set_enabled, settings_path,
)


class TestSkillTrace(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, self.previous)
        environment = patch.dict(os.environ, {"CODEX_HOME": str(self.root / "codex"),
                                              "CODEX_THREAD_ID": "task", "CODEX_SESSION_ID": "task"})
        environment.start()
        self.addCleanup(environment.stop)
        for key in (ENABLE_ENV, SETTINGS_ENV, "SKILLS_AUDITOR_LOG_DIR"):
            os.environ.pop(key, None)

    def cli(self, *args):
        out, err = StringIO(), StringIO()
        with patch("sys.argv", ["skills-audit", *args]), redirect_stdout(out), redirect_stderr(err):
            code = main()
        return code, out.getvalue(), err.getvalue()

    def event(self, hook="PreToolUse", **values):
        arguments = dict(provider="codex", event_type="pre_tool_use", source="hook",
                         session_id="task", cwd=str(self.root),
                         metadata={"skill_trace": 1, "hook_event_name": hook})
        arguments.update(values)
        event = SensorEvent(**arguments)
        return write_sensor_event(event, log_root(self.root))

    def pair(self, **values):
        self.event("PreToolUse", **values)
        self.event("PostToolUse", **values)

    def test_default_off_and_persistent_switch_do_not_edit_host_config_or_history(self):
        self.assertEqual(check_health()["status"], "disabled")
        self.assertFalse(settings_path().exists())
        self.assertEqual(list(self.root.iterdir()), [])
        host = self.root / "codex/config.toml"
        host.parent.mkdir()
        original = '[features]\nhooks = true\n[plugins."other"]\nenabled = true\n'
        host.write_text(original)
        log = self.event()
        before = log.read_bytes()
        set_enabled(True)
        self.assertTrue(read_settings()["enabled"])
        self.assertEqual(check_health()["status"], "stale")
        set_enabled(False)
        self.assertEqual(check_health()["status"], "disabled")
        self.assertEqual(log.read_bytes(), before)
        self.assertEqual(host.read_text(), original)

    def test_environment_override_and_custom_settings_location(self):
        os.environ[SETTINGS_ENV] = str(self.root / "private/settings.json")
        set_enabled(True)
        self.assertEqual(settings_path(), self.root / "private/settings.json")
        os.environ[ENABLE_ENV] = "0"
        self.assertFalse(read_settings()["enabled"])
        code, out, _ = self.cli("skill-trace", "enable")
        self.assertEqual(code, 0)
        self.assertIn("overrides", out)
        os.environ[ENABLE_ENV] = "1"
        self.assertTrue(read_settings()["enabled"])
        os.environ[ENABLE_ENV] = "yes"
        self.assertEqual(check_health()["status"], "error")

    def test_bad_settings_are_visible_without_blocking_audit(self):
        settings_path().parent.mkdir()
        for data in ("{invalid", "[]", '{"schema_version":1,"enabled":"false"}',
                     '{"schema_version":1,"enabled":true,"updated_at":"bad"}'):
            settings_path().write_text(data)
            self.assertEqual(check_health()["status"], "error")
            code, output, errors = self.cli("audit", "--skills-dir", str(self.root / "empty"))
            self.assertEqual(code, 0, output)
            self.assertIn("Skill Trace preflight [error]", errors)

    def test_health_requires_recent_pre_and_post_for_same_task_and_workspace(self):
        set_enabled(True)
        self.assertEqual(check_health()["status"], "unverified")
        self.event("SessionStart")
        self.assertEqual(check_health()["status"], "stale")
        self.event()
        self.assertEqual(check_health()["status"], "stale")
        self.event("PostToolUse")
        result = check_health()
        self.assertEqual(result["status"], "healthy")
        self.assertIn("SessionStart", result["observed_hooks"])
        self.assertEqual(check_health(session_id="other")["status"], "unverified")

    def test_wrong_session_cwd_source_provider_and_unmarked_events_do_not_pass(self):
        set_enabled(True)
        for values in ({"session_id": "other"}, {"cwd": str(self.root / "other")},
                       {"source": "manual"}, {"provider": "claude-code"}, {"metadata": {}},
                       {"metadata": {"skill_trace": 1, "hook_event_name": []}}, {"cwd": ""}):
            self.pair(**values)
        self.assertEqual(check_health()["status"], "unverified")

    def test_stale_future_and_pre_enable_events_do_not_pass(self):
        set_enabled(True)
        self.pair(timestamp=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat())
        self.pair(timestamp=(datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat())
        self.assertEqual(check_health()["status"], "unverified")
        self.pair(timestamp=(datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat())
        self.assertEqual(check_health()["status"], "stale")
        self.pair()
        self.assertEqual(check_health()["status"], "healthy")
        set_enabled(False)
        set_enabled(True)
        self.assertEqual(check_health()["status"], "stale")

    def test_missing_session_and_explicit_terminal_session(self):
        set_enabled(True)
        self.pair()
        os.environ.pop("CODEX_THREAD_ID")
        self.assertEqual(check_health()["status"], "healthy")
        os.environ.pop("CODEX_SESSION_ID")
        self.assertEqual(check_health()["status"], "unverified")
        self.assertEqual(check_health(session_id="task")["status"], "healthy")
        with redirect_stderr(StringIO()) as err:
            preflight_warning()
        self.assertEqual(err.getvalue(), "")

    def test_log_root_override_is_shared_with_cli_preflight(self):
        os.environ["SKILLS_AUDITOR_LOG_DIR"] = "custom/logs"
        set_enabled(True)
        self.pair()
        self.assertEqual(check_health()["status"], "healthy")
        self.assertEqual(log_root(self.root), self.root / "custom/logs")
        self.assertEqual(log_root(self.root, str(self.root / "absolute")), self.root / "absolute")
        code, _, err = self.cli("audit-sensor-logs")
        self.assertEqual(code, 0, err)
        self.assertNotIn("preflight", err)

    def test_bounded_tail_ignores_partial_invalid_and_non_object_records(self):
        path = self.root / "tail.jsonl"
        path.write_bytes(b"x" * 600 + b'\n[]\n{invalid\n\xff\n{"ok":1}\n{"partial":')
        with patch("skills_auditor.skill_trace.MAX_TAIL_BYTES", 100):
            self.assertEqual(list(_tail_events(path)), [{"ok": 1}])
        self.assertEqual(list(_tail_events(self.root / "absent")), [])
        set_enabled(True)
        with patch("skills_auditor.skill_trace._tail_events", side_effect=PermissionError("denied")):
            self.assertEqual(check_health()["status"], "error")

    def test_preflight_does_not_change_json_output_or_command_exit_code(self):
        args = ("integrate", "--source", str(self.root / "missing"), "--target", "codex", "--format", "json")
        before = self.cli(*args)
        set_enabled(True)
        after = self.cli(*args)
        self.assertEqual(before[:2], after[:2])
        json.loads(after[1])
        self.assertIn("Skill Trace preflight", after[2])
        self.assertFalse((self.root / ".skills-auditor-local/sensors").exists())

    def test_symlink_loop_in_observed_path_does_not_block_audit(self):
        set_enabled(True)
        loop = self.root / "loop"
        loop.symlink_to("loop")
        self.pair(cwd=str(loop))
        # Python 3.13+ leaves loops unresolved in non-strict mode; older
        # versions raise RuntimeError. Neither may certify healthy capture.
        self.assertIn(check_health()["status"], {"error", "unverified"})
        code, _, errors = self.cli("audit", "--skills-dir", str(self.root / "empty"))
        self.assertEqual(code, 0)
        self.assertIn("Skill Trace preflight [", errors)
        with patch("skills_auditor.skill_trace.log_root", side_effect=RuntimeError("invalid home")):
            self.assertEqual(check_health()["status"], "error")

    def test_control_exit_codes_and_json_diagnostics(self):
        for action in ("status", "check", "disable", "enable"):
            code, out, err = self.cli("skill-trace", action, "--format", "json")
            self.assertEqual(code, 0, err)
            json.loads(out)
        code, out, _ = self.cli("skill-trace", "check", "--format", "json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["status"], "unverified")
        self.pair()
        self.assertEqual(self.cli("skill-trace", "check")[0], 0)
        with patch("skills_auditor.skill_trace.set_enabled", side_effect=PermissionError("denied")):
            code, _, err = self.cli("skill-trace", "disable")
            self.assertEqual(code, 2)
            self.assertIn("denied", err)
        os.environ[ENABLE_ENV] = "bad"
        self.assertEqual(self.cli("skill-trace", "status")[0], 2)

    def test_settings_path_defaults_and_invalid_times(self):
        os.environ.pop("CODEX_HOME")
        with patch("skills_auditor.skill_trace.Path.home", return_value=self.root):
            self.assertEqual(settings_path(), self.root / ".codex/skill-trace.json")
        for value in ("", "not-a-date", "2026-09-07T00:00:00", None):
            self.assertIsNone(parse_time(value))
        self.assertIsNotNone(parse_time("2026-09-07T00:00:00Z"))


if __name__ == "__main__":
    unittest.main()
