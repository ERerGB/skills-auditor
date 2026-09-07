"""Optional capture diagnostics never become managed Skill authorization."""

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from skills_auditor import cli, skill_trace
from skills_auditor.lifecycle import cli as lifecycle_cli
from skills_auditor.lifecycle.engine import Manager
from skills_auditor.observability import SensorEvent, write_sensor_event


CHECKOUT = Path(__file__).resolve().parents[1]


class TestLifecycleTraceCli(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="lifecycle-trace-cli-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.project = self.root / "managed project A"
        self.task = self.root / "task B"
        self.project.mkdir()
        self.task.mkdir()
        self.source = self.project / "candidate"
        self.source.mkdir()
        (self.source / "SKILL.md").write_text("# Managed fixture\n")
        self.target = self.project / "host entry"
        self.manager = Manager(self.project)
        self.addCleanup(self.manager.repository.close)
        plan = self.manager.plan("install", source=self.source, target=self.target)
        self.receipt = self.manager.apply(plan, approve_plan_id=plan["plan_id"])
        self.identifier = self.receipt["installation_id"]
        self.manager.verify(self.identifier)
        previous = Path.cwd()
        os.chdir(self.task)
        self.addCleanup(os.chdir, previous)
        environment = patch.dict(os.environ, {
            "CODEX_THREAD_ID": "trace-fixture-task", "CODEX_SESSION_ID": "trace-fixture-task",
            skill_trace.SETTINGS_ENV: str(self.root / "private settings.json"),
            skill_trace.ENABLE_ENV: "0", "SKILLS_AUDITOR_LOG_DIR": "capture",
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(CHECKOUT),
        })
        environment.start()
        self.addCleanup(environment.stop)

    def run_cli(self, *arguments, direct=False):
        output, errors = StringIO(), StringIO()
        argv = ["--project-root", str(self.project), "--format", "json", *map(str, arguments)]
        with redirect_stdout(output), redirect_stderr(errors):
            if direct:
                code = lifecycle_cli.main(argv)
            else:
                with patch("sys.argv", ["skills-audit", "lifecycle", *argv]):
                    code = cli.main()
        return code, json.loads(output.getvalue()), errors.getvalue()

    def capture_state(self, state):
        os.environ[skill_trace.ENABLE_ENV] = "0" if state == "disabled" else "invalid" if state == "error" else "1"
        os.environ["SKILLS_AUDITOR_LOG_DIR"] = "capture-" + state
        if state in {"healthy", "stale"}:
            stamp = datetime.now(timezone.utc) - timedelta(minutes=20 if state == "stale" else 0)
            for hook in ("PreToolUse", "PostToolUse"):
                write_sensor_event(SensorEvent(provider="codex", event_type="pre_tool_use", source="hook",
                    session_id="trace-fixture-task", cwd=str(self.task), timestamp=stamp.isoformat(),
                    metadata={"skill_trace": 1, "hook_event_name": hook}), skill_trace.log_root(self.task))
        self.assertEqual(skill_trace.check_health()["status"], state)

    def unchanged_inputs(self):
        files = {str(path.relative_to(self.task)): path.read_bytes() for path in self.task.rglob("*") if path.is_file()}
        settings = Path(os.environ[skill_trace.SETTINGS_ENV])
        return {"task_files": files, "settings": settings.read_bytes() if settings.exists() else None,
                "source": (self.source / "SKILL.md").read_bytes(), "pointer": (os.readlink(self.target), self.target.lstat().st_ino),
                "installation": self.manager.get_installation(self.identifier),
                "authorization": self.manager.repository.list("authorization"),
                "grant": self.manager.repository.list("grant"), "receipt": self.manager.repository.list("receipt")}

    def test_capture_health_is_warning_only_for_valid_and_revoked_authorization(self):
        for denied in (False, True):
            if denied:
                plan = self.manager.plan("revoke", installation_id=self.identifier)
                self.manager.apply(plan, approve_plan_id=plan["plan_id"])
            for state in ("disabled", "healthy", "unverified", "stale", "error"):
                self.capture_state(state)
                before = self.unchanged_inputs()
                for direct in (False, True):
                    with self.subTest(denied=denied, state=state, direct=direct), \
                            patch.object(skill_trace, "preflight_warning", wraps=skill_trace.preflight_warning) as warning:
                        code, result, stderr = self.run_cli("status", self.identifier, direct=direct)
                        warning.assert_called_once_with(None)
                        self.assertEqual(code, 3 if denied else 0)
                        self.assertEqual(result["authorization_state"], "revoked" if denied else "valid")
                        self.assertEqual(result["grant_id"], self.receipt["grant_id"])
                        self.assertEqual(stderr.count("Skill Trace preflight ["), 0 if state in {"healthy", "disabled"} else 1)
                        self.assertEqual(self.unchanged_inputs(), before)
                        self.assertEqual(self.manager.repository.list("capture-evidence"), [])
                        self.assertFalse((self.task / ".skills-auditor-local/lifecycle").exists())

    def test_module_and_legacy_launcher_aliases_emit_the_same_advisory(self):
        self.capture_state("unverified")
        before = self.unchanged_inputs()
        aliases = ([sys.executable, "-m", "skills_auditor"], [sys.executable, str(CHECKOUT / "scripts/skills_audit.py")],
                   [sys.executable, "-m", "skills_auditor.cli"])
        for alias in aliases:
            with self.subTest(alias=alias):
                completed = subprocess.run([*alias, "lifecycle", "--project-root", str(self.project), "--format", "json", "status", self.identifier],
                                           cwd=self.task, capture_output=True, text=True, timeout=30)
                self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
                self.assertEqual(json.loads(completed.stdout)["authorization_state"], "valid")
                self.assertEqual(completed.stderr.count("Skill Trace preflight [unverified]"), 1)
                self.assertEqual(self.unchanged_inputs(), before)

    def test_help_and_parser_errors_do_not_run_preflight(self):
        self.capture_state("unverified")
        for direct in (False, True):
            for arguments in (("--help",), ("not-a-command",), ("status",), ("list", "--not-an-option")):
                with self.subTest(direct=direct, arguments=arguments), patch.object(skill_trace, "preflight_warning") as warning:
                    output, errors = StringIO(), StringIO()
                    with redirect_stdout(output), redirect_stderr(errors):
                        try:
                            if direct:
                                code = lifecycle_cli.main(list(arguments))
                            else:
                                with patch("sys.argv", ["skills-audit", "lifecycle", *arguments]):
                                    code = cli.main()
                        except SystemExit as error:
                            code = error.code
                    self.assertEqual(code, 0 if arguments == ("--help",) else 2)
                    warning.assert_not_called()
                    self.assertNotIn("Skill Trace preflight", errors.getvalue())

    def test_capture_evidence_and_inspection_parsers_are_explicit_existing_repository_commands(self):
        parser = lifecycle_cli.configure(lifecycle_cli._Parser())
        parsed = parser.parse_args(["--project-root", str(self.project), "capture-evidence", "--evidence-id", "health-one",
                                    "--log-dir", "capture", "--actor", "local-human", "--tool", "diagnostic"])
        self.assertEqual((parsed.evidence_id, parsed.log_dir, parsed.actor, parsed.tool),
                         ("health-one", "capture", "local-human", "diagnostic"))
        inspected = parser.parse_args(["inspect", "capture-evidence", "health-one"])
        self.assertEqual((inspected.kind, inspected.identifier), ("capture-evidence", "health-one"))

    def test_preference_sources_and_no_codex_task_preserve_text_and_json_results(self):
        settings = Path(os.environ[skill_trace.SETTINGS_ENV])
        for preference, override, expected_source, state in (
                (None, None, "default", "disabled"), (False, None, "file", "disabled"),
                (True, None, "file", "unverified"), (True, "0", "environment", "disabled"),
                (False, "1", "environment", "unverified")):
            if preference is not None:
                settings.write_text(json.dumps({"schema_version": 1, "enabled": preference,
                    "updated_at": datetime.now(timezone.utc).isoformat()}))
            if override is None:
                os.environ.pop(skill_trace.ENABLE_ENV, None)
            else:
                os.environ[skill_trace.ENABLE_ENV] = override
            self.assertEqual(skill_trace.check_health()["source"], expected_source)
            before = self.unchanged_inputs()
            for denied in (False, True):
                if denied:
                    revoke = self.manager.plan("revoke", installation_id=self.identifier)
                    self.manager.apply(revoke, approve_plan_id=revoke["plan_id"])
                    before = self.unchanged_inputs()
                for output_format in ("text", "json"):
                    with self.subTest(preference=preference, override=override, denied=denied, format=output_format):
                        output, errors = StringIO(), StringIO()
                        with redirect_stdout(output), redirect_stderr(errors):
                            code = lifecycle_cli.main(["--project-root", str(self.project), "--format", output_format,
                                                       "status", self.identifier])
                        self.assertEqual(code, 3 if denied else 0)
                        if output_format == "json":
                            self.assertEqual(json.loads(output.getvalue())["authorization_state"], "revoked" if denied else "valid")
                        else:
                            self.assertIn("[BLOCK]" if denied else "[OK]", output.getvalue())
                        self.assertEqual(errors.getvalue().count("Skill Trace preflight ["), int(state == "unverified"))
                        self.assertEqual(self.unchanged_inputs(), before)
                if denied:
                    renewal = self.manager.plan("renew", installation_id=self.identifier)
                    self.manager.apply(renewal, approve_plan_id=renewal["plan_id"])
                    self.manager.verify(self.identifier)
        os.environ.pop("CODEX_THREAD_ID", None)
        os.environ.pop("CODEX_SESSION_ID", None)
        self.assertEqual(self.run_cli("status", self.identifier)[2], "")

    def test_explicit_capture_persists_scoped_snapshot_and_retry_never_replaces_it(self):
        self.capture_state("healthy")
        before = self.unchanged_inputs()
        with patch.object(skill_trace, "preflight_warning", wraps=skill_trace.preflight_warning) as warning:
            code, evidence, stderr = self.run_cli("capture-evidence", "--evidence-id", "health-one", "--log-dir", "capture-healthy",
                                                 "--actor", "local-human", "--tool", "diagnostic")
        warning.assert_called_once_with("capture-healthy")
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(evidence["schema_version"], "skills-auditor-lifecycle-capture-evidence/v1")
        self.assertEqual((evidence["project_root"], evidence["task_cwd"], evidence["log_root"]),
                         (str(self.project), str(self.task), str(self.task / "capture-healthy")))
        self.assertEqual(evidence["health"]["status"], "healthy")
        self.assertEqual(evidence["host_trust"], "unverified")
        self.assertEqual((evidence["actor"], evidence["tool"]), ("local-human", "diagnostic"))
        self.assertEqual(self.unchanged_inputs(), before)
        os.environ[skill_trace.ENABLE_ENV] = "0"
        retried = self.run_cli("capture-evidence", "--evidence-id", "health-one", "--log-dir", "capture-healthy",
                               "--actor", "local-human", "--tool", "diagnostic", direct=True)
        self.assertEqual(retried, (0, evidence, ""))
        self.assertEqual(self.run_cli("inspect", "capture-evidence", "health-one"), (0, evidence, ""))
        self.assertEqual(len(self.manager.repository.list("capture-evidence")), 1)
        self.assertEqual(self.unchanged_inputs(), before)
        # The advisory describes now; an exact retry returns the original
        # diagnostic receipt, even when the new advisory differs from it.
        os.environ[skill_trace.ENABLE_ENV] = "invalid"
        code, retried, stderr = self.run_cli("capture-evidence", "--evidence-id", "health-one", "--log-dir", "capture-healthy",
                                            "--actor", "local-human", "--tool", "diagnostic")
        self.assertEqual((code, retried), (0, evidence))
        self.assertEqual(stderr.count("Skill Trace preflight [error]"), 1)
        code, unhealthy, stderr = self.run_cli("capture-evidence", "--evidence-id", "health-error")
        self.assertEqual(code, 0, unhealthy)
        self.assertEqual(unhealthy["health"]["status"], "error")
        self.assertEqual(stderr.count("Skill Trace preflight [error]"), 1)
        self.assertNotIn("detail", unhealthy)
        self.assertNotIn(skill_trace.ENABLE_ENV, json.dumps(unhealthy))
        self.assertEqual(self.unchanged_inputs(), before)
        code, error, _ = self.run_cli("capture-evidence", "--evidence-id", "health-one", "--log-dir", "different")
        self.assertEqual((code, error["code"]), (3, "capture_evidence_conflict"))

    def test_capture_uses_default_project_and_refuses_unknown_or_corrupt_evidence(self):
        os.chdir(self.project)
        output = StringIO()
        with redirect_stdout(output):
            code = lifecycle_cli.main(["--format", "json", "capture-evidence", "--evidence-id", "default-owner"])
        self.assertEqual(code, 0)
        evidence = json.loads(output.getvalue())
        self.assertEqual((evidence["project_root"], evidence["task_cwd"]), (str(self.project), str(self.project)))
        self.assertEqual((evidence["actor"], evidence["tool"]), ("local-observer", "lifecycle"))
        code, error, _ = self.run_cli("inspect", "capture-evidence", "missing")
        self.assertEqual((code, error["code"]), (3, "capture_evidence_missing"))
        self.manager.repository.put("capture-evidence", "forged", {"evidence_id": "forged"})
        code, error, _ = self.run_cli("inspect", "capture-evidence", "forged")
        self.assertEqual((code, error["code"]), (3, "capture_evidence_invalid"))
        missing = self.root / "uninitialized owner"
        missing.mkdir()
        output = StringIO()
        with redirect_stdout(output):
            code = lifecycle_cli.main(["--project-root", str(missing), "--format", "json", "capture-evidence"])
        self.assertEqual(code, 3)
        self.assertIs(json.loads(output.getvalue())["context_verified"], False)
        self.assertFalse((missing / ".skills-auditor-local").exists())

    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX FIFO regression")
    def test_fifo_sensor_and_settings_never_block_the_ordinary_lifecycle_command(self):
        for denied in (False, True):
            if denied:
                plan = self.manager.plan("revoke", installation_id=self.identifier)
                self.manager.apply(plan, approve_plan_id=plan["plan_id"])
            for kind in ("sensor", "settings"):
                with self.subTest(kind=kind, denied=denied):
                    os.environ[skill_trace.ENABLE_ENV] = "1"
                    if kind == "settings":
                        os.environ.pop(skill_trace.ENABLE_ENV)
                        path = Path(os.environ[skill_trace.SETTINGS_ENV])
                    else:
                        path = self.task / "capture/sensors" / datetime.now(timezone.utc).strftime("%Y-%m-%d") / "codex.jsonl"
                        path.parent.mkdir(parents=True, exist_ok=True)
                    before = self.unchanged_inputs()
                    os.mkfifo(path)
                    try:
                        try:
                            completed = subprocess.run([sys.executable, "-m", "skills_auditor", "lifecycle", "--project-root", str(self.project),
                                                        "--format", "json", "status", self.identifier],
                                                       cwd=self.task, capture_output=True, text=True, timeout=3)
                        except subprocess.TimeoutExpired:
                            self.fail("Optional {} FIFO blocked an ordinary lifecycle command".format(kind))
                        self.assertEqual(completed.returncode, 3 if denied else 0, completed.stdout + completed.stderr)
                        self.assertEqual(json.loads(completed.stdout)["authorization_state"], "revoked" if denied else "valid")
                        self.assertEqual(completed.stderr.count("Skill Trace preflight [error]"), 1)
                        self.assertEqual(self.manager.repository.list("capture-evidence"), [])
                    finally:
                        path.unlink()
                    self.assertEqual(self.unchanged_inputs(), before)


if __name__ == "__main__":
    unittest.main()
