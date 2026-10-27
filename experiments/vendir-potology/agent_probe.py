#!/usr/bin/env python3
"""Linux-only live acceptance harness. Uses existing target login, never copies auth.

Run on the target with a new --output directory, a built wheel, pinned vendir binary,
and the native lock from the first experiment. All registration is temporary.
The caller reviews agent.jsonl and agent-final.txt; a zero model exit is not acceptance.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import zipfile

COMMIT = "9f1fd18022ecaaee14f8d2a67bf35821f48dc710"
ORIGIN = "https://github.com/the-mag-tech/potology.git"
REQUIRED = [
    "AGENTS.md", "docs/repo-context.md", "docs/context-migration-standard.md",
    "docs/remote-work.md", "docs/context-protocol.md", "tools/check-workspace.py",
    "tools/repo-context.py", "tools/context_migration.py",
    "tools/context_archive.py", "tools/context_protocol.py",
    "tools/context-schema.json", "tools/context-migration-schema.json",
]


def assess_agent_events(events):
    """Bounded checks for this Codex CLI fixture; semantic acceptance is reviewed separately."""
    completed = [event["item"] for event in events if event.get("type") == "item.completed"]
    commands = [item for item in completed if item["type"] == "command_execution"
                and item.get("exit_code") == 0]
    def executed(command_fragment, output_fragment):
        return any(command_fragment in item["command"]
                   and output_fragment in item.get("aggregated_output", "") for item in commands)
    exact_record = False
    for item in commands:
        if "repo-context.py record" not in item["command"]:
            continue
        output = item.get("aggregated_output", "")
        # A real agent may batch record retrieval with another shell check.
        for candidate in [output, *output.splitlines()]:
            try:
                result = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(result, dict) and result.get("public_only") is True:
                exact_record = exact_record or any(
                    match.get("record", {}).get("id", "").startswith("record:")
                    and match["record"]["id"] in item["command"]
                    for match in result.get("matches", [])
                )
    return {
        "agent_read_skill": executed("SKILL.md", "name: task-handoff"),
        "agent_read_packet": executed("handoff-packet.md", "# Handoff packet"),
        "agent_inventory": executed("context_migration.py inventory", COMMIT),
        "agent_shared_context": executed("repo-context.py shared", COMMIT),
        "agent_exact_record": exact_record,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("source", "lock", "vendir", "wheel", "output"):
        parser.add_argument("--" + flag, required=True, type=Path)
    args = parser.parse_args()
    args.output = args.output.resolve()
    args.output.mkdir(mode=0o700)  # Refuse to mix or overwrite earlier evidence.
    evidence = {"commands": [], "checks": [], "commit": COMMIT}
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")

    def run(argv, cwd=args.output, ok=True, timeout=120):
        result = subprocess.run([str(x) for x in argv], cwd=cwd, env=env,
                                text=True, capture_output=True, timeout=timeout)
        evidence["commands"].append({"argv": [str(x) for x in argv], "cwd": str(cwd),
                                    "exit": result.returncode, "stdout": result.stdout,
                                    "stderr": result.stderr})
        if ok and result.returncode:
            raise RuntimeError(f"{argv[0]} failed: {result.stderr[-1500:]}")
        return result

    def check(name, value, **details):
        evidence["checks"].append({"name": name, "passed": bool(value), **details})
        if not value:
            raise AssertionError(name)

    def source_status():
        return run(["git", "-C", args.source, "status", "--porcelain"]).stdout

    before = source_status()
    try:
        # This image has no pip/ensurepip. Exercise the built pure-Python wheel
        # without installing tooling or editing the target's Python environment.
        package_root = args.output / "wheel"
        with zipfile.ZipFile(args.wheel) as wheel:
            wheel.extractall(package_root)
        env["PYTHONPATH"] = str(package_root)
        cli = ["python3", "-m", "skills_auditor"]
        evidence["wheel_sha256"] = hashlib.sha256(args.wheel.read_bytes()).hexdigest()
        evidence["vendir_sha256"] = hashlib.sha256(args.vendir.read_bytes()).hexdigest()
        evidence["codex_version"] = run(["codex", "--version"]).stdout.strip()

        # Use the host's tool-visible filesystem. /dev/shm is separately mounted
        # inside the Codex shell isolation and can yield a listed but unreadable skill.
        with tempfile.TemporaryDirectory(prefix="auditor-payload-", dir=args.output) as temp:
            release = Path(temp)
            payload = release / "payload"
            config = ("apiVersion: vendir.k14s.io/v1alpha1\nkind: Config\ndirectories:\n"
                      "- path: payload\n  contents:\n  - path: .\n    git:\n"
                      f"      url: {json.dumps(args.source.resolve().as_uri())}\n"
                      f"      ref: {COMMIT}\n")
            (release / "vendir.yml").write_text(config)
            shutil.copyfile(args.lock, release / "vendir.lock.yml")
            lock_bytes = args.lock.read_bytes()
            run([args.vendir.resolve(), "sync", "--locked"], cwd=release)
            check("native_lock_unchanged", (release / "vendir.lock.yml").read_bytes() == lock_bytes)
            shutil.copyfile(release / "vendir.yml", args.output / "vendir.yml")
            shutil.copyfile(release / "vendir.lock.yml", args.output / "vendir.lock.yml")
            check("cross_filesystem", args.source.stat().st_dev != payload.stat().st_dev,
                  source_device=args.source.stat().st_dev, target_device=payload.stat().st_dev,
                  mounts=run(["df", "-T", args.source, payload]).stdout)
            check("native_git_head", run(["git", "rev-parse", "HEAD"], cwd=payload).stdout.strip() == COMMIT)
            # Binding this private delivered copy is part of this explicitly scoped probe.
            run(["git", "remote", "set-url", "origin", ORIGIN], cwd=payload)
            run(["python3", "-m", "unittest", "discover", "-s", "tests",
                 "-p", "test_context_migration.py", "-v"], cwd=payload)

            host = args.output / "host"
            host.mkdir()
            run(["git", "init", "--quiet"], cwd=host)
            plan, receipt = args.output / "plan.json", args.output / "receipt.json"
            flags = [part for path in REQUIRED for part in ("--require-file", str(payload / path))]
            # Also retain the reviewed native delivery inputs as ordinary required files.
            flags += ["--require-file", str(release / "vendir.yml"),
                      "--require-file", str(release / "vendir.lock.yml")]
            run([*cli, "integrate", "--source", payload / ".agents/skills",
                 "--target-root", f"codex={host / '.agents/skills'}",
                 *flags, "--plan-out", plan, "--format", "json"], cwd=host)
            run([*cli, "apply", plan, "--receipt-out", receipt, "--format", "json"], cwd=host)
            run([*cli, "verify", receipt, "--format", "json"], cwd=host)
            dependency = payload / "tools/context_protocol.py"
            original = dependency.read_bytes()
            dependency.unlink()
            missing = run([*cli, "verify", receipt, "--format", "json"], cwd=host, ok=False)
            check("missing_external_dependency_rejected", missing.returncode == 3 and any(
                c["code"] == "required_file" and not c["ok"] for c in json.loads(missing.stdout)["checks"]))
            dependency.write_bytes(original)
            run([*cli, "verify", receipt, "--format", "json"], cwd=host)

            prompt = f"""This is a bounded live acceptance test of an installed skill.
