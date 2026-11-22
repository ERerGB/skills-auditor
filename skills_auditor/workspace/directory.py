"""Account registrations, environment consent and point-in-time native observations."""
import asyncio
import hashlib
import json
import re
import shutil
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .common import decode_json, digest, fail, fields, identifier, relative

MAX_DIRECTORY = 2 * 1024 * 1024


def skill_uuid(value):
    try:
        if str(uuid.UUID(value)) == value:
            return value
    except (ValueError, AttributeError, TypeError):
        pass
    fail("catalog_identity", "Expected a canonical Skill UUID.")


def listing(profile, pin):
    from .catalog import DIGEST, fetch
    if pin is not None and (not isinstance(pin, str) or not DIGEST.fullmatch(pin)):
        fail("catalog_digest", "Expected a SHA-256 release pin.")
    data = fields(decode_json(fetch(profile, profile["workspace"] + "/directory/" + (pin or "current"), MAX_DIRECTORY)),
                  {"workspace", "environment", "digest", "skills"})
    if data["workspace"] != profile["workspace"] or (pin and data["digest"] != pin):
        fail("catalog_identity", "Directory response differs from the requested workspace or pin.")
    if data["digest"] is not None and not DIGEST.fullmatch(data["digest"]):
        fail("catalog_digest", "Invalid directory pin.")
    skill_uuid(data["environment"])
    seen, names, entries = set(), set(), set()
    for row in data["skills"]:
        fields(row, {"skill_id", "name", "entry_name", "subscribed", "source_file", "repository", "revision", "declared_source_file"})
        key = skill_uuid(row["skill_id"])
        entry = row["entry_name"]
        for name in (row["name"], entry):
            if name is not None and (not isinstance(name, str) or len(name) > 240 or not re.fullmatch(r"[\w.:-]+(?:/[\w.:-]+)*", name, re.ASCII)):
                fail("catalog_identity", "Invalid directory name.")
        if (not row["name"] or key in seen or row["name"] in names or (entry is not None and entry in entries)
                or type(row["subscribed"]) is not bool):
            fail("catalog_identity", "Ambiguous account directory.")
        seen.add(key)
        names.add(row["name"])
        if entry is not None:
            entries.add(entry)
    return data


def consent(profile, path, directory):
    return {"profile": str(Path(path).absolute()), "origin": profile["origin"], "workspace": directory["workspace"],
            "environment": directory["environment"], "digest": directory["digest"],
            "reader": hashlib.sha256(profile["token"].encode()).hexdigest(),
            "selection": digest(sorted((r["skill_id"], r["entry_name"], r["subscribed"]) for r in directory["skills"]))}


def recheck(approved):
    from .catalog import profile_at
    profile = profile_at(approved["profile"])
    if consent(profile, approved["profile"], listing(profile, approved["digest"])) != approved:
        fail("catalog_stale", "Environment subscriptions or registration changed. Prepare a new plan.")


def local_project(service, name):
    state = service.registry.read()
    project = state["workspaces"].get(name)
    if not project or state["environments"][project["environment"]]["provider"] != "local":
        fail("catalog_environment", "Run this command on the enrolled target using its local project binding.")
    # Confirm that the private binding still names the same concrete project.
    from .node import Node
    env = state["environments"][project["environment"]]
    node = Node(env["state_dir"])
    node._binding({"environment": project["environment"], "generation": env["generation"]})
    registered = node._workspace(name)
    if Path(registered["path"]) != Path(project["path"]).resolve():
        fail("catalog_environment", "Project path differs from the enrolled binding.")
    return registered


async def _codex_entries(project):
    executable = shutil.which("codex")
    if not executable:
        fail("catalog_native", "Codex is unavailable on this client.")
    proc = await asyncio.create_subprocess_exec(executable, "app-server", "--listen", "stdio://", cwd=project,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        limit=8 * 1024 * 1024)

    async def send(msg):
        proc.stdin.write(json.dumps(msg).encode() + b"\n")
        await proc.stdin.drain()

    async def reply(number):
        while True:
            line = await proc.stdout.readline()
            if not line:
                fail("catalog_native", "Codex ended before returning its catalog.")
            row = decode_json(line)
            if row.get("id") == number:
                if "error" in row:
                    fail("catalog_native", "Codex could not inspect the project.")
                return row["result"]

    async def inspect():
        await send({"id": 1, "method": "initialize", "params": {"clientInfo": {
            "name": "skills_auditor", "title": "Skills Auditor", "version": "1"}, "capabilities": {}}})
        await reply(1)
        await send({"method": "initialized", "params": {}})
        await send({"id": 2, "method": "skills/list", "params": {"cwds": [project], "forceReload": True}})
        rows = (await reply(2))["data"]
        if len(rows) != 1 or rows[0]["cwd"] != project or rows[0]["errors"]:
            fail("catalog_native", "Codex did not return a complete scan for this project.")
        return rows[0]["skills"]

    try:
        return await asyncio.wait_for(inspect(), timeout=30)
    except (asyncio.TimeoutError, ValueError, KeyError, OSError):
        fail("catalog_native", "Native scan failed. Previous observations remain historical.")
    finally:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        await proc.communicate()


