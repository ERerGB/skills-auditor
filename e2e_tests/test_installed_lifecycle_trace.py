"""Skill Trace compatibility through the installed CLI in temporary projects.

The synthetic sensor records below are test fixtures, not hooks executed by the
host. No runtime package is imported from the source checkout.
"""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest

from lifecycle_model import ModelOracle, normalized_tree, tree_state


CLI = Path(os.environ["SKILLS_AUDITOR_CLI"])
PYTHON = os.environ["SKILLS_AUDITOR_PYTHON"]


class TestInstalledLifecycleTrace(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="installed-lifecycle-trace-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.project = self.root / "managed project A"
        self.task = self.root / "task B"
        self.project.mkdir()
        self.task.mkdir()
        self.source = self.root / "candidate"
        self.source.mkdir()
        (self.source / "SKILL.md").write_text("# Installed capture fixture\n")
        (self.source / "payload").write_bytes(b"H1")
        self.target = self.project / "host entry"
        self.settings = self.root / "private-settings.json"
        self.environment = dict(os.environ)
        self.environment.pop("PYTHONPATH", None)
        self.environment.update({
            "SKILLS_AUDITOR_SKILL_TRACE_CONFIG": str(self.settings),
            "SKILLS_AUDITOR_SKILL_TRACE": "0", "SKILLS_AUDITOR_LOG_DIR": "capture",
            "CODEX_THREAD_ID": "installed-trace-task", "CODEX_SESSION_ID": "installed-trace-task",
        })
        self.oracle = ModelOracle(self, SimpleNamespace(project_root=self.project))
        self.oracle.watch_tree(self.source, tree_state(self.source))
        self.oracle.watch_pointer(self.target, None)
        path = self.project / "install.json"
        plan = self.cli("plan", "install", "--source", self.source, "--target", self.target, "--plan-out", path)
        self.oracle.watch_tree(plan["version"]["snapshot"]["path"], normalized_tree(tree_state(self.source)))
        self.oracle.watch_pointer(self.target, plan["version"]["snapshot"]["path"])
        self.receipt = self.cli("apply", path, "--approve-plan-id", plan["plan_id"])
        self.identifier = self.receipt["installation_id"]
        self.cli("verify", self.identifier)

    def cli(self, *arguments, expected=0, warning=None, alias="console"):
        arguments = ["--project-root", str(self.project), "--format", "json", *map(str, arguments)]
        if alias == "direct":
            command = [PYTHON, "-I", "-c", "import sys; from skills_auditor.lifecycle.cli import main; sys.exit(main(sys.argv[1:]))", *arguments]
        else:
            prefix = [str(CLI)] if alias == "console" else [PYTHON, "-I", "-m", "skills_auditor"]
            command = [*prefix, "lifecycle", *arguments]
        completed = subprocess.run(command, cwd=self.task, env=self.environment,
                                   capture_output=True, text=True, timeout=30)
        self.assertEqual(completed.returncode, expected, completed.stdout + completed.stderr)
        self.assertEqual(completed.stderr.count("Skill Trace preflight ["), int(warning is not None), completed.stderr)
        if warning is not None:
            self.assertIn("Skill Trace preflight [" + warning + "]", completed.stderr)
        else:
            self.assertEqual(completed.stderr, "")
        if self.oracle.database.exists():
            self.oracle.check()
        self.assertFalse((self.task / ".skills-auditor-local/lifecycle").exists())
        return json.loads(completed.stdout)

    def capture_state(self, state):
        self.environment["SKILLS_AUDITOR_SKILL_TRACE"] = "0" if state == "disabled" else "bad" if state == "error" else "1"
        self.environment["SKILLS_AUDITOR_LOG_DIR"] = "capture-" + state
        if state in {"healthy", "stale"}:
            stamp = datetime.now(timezone.utc) - timedelta(minutes=20 if state == "stale" else 0)
            path = self.task / ("capture-" + state) / "sensors" / stamp.strftime("%Y-%m-%d") / "codex.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            # Exact allowlisted health metadata plus private fixture fields that
            # must never enter a persisted diagnostic or investigation packet.
            events = [{"provider": "codex", "event_type": "pre_tool_use", "source": "hook",
                       "session_id": "installed-trace-task", "cwd": str(self.task), "timestamp": stamp.isoformat(),
                       "metadata": {"skill_trace": 1, "hook_event_name": hook},
                       "prompt": "PRIVATE_FIXTURE_PROMPT", "tool_output": "PRIVATE_FIXTURE_OUTPUT"}
                      for hook in ("PreToolUse", "PostToolUse")]
            path.write_text("".join(json.dumps(event) + "\n" for event in events))

    def protected_state(self):
        records = self.oracle.check()["records"]
        return {"authority": {kind: records.get(kind, {}) for kind in
                ("installation", "authorization", "grant", "receipt", "version", "transaction", "verification", "latest-verification", "verification-run")},
                "task_files": {str(path.relative_to(self.task)): path.read_bytes() for path in self.task.rglob("*") if path.is_file()},
                "settings": self.settings.read_bytes() if self.settings.exists() else None,
                "pointer": (os.readlink(self.target), self.target.lstat().st_ino) if self.target.is_symlink() else None}

    def test_installed_all_lifecycle_entrypoints_keep_capture_advisory(self):
        for denied in (False, True):
            if denied:
                self.capture_state("disabled")
                path = self.project / "revoke.json"
                plan = self.cli("plan", "revoke", "--installation-id", self.identifier, "--plan-out", path)
                self.cli("apply", path, "--approve-plan-id", plan["plan_id"])
            for state in ("disabled", "healthy", "unverified", "stale", "error"):
                self.capture_state(state)
                before = self.protected_state()
                for alias in ("console", "module", "direct"):
                    with self.subTest(denied=denied, state=state, alias=alias):
                        result = self.cli("status", self.identifier, expected=3 if denied else 0,
                                          warning=None if state in {"disabled", "healthy"} else state, alias=alias)
                        self.assertEqual(result["authorization_state"], "revoked" if denied else "valid")
                        self.assertEqual(result["grant_id"], self.receipt["grant_id"])
                        self.assertEqual(self.protected_state(), before)
                        self.assertFalse(self.oracle.check()["records"].get("capture-evidence"))

    def test_installed_capture_snapshot_retry_and_incident_reference_never_authorize(self):
        self.target.unlink()
        self.oracle.watch_pointer(self.target, None)
        failed = self.cli("verify", self.identifier, expected=3)
        self.assertEqual(failed["approval"]["state"], "invalidated")
        incident = self.cli("incidents")["incidents"][0]["incident_id"]
        self.capture_state("healthy")
        before = self.protected_state()
        options = ("capture-evidence", "--evidence-id", "health-fixture", "--log-dir", "capture-healthy", "--actor", "local-reviewer", "--tool", "installed-test")
        evidence = self.cli(*options)
        self.assertEqual(evidence["schema_version"], "skills-auditor-lifecycle-capture-evidence/v1")
        self.assertEqual((evidence["project_root"], evidence["task_cwd"], evidence["log_root"]),
                         (str(self.project), str(self.task), str(self.task / "capture-healthy")))
        self.assertEqual((evidence["health"]["status"], evidence["host_trust"]), ("healthy", "unverified"))
        self.assertEqual(self.protected_state(), before)
        note = self.cli("append-note", incident, "--text", "Optional capture is separate from managed integrity.",
                        "--event-id", "capture-note", "--actor", "local-reviewer", "--tool", "installed-test",
                        "--evidence-ref", "capture-evidence:health-fixture")
        self.assertEqual(note["payload"]["evidence_refs"], [{"kind": "capture-evidence", "id": "health-fixture"}])
        packet = self.cli("investigate", incident)
        self.assertEqual(packet["capture_evidence"], [evidence])
        self.assertEqual(packet["incident"]["state"], "investigating")
        self.assertIsNone(packet["incident"]["resolution"])
        self.assertNotIn("PRIVATE_FIXTURE", json.dumps(packet))
        self.assertEqual(self.protected_state(), before)
        self.cli("status", self.identifier, expected=3)
        self.environment["SKILLS_AUDITOR_SKILL_TRACE"] = "bad"
        self.assertEqual(self.cli(*options, warning="error"), evidence)
        self.assertEqual(self.cli("inspect", "capture-evidence", "health-fixture", warning="error"), evidence)
        unhealthy = self.cli("capture-evidence", "--evidence-id", "health-error", warning="error")
        self.assertEqual(unhealthy["health"]["status"], "error")
        self.assertNotIn("detail", unhealthy)
        self.assertNotIn("SKILLS_AUDITOR_SKILL_TRACE must", json.dumps(unhealthy))
        self.assertEqual(self.protected_state(), before)
        self.cli("status", self.identifier, expected=3, warning="error")
        self.assertEqual(self.cli("inspect", "receipt", self.receipt["receipt_id"], warning="error"), self.receipt)


if __name__ == "__main__":
    unittest.main()