Use $task-handoff from the skills available to you. Do not locate an unlisted skill by searching elsewhere;
if it is absent from your available skills, report DISCOVERY_FAILED and stop.
First report its catalog path, then read its SKILL.md and packet template through tools.
The task is read-only acceptance of the existing reviewed Potology context in {payload}.
This is an isolated delivered copy at expected commit {COMMIT}, origin {ORIGIN}; your host cwd is {host}.
Apply the skill's destination-evidence steps: read repo rules, verify actual cwd/HEAD/origin/status;
execute context_migration.py inventory; query repo-context.py shared --public-only --ref HEAD;
retrieve at least one exact public record with repo-context.py record --public-only --ref HEAD --id ID.
Explain a decision from that record, cite its exact ID, state the next action and authorization limits.
Use actual tool execution and exit/output evidence. Read canonical paths for skill-relative references.
No file changes, session capture, dispatch, new tasks, messages, installation, trust/config changes,
hook activation, publication, network fetching or global filesystem searching are authorized.
You are only the read-only acceptance executor; no work ownership is transferred.
Do not claim native session migration, hook capture, or full task handoff acceptance.
"""
            (args.output / "prompt.txt").write_text(prompt)
            argv = ["codex", "exec", "--ephemeral", "--sandbox", "read-only", "-C", str(host),
                    "--json", "--output-last-message", str(args.output / "agent-final.txt"), "-"]
            evidence["agent_argv"] = argv
            with (args.output / "agent.jsonl").open("w") as stdout, (args.output / "agent.stderr").open("w") as stderr:
                result = subprocess.run(argv, input=prompt, text=True, stdout=stdout, stderr=stderr,
                                        env=env, timeout=600)
            check("agent_process_completed", result.returncode == 0, exit=result.returncode)
            events = [json.loads(line) for line in (args.output / "agent.jsonl").read_text().splitlines()]
            for name, passed in assess_agent_events(events).items():
                check(name, passed)
            run([*cli, "verify", receipt, "--format", "json"], cwd=host)
            check("payload_still_clean", not run(["git", "status", "--porcelain"], cwd=payload).stdout.strip())
        check("source_status_unchanged", source_status() == before)
    finally:
        # Even a failed/timeout probe must not retain links into deleted content.
        host = args.output / "host"
        if host.exists():
            shutil.rmtree(host)
        evidence["temporary_host_removed"] = not host.exists()
        (args.output / "evidence.json").write_text(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    main()