def summarize(directory, entries):
    paths = {}
    for entry in entries:
        if not isinstance(entry["enabled"], bool):
            fail("catalog_native", "Invalid Codex discovery status.")
        if entry["enabled"]:
            paths.setdefault(entry["name"], []).append(Path(entry["path"]))
    results = {}
    for item in directory["skills"]:
        matches = paths.get(item["entry_name"] or item["name"], [])
        state = "multiple" if len(matches) > 1 else "present" if matches else "absent"
        entry_hash = None
        if len(matches) == 1:
            path = matches[0]
            if path.name == "SKILL.md" and path.is_absolute():
                try:
                    info = path.stat()
                    if stat.S_ISREG(info.st_mode) and info.st_size <= 1024 * 1024:
                        with path.open("rb") as f:
                            body = f.read(1024 * 1024 + 1)
                        if len(body) == info.st_size:
                            entry_hash = hashlib.sha256(body).hexdigest()
                except OSError:
                    pass
        results[item["skill_id"]] = {"state": state, "entry_sha256": entry_hash}
    return results


def set_subscription(profile, key, enabled):
    from .catalog import fetch
    fetch(profile, profile["workspace"] + "/subscriptions/" + skill_uuid(key), method="PUT" if enabled else "DELETE")
    return {"skill_id": key, "subscribed": enabled}


def configure(cmds):
    for action in ("list", "select", "observe"):
        command = cmds.add_parser(action)
        command.add_argument("--pin")
        if action == "list":
            command.add_argument("--project", help="Include this environment's last reported Codex scan.")
        if action == "observe":
            command.add_argument("project", help="Registered local project to scan using Codex.")
    register = cmds.add_parser("register", help="Explicitly register an identity using an account-owner credential.")
    register.add_argument("skill_id")
    register.add_argument("--name", required=True)
    register.add_argument("--source-file", help="Optional portable relative SKILL.md path.")
    cmds.add_parser("unregister", help="Retire an account registration.").add_argument("skill_id")
    for action in ("subscribe", "unsubscribe"):
        cmds.add_parser(action).add_argument("skill_id")


def run(args, profile, service):
    from .catalog import fetch
    op = args.catalog_command
    if op in ("register", "unregister"):
        key = skill_uuid(args.skill_id)
        route = profile["workspace"] + "/registrations/" + key
        if op == "register":
            if args.source_file and Path(relative(args.source_file)).name != "SKILL.md":
                fail("catalog_source", "Supply a relative SKILL.md path.")
            fetch(profile, route, method="PUT", body={"name": args.name, "source_file": args.source_file})
        else:
            fetch(profile, route, method="DELETE")
        return {"skill_id": key, "registered": op == "register"}
    if op in ("subscribe", "unsubscribe"):
        return set_subscription(profile, args.skill_id, op == "subscribe")
    data = listing(profile, args.pin)
    pin = data["digest"]
    if op == "select":
        for number, row in enumerate(data["skills"], 1):
            print("[{}] {}. {}".format("x" if row["subscribed"] else " ", number, row["name"]))
        try:
            answer = input("Toggle one subscription by number (Enter to leave): ").strip()
        except EOFError:
            answer = ""
        if not answer:
            return {"status": "unchanged"}
        if not answer.isdecimal() or not 1 <= int(answer) <= len(data["skills"]):
            fail("catalog_selection", "Choose a displayed number.")
        row = data["skills"][int(answer) - 1]
        return set_subscription(profile, row["skill_id"], not row["subscribed"])
    if args.project:
        if pin is None:
            fail("catalog_observation", "No promoted release to compare with the native scan.")
        route = "{}/observations/{}/{}/codex".format(profile["workspace"], pin, identifier(args.project))
        if op == "observe":
            project = local_project(service, args.project)
            observed_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            observations = summarize(data, asyncio.run(_codex_entries(project["path"])))
            fetch(profile, route, method="PUT", body={"observed_at": observed_at, "observations": observations})
        scan = decode_json(fetch(profile, route, MAX_DIRECTORY))
        data.update(project=args.project, platform="codex", observed_at=scan["observed_at"])
        for row in data["skills"]:
            row["native_discovery"] = scan["observations"].get(row["skill_id"], {"state": "unverified"})
    return data
