"""Chat-friendly operations backed by the same bounded Python API."""
import argparse
import json
from pathlib import Path

from ..lifecycle.common import LifecycleError
from .common import DEFAULT_PLATFORMS, PLATFORMS, atomic_json, fail, read_json
from .service import WorkspaceService


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise LifecycleError("workspace_arguments", message, exit_code=2)


def parser(prog="skills-audit workspace"):
    root = Parser(prog=prog, description="Manage Skills across workspaces using existing local/SSH connections.")
    root.add_argument("--registry-root", type=Path, help="Private owner-local bindings and controller evidence.")
    root.add_argument("--format", choices=("json", "text"), default="json")
    sub = root.add_subparsers(dest="command", required=True)
    env = sub.add_parser("environment", help="Bind an execution environment without provisioning connectivity.").add_subparsers(dest="environment_command", required=True)
    add = env.add_parser("add")
    add.add_argument("id")
    add.add_argument("--provider", choices=("local", "ssh", "potology"), required=True)
    add.add_argument("--generation", required=True)
    add.add_argument("--state-dir", required=True)
    for name in ("connection", "python", "python-path", "potology-state", "potology-tools", "potology-target", "pairing"):
        add.add_argument("--" + name)
    source = sub.add_parser("source", help="Bind an explicit package source; plugin packages are excluded.").add_subparsers(dest="source_command", required=True)
    add = source.add_parser("add")
    add.add_argument("id")
    add.add_argument("--root", required=True, type=Path)
    add.add_argument("--kind", choices=("git", "local"), required=True)
    add.add_argument("--commit")
    add.add_argument("--include", action="append", required=True)
    add.add_argument("--require-file", action="append", default=[])
    add.add_argument("--repository", help="Credential-free provenance URL; no implicit fetch.")
    add.add_argument("--entry", action="append", required=True, help="ALIAS=RELATIVE/SKILL/DIRECTORY")
    add.add_argument("--platform", action="append", choices=PLATFORMS)
    register = sub.add_parser("register", help="Explicitly bind a workspace and prepare empty native roots.")
    register.add_argument("id")
    register.add_argument("--environment", required=True)
    register.add_argument("--path", required=True)
    register.add_argument("--declaration", required=True, type=Path)
    sub.add_parser("list")
    sub.add_parser("status")
    plan = sub.add_parser("plan")
    plan.add_argument("workspaces", nargs="*")
    plan.add_argument("--legacy-receipt", action="append", default=[], help="ENVIRONMENT=CONTROLLER_RECEIPT_FILE; same-target proof only.")
    plan.add_argument("--release", type=Path, help="Retained source release; never restores old approvals.")
    plan.add_argument("--plan-out", type=Path)
    apply = sub.add_parser("apply")
    apply.add_argument("plan", type=Path)
    apply.add_argument("--approve-plan-id")
    verify = sub.add_parser("verify")
    verify.add_argument("workspaces", nargs="*")
    verify.add_argument("--native", action="append", choices=("grok",), default=[], help="Read the native catalog; never grants folder/hook trust or logs in.")
    rollback = sub.add_parser("rollback", help="Prepare a new reviewed inverse; never silently replays old approval.")
    rollback.add_argument("plan", type=Path)
    rollback.add_argument("--plan-out", type=Path)
    recover = sub.add_parser("recover")
    recover.add_argument("plan", type=Path)
    recover.add_argument("--mode", choices=("inspect", "resume"), default="inspect")
    recover.add_argument("--approve-plan-id")
    export = sub.add_parser("export-release")
    export.add_argument("plan", type=Path)
    export.add_argument("--out", required=True, type=Path)
    return root


def main(argv=None, prog="skills-audit workspace"):
    try:
        args = parser(prog).parse_args(argv)
        service = WorkspaceService(args.registry_root)
        if args.command == "environment":
            config = {"provider": args.provider, "generation": args.generation, "state_dir": args.state_dir}
            for key in ("connection", "python", "python_path", "potology_state", "potology_tools", "potology_target", "pairing"):
                if getattr(args, key):
                    config[key] = getattr(args, key)
            service.registry.put("environments", args.id, config)
            value = {"status": "bound", "environment": args.id, "generation": args.generation}
        elif args.command == "source":
            entries = {}
            for item in args.entry:
                name, sep, path = item.partition("=")
                if not sep or name in entries:
                    fail("workspace_arguments", "Entrypoints must be distinct ALIAS=PATH operands.")
                entries[name] = path
            binding = {"root": str(args.root.expanduser().resolve()), "kind": args.kind,
                       "include": args.include, "entries": entries,
                       "required_files": args.require_file,
                       "platforms": args.platform or list(DEFAULT_PLATFORMS)}
            if args.commit:
                binding["commit"] = args.commit
            if args.repository:
                binding["repository"] = args.repository
            service.registry.put("sources", args.id, binding)
            value = {"status": "bound", "source": args.id}
        elif args.command == "register":
            value = service.register(args.id, args.environment, args.path, args.declaration)
        elif args.command == "list":
            value = service.registry.read()
        elif args.command == "status":
            value = service.status()
        elif args.command == "plan":
            receipts = {}
            for item in args.legacy_receipt:
                name, sep, path = item.partition("=")
                if not sep:
                    fail("workspace_arguments", "Legacy receipt requires ENVIRONMENT=FILE.")
                receipts.setdefault(name, []).append(read_json(path))
            value = service.plan(args.workspaces, legacy_receipts=receipts, release=read_json(args.release) if args.release else None)
        elif args.command == "apply":
            value = service.apply(read_json(args.plan), approve_plan_id=args.approve_plan_id)
        elif args.command == "verify":
            value = service.verify(args.workspaces, native_platforms=args.native)
        elif args.command == "rollback":
            value = service.rollback(read_json(args.plan))
        elif args.command == "recover":
            value = service.recover(read_json(args.plan), mode=args.mode, approve_plan_id=args.approve_plan_id)
        else:
            value = service.export_release(read_json(args.plan), args.out)
        if getattr(args, "plan_out", None):
            atomic_json(args.plan_out, value)
        if args.format == "text":
            print("Workspace {}: {}".format(args.command, value.get("status", "passed" if value.get("valid") else "observed")))
            if "summary" in value:
                print("Changes: {changes}; unchanged: {unchanged}; blocked: {blocked}".format(**value["summary"]))
        print(json.dumps(value, indent=2, ensure_ascii=False))
        return 3 if value.get("status") in {"blocked", "partial", "uncertain"} or value.get("valid") is False else 0
    except (LifecycleError, OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({"schema_version": "skills-auditor-workspace-error/v1", "status": "error",
                          "code": getattr(error, "code", "workspace_io"), "message": str(error)[:1000],
                          "details": getattr(error, "details", {})}))
        return getattr(error, "exit_code", 3)
