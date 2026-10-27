#!/usr/bin/env python3
"""Two-phase Codespaces acceptance fixture, not an environment manager.

An external operator creates/rebuilds/deletes the dedicated cloud environment and
retains provider receipts. This script only runs delivery, registration, and agent
acceptance. Inputs and evidence persist under /workspaces; delivery must not.
No credentials are provisioned, copied, printed, or captured here.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import uuid
import zipfile

from agent_probe import COMMIT, ORIGIN, assess_agent_events


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tracked_files(payload):
    names = subprocess.check_output(["git", "ls-files", "-z"], cwd=payload).decode().split("\0")
    return {name: {"sha256": sha(payload / name), "executable": bool((payload / name).stat().st_mode & 0o111)}
            for name in names if name}


def environment_facts():
    return {
        "codespace": os.environ.get("CODESPACE_NAME"),
        "codespaces": os.environ.get("CODESPACES"),
        "hostname": socket.gethostname(),
        "python_version": sys.version,
        "pid1_mount_namespace": os.readlink("/proc/1/ns/mnt"),
        "process_mount_namespace": os.readlink("/proc/self/ns/mnt"),
        "pid1_start_time": Path("/proc/1/stat").read_text().rsplit(")", 1)[1].split()[19],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("baseline", "restore"))
    for option in ("inputs", "evidence", "delivery", "wheel", "vendir"):
        parser.add_argument("--" + option, required=True, type=Path)
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--source-mirror", type=Path, help="Optional authorized Git mirror; uses native Git URL rewriting only for vendir")
    args = parser.parse_args()
    for option in ("inputs", "evidence", "delivery", "wheel", "vendir"):
        setattr(args, option, getattr(args, option).resolve())
    if os.environ.get("CODESPACES") != "true":
        parser.error("this fixture requires an actual Codespace; provider identity must be checked separately")
    persistent = Path("/workspaces")
    if not args.evidence.is_relative_to(persistent) or args.delivery.is_relative_to(persistent):
        parser.error("evidence must persist in /workspaces and delivery must be outside it")
    if args.delivery.exists():
        parser.error("delivery already exists: refuse to overwrite it or disguise a failed rebuild")
    args.evidence.mkdir(parents=True, exist_ok=True, mode=0o700)
    phase = args.evidence / args.phase
    phase.mkdir(mode=0o700)
    evidence = {"phase": args.phase, "status": "running", "checks": [], "commands": [],
                "environment": environment_facts(), "delivery": str(args.delivery)}
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", GIT_TERMINAL_PROMPT="0")

    def run(argv, cwd=args.delivery, ok=True, command_env=None):
        result = subprocess.run([str(v) for v in argv], cwd=cwd, env=command_env or env, text=True,
                                capture_output=True, timeout=180)
        evidence["commands"].append({"argv": [str(v) for v in argv], "cwd": str(cwd),
                                    "exit": result.returncode, "stdout": result.stdout, "stderr": result.stderr})
        if ok and result.returncode:
            raise RuntimeError(f"{argv[0]} failed with exit {result.returncode}; inspect private evidence")
        return result

    def check(name, passed, **details):
        evidence["checks"].append({"name": name, "passed": bool(passed), **details})
        if not passed:
            raise AssertionError(name)

    try:
        baseline = None
        inputs = {name: sha(args.inputs / name) for name in
                  ("vendir.yml", "vendir.lock.yml", "skills-auditor.json")}
        inputs["wheel"] = sha(args.wheel)
        inputs["vendir"] = sha(args.vendir)
        inputs["cloud_probe"] = sha(Path(__file__))
        inputs["agent_probe"] = sha(Path(__file__).with_name("agent_probe.py"))
        evidence["inputs"] = inputs
        if args.phase == "restore":
            baseline = json.loads((args.evidence / "baseline/evidence.json").read_text())
            check("baseline_passed", baseline["status"] == "passed")
            check("same_delivery_location", baseline["delivery"] == str(args.delivery))
            if baseline["environment"]["codespace"] and evidence["environment"]["codespace"]:
                check("same_codespace", baseline["environment"]["codespace"] == evidence["environment"]["codespace"])
            else:
                evidence["runtime_codespace_identity"] = "not exposed; require matching provider receipts"
            check("container_recreated", any(baseline["environment"][key] != evidence["environment"][key]
                  for key in ("hostname", "pid1_mount_namespace", "pid1_start_time")))
            check("old_delivery_absent_before_sync", not args.delivery.exists())
            check("identical_inputs_after_rebuild", inputs == baseline["inputs"])

        args.delivery.mkdir(mode=0o700)
        (args.delivery / "rebuild-canary").write_text(str(uuid.uuid4()))
        evidence["rebuild_canary_sha256"] = sha(args.delivery / "rebuild-canary")
        for name in ("vendir.yml", "vendir.lock.yml", "skills-auditor.json"):
            shutil.copyfile(args.inputs / name, args.delivery / name)
        package = args.delivery / "auditor-wheel"
        with zipfile.ZipFile(args.wheel) as wheel:
            wheel.extractall(package)
        env["PYTHONPATH"] = str(package)
        evidence["codex_version"] = run([args.codex, "--version"]).stdout.strip()
        evidence["vendir_version"] = run([args.vendir, "--version"]).stdout.strip()
        delivery_env = env
        if args.source_mirror:
            mirror = args.source_mirror.resolve().as_uri()
            delivery_env = dict(env, GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0=f"url.{mirror}.insteadOf",
                                GIT_CONFIG_VALUE_0=ORIGIN)
            evidence["source_mirror"] = mirror
        run([args.vendir, "sync", "--locked"], command_env=delivery_env)
        payload, host = args.delivery / "payload", args.delivery / "host"
        check("native_inputs_unchanged", all(sha(args.delivery / name) == inputs[name]
              for name in ("vendir.yml", "vendir.lock.yml")))
        check("pinned_head", run(["git", "rev-parse", "HEAD"], cwd=payload).stdout.strip() == COMMIT)
        check("canonical_origin", run(["git", "remote", "get-url", "origin"], cwd=payload).stdout.strip() == ORIGIN)
        evidence["tracked_files"] = tracked_files(payload)
        if baseline:
            check("restored_file_bytes_and_exec_bits", evidence["tracked_files"] == baseline["tracked_files"])
        evidence["mounts"] = run(["findmnt", "-T", payload]).stdout
        run(["python3", "-m", "unittest", "discover", "-s", "tests", "-p", "test_context_migration.py", "-v"], cwd=payload)
        cli = ["python3", "-m", "skills_auditor"]
        plan, receipt = phase / "plan.json", phase / "receipt.json"
        run([*cli, "integrate", "--config", "skills-auditor.json", "--plan-out", plan, "--format", "json"])
        run([*cli, "apply", plan, "--receipt-out", receipt, "--format", "json"])
        run([*cli, "verify", receipt, "--format", "json"])
        run(["git", "init", "--quiet"], cwd=host)
        probe_file = payload / "tools/context_protocol.py"
        expected_probe = sha(probe_file)
        prompt = f"""Use $task-handoff from your available skill catalog for a bounded read-only cloud acceptance test.
