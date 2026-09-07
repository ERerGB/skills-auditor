"""Plugin preference, runtime evidence, and automatic preflight contracts."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from skills_auditor import skill_trace
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

    def test_settings_size_limit_preserves_regular_symlinks_and_rejects_oversize(self):
        set_enabled(True)
        actual = settings_path()
        payload = actual.read_bytes()
        actual.write_bytes(payload + b" " * (64 * 1024 - len(payload)))
        alias = self.root / "settings alias.json"
        alias.symlink_to(actual)
        os.environ[SETTINGS_ENV] = str(alias)
        self.assertTrue(read_settings()["enabled"])
        self.assertEqual(read_settings()["source"], "file")
        actual.write_bytes(actual.read_bytes() + b" ")
        before = actual.read_bytes()
        with patch.object(skill_trace.os, "read", wraps=os.read) as reader:
            self.assertEqual(check_health()["status"], "error")
            reader.assert_not_called()
        code, output, _ = self.cli("skill-trace", "check", "--format", "json")
        self.assertEqual((code, json.loads(output)["status"]), (2, "error"))
        code, _, error = self.cli("audit", "--skills-dir", str(self.root / "empty"))
        self.assertEqual(code, 0)
        self.assertEqual(error.count("Skill Trace preflight [error]"), 1)
        self.assertEqual(actual.read_bytes(), before)
        self.assertEqual(alias.readlink(), actual)

    def test_sensor_symlink_to_regular_file_preserves_health(self):
        set_enabled(True)
        self.pair()
        path = self.event("SessionStart")
        actual = path.with_suffix(".saved")
        path.rename(actual)
        path.symlink_to(actual)
        before = actual.read_bytes()
        self.assertEqual(check_health()["status"], "healthy")
        self.assertEqual(actual.read_bytes(), before)
        self.assertEqual(path.readlink(), actual)

    def test_nonregular_descriptor_is_rejected_before_read_and_closed(self):
        set_enabled(True)
        path = self.root / "sensor.jsonl"
        path.write_text('{"ok":1}\n')
        real_open, real_close = os.open, os.close
        for loader in (read_settings, lambda: list(_tail_events(path))):
            with self.subTest(loader=loader):
                opened = []

                def track_open(*args, **kwargs):
                    descriptor = real_open(*args, **kwargs)
                    opened.append(descriptor)
                    return descriptor

                # Inspect the opened object, not an earlier path stat: the path
                # may have been replaced between the caller's check and open.
                with patch.object(skill_trace.os, "open", side_effect=track_open) as opener, \
                        patch.object(skill_trace.os, "fstat", return_value=SimpleNamespace(st_mode=stat.S_IFIFO, st_size=0)), \
                        patch.object(skill_trace.os, "read") as reader, \
                        patch.object(skill_trace.os, "close", wraps=real_close) as closer:
                    with self.assertRaisesRegex(ValueError, "regular file"):
                        loader()
                    opener.assert_called_once()
                    if hasattr(os, "O_NONBLOCK"):
                        self.assertTrue(opener.call_args.args[1] & os.O_NONBLOCK)
                    reader.assert_not_called()
                self.assertEqual(len(opened), 1)
                closer.assert_called_once_with(opened[0])
                with self.assertRaises(OSError):
                    os.fstat(opened[0])

    def test_regular_read_closes_descriptor_on_fstat_seek_and_read_failures(self):
        set_enabled(True)
        path = self.root / "sensor.jsonl"
        path.write_bytes(b"x" * (skill_trace.MAX_TAIL_BYTES + 1))
        real_open, real_close = os.open, os.close
        for kind, loader in (("settings", read_settings), ("sensor", lambda: list(_tail_events(path)))):
            faults = [("fstat", OSError), ("read", OSError), ("read", ValueError), ("read", FileNotFoundError)]
            if kind == "sensor":
                faults.append(("lseek", OSError))
            for operation, error_type in faults:
                with self.subTest(kind=kind, operation=operation, error_type=error_type):
                    opened = []

                    def track_open(*args, **kwargs):
                        descriptor = real_open(*args, **kwargs)
                        opened.append(descriptor)
                        return descriptor

                    with patch.object(skill_trace.os, "open", side_effect=track_open), \
                            patch.object(skill_trace.os, operation, side_effect=error_type("injected read failure")), \
                            patch.object(skill_trace.os, "close", wraps=real_close) as closer:
                        with self.assertRaisesRegex(error_type, "injected read failure"):
                            loader()
                    self.assertEqual(len(opened), 1)
                    closer.assert_called_once_with(opened[0])
                    with self.assertRaises(OSError):
                        os.fstat(opened[0])

    def test_open_failure_does_not_close_an_unowned_descriptor(self):
        for loader in (read_settings, lambda: list(_tail_events(self.root / "sensor.jsonl"))):
            with self.subTest(loader=loader), \
                    patch.object(skill_trace.os, "open", side_effect=PermissionError("injected open failure")), \
                    patch.object(skill_trace.os, "close") as closer:
                with self.assertRaisesRegex(PermissionError, "injected open failure"):
                    loader()
                closer.assert_not_called()

    def test_settings_growth_after_fstat_is_rejected_with_bounded_read(self):
        set_enabled(True)
        path = settings_path()
        payload = path.read_bytes()
        path.write_bytes(payload + b" " * (64 * 1024 + 1 - len(payload)))
        metadata = SimpleNamespace(st_mode=stat.S_IFREG, st_size=64 * 1024)
        with patch.object(skill_trace.os, "fstat", return_value=metadata), \
                patch.object(skill_trace.os, "read", wraps=os.read) as reader:
            self.assertEqual(check_health()["status"], "error")
            reader.assert_called_once()
            self.assertEqual(reader.call_args.args[1], 64 * 1024 + 1)

    def test_tail_read_and_partial_line_discard_are_bounded(self):
        path = self.root / "sensor.jsonl"
        path.write_bytes(b"x" * (skill_trace.MAX_TAIL_BYTES + 1))
        with patch.object(skill_trace.os, "read", wraps=os.read) as reader:
            self.assertEqual(list(_tail_events(path)), [])
            reader.assert_called_once()
            self.assertEqual(reader.call_args.args[1], skill_trace.MAX_TAIL_BYTES)

    def test_short_regular_reads_are_completed_and_descriptor_closed_on_success(self):
        set_enabled(True)
        path = self.root / "sensor.jsonl"
        path.write_bytes(b'{"ok":1}\n{"ok":2}\n')
        real_read, real_open, real_close = os.read, os.open, os.close
        for kind, loader in (("settings", read_settings), ("sensor", lambda: list(_tail_events(path)))):
            with self.subTest(kind=kind):
                opened = []

                def track_open(*args, **kwargs):
                    descriptor = real_open(*args, **kwargs)
                    opened.append(descriptor)
                    return descriptor

                with patch.object(skill_trace.os, "open", side_effect=track_open), \
                        patch.object(skill_trace.os, "read", side_effect=lambda fd, size: real_read(fd, min(size, 7))) as reader, \
                        patch.object(skill_trace.os, "close", wraps=real_close) as closer:
                    result = loader()
                    self.assertGreater(reader.call_count, 1)
                if kind == "settings":
                    self.assertTrue(result["enabled"])
                else:
                    self.assertEqual(result, [{"ok": 1}, {"ok": 2}])
                self.assertEqual(len(opened), 1)
                closer.assert_called_once_with(opened[0])
                with self.assertRaises(OSError):
                    os.fstat(opened[0])

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
