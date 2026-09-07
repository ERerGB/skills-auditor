"""Tests for the repo-local skill-trace Codex plugin."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "skill-trace"
MARKETPLACE_PATH = REPO_ROOT / ".agents" / "plugins" / "marketplace.json"


class TestSensorPlugin(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.codex_home = Path(temporary.name) / ".codex"
        environment = patch.dict(os.environ, {"CODEX_HOME": str(self.codex_home), "SKILLS_AUDITOR_SKILL_TRACE": "1"})
        environment.start()
        self.addCleanup(environment.stop)

    def test_codex_manifest_is_repo_local_plugin(self) -> None:
        manifest_path = PLUGIN_ROOT / ".codex-plugin" / "plugin.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(manifest["name"], "skill-trace")
        self.assertEqual(manifest["skills"], "./skills/")
        self.assertNotIn("hooks", manifest)
        self.assertEqual(manifest["interface"]["displayName"], "Skill Trace")
        self.assertIn("Observability", manifest["keywords"])

    def test_hook_config_registers_tool_and_session_events(self) -> None:
        hooks_path = PLUGIN_ROOT / "hooks" / "hooks.json"
        hooks_config = json.loads(hooks_path.read_text(encoding="utf-8"))
        hooks = hooks_config["hooks"]

        for event_name in ("SessionStart", "PreToolUse", "PostToolUse", "Stop"):
            self.assertIn(event_name, hooks)
            encoded = json.dumps(hooks[event_name])
            self.assertIn("${PLUGIN_ROOT}/scripts/sensor_hook.py", encoded)
            self.assertIn("--provider codex", encoded)
            self.assertNotIn("/Users/", encoded)
        self.assertNotIn("SessionEnd", hooks)

    def test_repo_marketplace_points_at_plugin(self) -> None:
        marketplace = json.loads(MARKETPLACE_PATH.read_text(encoding="utf-8"))

        self.assertEqual(marketplace["name"], "skills-auditor-local")
        entry = next(p for p in marketplace["plugins"] if p["name"] == "skill-trace")
        self.assertEqual(entry["source"], {"source": "local", "path": "./plugins/skill-trace"})
        self.assertEqual(entry["policy"]["installation"], "AVAILABLE")
        self.assertEqual(entry["policy"]["authentication"], "ON_INSTALL")

    def test_sensor_hook_dry_run_normalizes_skill_read(self) -> None:
        payload = {
            "hook_event_name": "PreToolUse",
            "session_id": "sess-plugin",
            "cwd": str(REPO_ROOT),
            "tool_name": "Read",
            "tool_input": {"file_path": "/Users/me/.codex/skills/foo/SKILL.md"},
        }
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run(
                [
                    sys.executable,
                    str(PLUGIN_ROOT / "scripts" / "sensor_hook.py"),
                    "--provider",
                    "codex",
                    "--log-dir",
                    str(Path(tmp) / ".skills-auditor-local"),
                    "--dry-run",
                ],
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        event = json.loads(proc.stdout)
        self.assertEqual(event["provider"], "codex")
        self.assertEqual(event["source"], "hook")
        self.assertEqual(event["event_type"], "skill_file_access")
        self.assertEqual(event["operation"], "read")
        self.assertEqual(event["skill_name"], "foo")
        self.assertEqual(event["path"], "/Users/me/.codex/skills/foo/SKILL.md")

    def test_installed_sensor_hook_finds_repo_from_codex_config(self) -> None:
        payload_path = REPO_ROOT / "tests" / "fixtures" / "codex-skill-read-hook.json"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            codex_home = tmp_path / ".codex"
            installed_scripts = (
                tmp_path
                / ".codex"
                / "plugins"
                / "cache"
                / "skills-auditor-local"
                / "skill-trace"
                / "0.1.0"
                / "scripts"
            )
            installed_scripts.mkdir(parents=True)
            shutil.copy2(PLUGIN_ROOT / "scripts" / "sensor_hook.py", installed_scripts / "sensor_hook.py")
            codex_home.mkdir(exist_ok=True)
            (codex_home / "config.toml").write_text(
                "\n".join(
                    [
                        "[marketplaces.skills-auditor-local]",
                        'source_type = "local"',
                        f'source = "{REPO_ROOT}"',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["CODEX_HOME"] = str(codex_home)

            proc = subprocess.run(
                [
                    sys.executable,
                    str(installed_scripts / "sensor_hook.py"),
                    "--provider",
                    "codex",
                    "--input-file",
                    str(payload_path),
                    "--log-dir",
                    str(tmp_path / ".skills-auditor-local"),
                    "--dry-run",
                ],
                cwd=str(tmp_path),
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        event = json.loads(proc.stdout)
        self.assertEqual(event["provider"], "codex")
        self.assertEqual(event["event_type"], "skill_file_access")

    def test_sensor_hook_fail_open_on_invalid_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run(
                [
                    sys.executable,
                    str(PLUGIN_ROOT / "scripts" / "sensor_hook.py"),
                    "--provider",
                    "codex",
                    "--log-dir",
                    str(Path(tmp) / ".skills-auditor-local"),
                ],
                input="{not json",
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(proc.returncode, 0)
        self.assertIn("skill-trace sensor hook failed", proc.stderr)

    def test_manifest_command_runs_cached_plugin_from_foreign_workspace(self) -> None:
        with tempfile.TemporaryDirectory(prefix="skill trace ") as tmp:
            root = Path(tmp)
            cached = self.codex_home / "plugins/cache/skills-auditor-local/skill-trace/0.2.0"
            shutil.copytree(PLUGIN_ROOT, cached)
            (self.codex_home / "config.toml").write_text(
                f'[marketplaces.skills-auditor-local]\nsource = "{REPO_ROOT}"\n', encoding="utf-8"
            )
            workspace = root / "another project"
            workspace.mkdir()
            # An unrelated workspace script must never be selected by the hook command.
            (workspace / "scripts").mkdir()
            (workspace / "scripts/sensor_hook.py").write_text("raise RuntimeError('wrong script')")
            config = json.loads((cached / "hooks/hooks.json").read_text())
            command = config["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
            payload = {"hook_event_name": "PreToolUse", "session_id": "foreign", "cwd": str(workspace),
                       "tool_name": "Bash", "tool_input": {"command": "cat SKILL.md"}}
            env = os.environ.copy()
            env["PLUGIN_ROOT"] = str(cached)
            env.pop("SKILLS_AUDITOR_LOG_DIR", None)
            proc = subprocess.run(command, shell=True, cwd=root, env=env, input=json.dumps(payload),
                                  text=True, capture_output=True, timeout=10)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stderr, "")
            files = list((workspace / ".skills-auditor-local/sensors").glob("*/*.jsonl"))
            self.assertEqual(len(files), 1)
            event = json.loads(files[0].read_text())
            self.assertEqual(event["metadata"]["skill_trace"], 1)
            self.assertEqual(event["metadata"]["hook_event_name"], "PreToolUse")
            self.assertFalse((root / ".skills-auditor-local").exists())

    def test_disabled_hook_skips_invalid_input_and_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["SKILLS_AUDITOR_SKILL_TRACE"] = "0"
            proc = subprocess.run(
                [sys.executable, str(PLUGIN_ROOT / "scripts/sensor_hook.py"), "--provider", "codex"],
                cwd=tmp, env=env, input="not JSON", text=True, capture_output=True, timeout=10,
            )
            self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, "", ""))
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_dry_run_is_not_live_capture_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run(
                [sys.executable, str(PLUGIN_ROOT / "scripts/sensor_hook.py"), "--provider", "codex",
                 "--dry-run", "--log-dir", tmp], input='{"hook_event_name":"PreToolUse"}',
                text=True, capture_output=True, timeout=10,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(json.loads(proc.stdout))
            self.assertEqual(list(Path(tmp).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