If the skill is not listed, report DISCOVERY_FAILED and stop; do not search other locations for it.
Report its catalog path, read SKILL.md through tools, and read the canonical handoff-packet.md template.
Potology is delivered at {payload}, expected HEAD {COMMIT}, origin {ORIGIN}. Host cwd is {host}.
Follow its destination-evidence steps, including repo rules, actual cwd/HEAD/origin/status,
context_migration.py inventory, repo-context.py shared --public-only --ref HEAD, and
repo-context.py record --public-only --ref HEAD --id for one exact returned public record.
Explain one sourced decision and the next action, distinguishing historical approvals from current scope.
Also prove tool-side filesystem visibility with a shell command running Python that prints ONE JSON line:
{{"probe":"skill-filesystem-visibility","dependency_path":"{probe_file}","sha256":<actual SHA256 of its bytes>,
"mount_namespace":<os.readlink('/proc/self/ns/mnt')>,"device":<os.stat(path).st_dev>}}.
Expected dependency SHA256 is {expected_probe}. Use actual tool execution; do not merely repeat expected values.
No writes, session capture, dispatch, new tasks/messages, install, trust changes, hooks, network fetching,
or public posting are authorized. No work ownership is transferred. Do not claim full handoff acceptance.
"""
        (phase / "prompt.txt").write_text(prompt)
        argv = [args.codex, "exec", "--ephemeral", "--sandbox", "read-only", "-C", str(host),
                "--json", "--output-last-message", str(phase / "agent-final.txt"), "-"]
        evidence["agent_argv"] = argv
        with (phase / "agent.jsonl").open("w") as stdout, (phase / "agent.stderr").open("w") as stderr:
            result = subprocess.run(argv, input=prompt, env=env, text=True, stdout=stdout,
                                    stderr=stderr, timeout=600)
        check("agent_exited_successfully", result.returncode == 0)
        events = [json.loads(line) for line in (phase / "agent.jsonl").read_text().splitlines()]
        for name, passed in assess_agent_events(events).items():
            check(name, passed)
        observations = []
        for event in events:
            item = event.get("item", {})
            if event.get("type") != "item.completed" or item.get("type") != "command_execution" or item.get("exit_code") != 0:
                continue
            for line in item.get("aggregated_output", "").splitlines():
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict) and value.get("probe") == "skill-filesystem-visibility":
                    observations.append(value)
        check("tool_side_visibility", any(v.get("dependency_path") == str(probe_file)
              and v.get("sha256") == expected_probe and isinstance(v.get("mount_namespace"), str)
              and isinstance(v.get("device"), int) for v in observations), observations=observations)
        run([*cli, "verify", receipt, "--format", "json"])
        check("payload_clean_after_agent", not run(["git", "status", "--porcelain"], cwd=payload).stdout.strip())
        evidence["status"] = "passed"
    except Exception as exc:
        evidence["status"] = "failed"
        evidence["error"] = {"type": type(exc).__name__, "message": str(exc)}
        raise
    finally:
        # Keep baseline delivery present: its disappearance must be caused by the
        # provider rebuild, never by local cleanup masquerading as a restore test.
        (phase / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")


if __name__ == "__main__":
    main()
