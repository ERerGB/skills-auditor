"""Managed console adapter: explicit approval, one JSON result, scoped state."""

import argparse
import json
import os
from pathlib import Path
import shlex
import stat
import sys

from .common import LifecycleError, atomic_json, canonical_entry, canonical_json, paths_overlap as _overlap
from .engine import Manager, OPERATIONS
from . import incidents, invocation, retention
from .batch import BatchManager
from .status import preflight, read_status, render_status, unknown_status
from .context import action, manager_context, project_context
from .pending import list_pending


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise LifecycleError("invalid_arguments", message, exit_code=2)


def _attribution(parser, *, tool=False, actor="local-operator"):
    parser.add_argument("--actor", default=actor, help="Local attribution label, not authenticated identity.")
    if tool:
        parser.add_argument("--tool", default="lifecycle", help="Local tool label, not authenticated identity.")


def _consumers(commands):
    leaves = []

    def leaf(group, name, **kwargs):
        parser = group.add_parser(name, **kwargs)
        leaves.append(parser)
        return parser

    listing = leaf(commands, "incidents", help="List validated incident records, including historical dispositions.")
    listing.add_argument("--installation-id")
    listing.add_argument("--state", choices=("open", "investigating", "resolved", "superseded"))
    investigate = leaf(commands, "investigate", help="Read an evidence page; references do not load arbitrary files.")
    investigate.add_argument("incident_id")
    investigate.add_argument("--limit", type=int, default=50)
    investigate.add_argument("--after-sequence", type=int)
    note = leaf(commands, "append-note", help="Append bounded investigation context; never include secrets or prompts.")
    note.add_argument("incident_id")
    note.add_argument("--text", required=True)
    note.add_argument("--event-id", help="Optional idempotency key for an exact note retry.")
    note.add_argument("--evidence-ref", action="append", default=[], help="Existing local record KIND:ID; not a file path.")
    resolve = leaf(commands, "resolve", help="Record explicit remediation proof or non-remediation disposition; never grants approval.")
    resolve.add_argument("incident_id")
    proof = resolve.add_mutually_exclusive_group(required=True)
    proof.add_argument("--verification-id")
    proof.add_argument("--disposition")
    resolve.add_argument("--explanation")
    supersede = leaf(commands, "supersede", help="Link an existing related incident without erasing history.")
    supersede.add_argument("incident_id")
    supersede.add_argument("replacement_id")
    supersede.add_argument("--explanation", required=True)
    for parser in (note, resolve, supersede):
        _attribution(parser, tool=True)

    uses = leaf(commands, "invocation", help="Select a currently authorized snapshot; never executes a Skill or silently rolls back.")
    use = uses.add_subparsers(dest="invocation_command", required=True)
    select = leaf(use, "select")
    select.add_argument("installation_id")
    select.add_argument("--policy", choices=("strict", "last-known-good"), default="strict")
    select.add_argument("--cached", action="store_true")
    select.add_argument("--max-age-seconds", type=int, default=300)
    select.add_argument("--override-id")
    _attribution(select, tool=True, actor="local-adapter")
    plan = leaf(use, "override-plan", help="Plan only a time-limited stale-observation exception; integrity and authorization cannot be waived.")
    plan.add_argument("installation_id")
    plan.add_argument("--reason", required=True)
    plan.add_argument("--ttl-seconds", type=int, default=300)
    plan.add_argument("--max-age-seconds", type=int, default=300)
    plan.add_argument("--plan-out", type=Path)
    apply = leaf(use, "override-apply")
    apply.add_argument("plan", type=Path)
    apply.add_argument("--approve-plan-id")
    revoke = leaf(use, "override-revoke")
    revoke.add_argument("override_id")
    revoke.add_argument("--reason", required=True)
    get = leaf(use, "override-get")
    get.add_argument("override_id")
    for parser in (apply, revoke):
        _attribution(parser, tool=True)

    retained = leaf(commands, "retention", help="Plan payload retention separately from permanent deletion; preserve metadata history.")
    storage = retained.add_subparsers(dest="retention_command", required=True)
    plan = leaf(storage, "plan")
    plan.add_argument("operation", choices=("policy", "expire", "collect", "restore", "purge"))
    for selector in ("receipt-id", "incident-id", "object-id", "stage-name"):
        plan.add_argument("--" + selector, action="append", default=[])
    pins = plan.add_mutually_exclusive_group()
    pins.add_argument("--pin-version-id", action="append")
    pins.add_argument("--clear-pins", action="store_true")
    plan.add_argument("--keep-recent", type=int)
    plan.add_argument("--grace-seconds", type=int, default=604800)
    plan.add_argument("--plan-out", type=Path)
    apply = leaf(storage, "apply")
    apply.add_argument("plan", type=Path)
    apply.add_argument("--transaction-id")
    _attribution(apply)
    recover = leaf(storage, "recover", help="Inspect by default; recovery retains the transaction's recorded local attribution.")
    recover.add_argument("transaction_id")
    recover.add_argument("--mode", choices=("inspect", "resume", "compensate"), default="inspect")
    for parser in (apply, recover):
        parser.add_argument("--approve-plan-id")
        parser.add_argument("--permanent-delete", action="store_true", help="Additional explicit authorization for permanent payload deletion; required even with exact plan approval.")

    grouped = leaf(commands, "batch", help="Recoverable per-child transactions; not simultaneous cross-target atomic visibility.")
    batch = grouped.add_subparsers(dest="batch_command", required=True)
    plan = leaf(batch, "plan")
    plan.add_argument("plans", type=Path, nargs="+", help="One to fifty already reviewed core plan files.")
    plan.add_argument("--plan-out", type=Path)
    apply = leaf(batch, "apply")
    apply.add_argument("plan", type=Path)
    apply.add_argument("--batch-id")
    inspect = leaf(batch, "inspect")
    inspect.add_argument("batch_id")
    resume = leaf(batch, "resume")
    resume.add_argument("batch_id")
    compensation = leaf(batch, "compensate-plan", help="Create a new inverse plan; unsupported children stay explicitly uncompensated.")
    compensation.add_argument("batch_id")
    compensation.add_argument("--plan-out", type=Path)
    for parser in (apply, resume):
        parser.add_argument("--approve-plan-id")
        _attribution(parser)
    return leaves


def configure(parser):
    parser.description = "Plan-first version-bound Skill management. Checks are not semantic-safety or continuous-enforcement guarantees."
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--format", choices=("text", "json"), default="text")
    commands = parser.add_subparsers(dest="lifecycle_command", required=True)
    plan = commands.add_parser("plan", help="Inspect a bounded operation without changing installation pointers.")
    plan.add_argument("operation", choices=sorted(OPERATIONS))
    for argument in ("source", "target", "legacy-receipt", "plan-out"):
        plan.add_argument("--" + argument, type=Path)
    for argument in ("installation-id", "skill-id", "name", "version-id"):
        plan.add_argument("--" + argument)
    apply = commands.add_parser("apply", help="Execute an exact saved plan with explicit approval.")
    apply.add_argument("plan", type=Path)
    apply.add_argument("--approve-plan-id")
    apply.add_argument("--transaction-id")
    apply.add_argument("--actor", default="local-operator", help="Local attribution, not authenticated identity.")
    verify = commands.add_parser("verify", help="Observe integrity and persist failed authorization.")
    verify.add_argument("installation_id")
    status = commands.add_parser("status", help="Read cached evidence without initializing state.")
    status.add_argument("installation_id")
    flight = commands.add_parser("preflight", help="Refresh verification before deciding proceed/warn/block.")
    flight.add_argument("installation_id")
    flight.add_argument("--cached", action="store_true", help="Diagnostic cached evidence only; no fresh verification.")
    for command in (status, flight):
        command.add_argument("--max-age-seconds", type=int, default=300)
    inspect = commands.add_parser("inspect", help="Read historical records, not proof of current health.")
    inspect.add_argument("kind", choices=("transaction", "installation", "skill", "receipt", "version", "grant", "verification", "incident"))
    inspect.add_argument("identifier")
    recover = commands.add_parser("recover", help="Inspect by default; recovery requires exact plan approval.")
    recover.add_argument("transaction_id")
    recover.add_argument("--mode", choices=("inspect", "resume", "compensate"), default="inspect")
    recover.add_argument("--approve-plan-id")
    recover.add_argument("--actor", default="local-operator", help="Local attribution label, not authenticated identity.")
    listing = commands.add_parser("list", help="List managed installation identities.")
    listing.add_argument("--pending", action="store_true", help="Read-only paged discovery of unfinished core, batch and retention intents.")
    listing.add_argument("--limit", type=int)
    listing.add_argument("--after-kind")
    listing.add_argument("--after-id")
    for command in [plan, apply, verify, status, flight, inspect, recover, listing] + _consumers(commands):
        command.add_argument("--format", choices=("text", "json"), default=argparse.SUPPRESS)
    return parser


def register(subparsers):
    return configure(subparsers.add_parser("lifecycle", help="Managed Skill lifecycle and explicit transaction recovery."))


def _read_object(path):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate object field")
            result[key] = value
        return result

    def nonfinite(value):
        raise ValueError("non-finite JSON value")

    try:
        descriptor = os.open(str(path), os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as handle:
            information = os.fstat(handle.fileno())
            if not stat.S_ISREG(information.st_mode) or information.st_size > 2 * 1024 * 1024:
                raise ValueError("input must be a regular JSON file, at most 2 MiB")
            data = handle.read(2 * 1024 * 1024 + 1)
            if len(data) > 2 * 1024 * 1024:
                raise ValueError("input exceeded its size bound")
        result = json.loads(data, object_pairs_hook=pairs, parse_constant=nonfinite)
        if not isinstance(result, dict):
            raise ValueError("input must be a JSON object")
        pending = [(result, 0)]
        while pending:
            value, depth = pending.pop()
            if isinstance(value, (dict, list)):
                if depth > 64:
                    raise ValueError("JSON nesting exceeds 64 container levels")
                pending.extend((child, depth + 1) for child in (value.values() if isinstance(value, dict) else value))
        return result
    except (OSError, ValueError, UnicodeError, RecursionError) as error:
        raise LifecycleError("invalid_input", "Cannot read a safe JSON object: {}".format(str(error)[:500]), exit_code=2) from error


def _save_plan(manager, plan, output):
    # Match atomic_json's actual encoding, not the compact stdout document.
    serialized = canonical_json(plan) + "\n"
    if len(serialized.encode("utf-8")) > 2 * 1024 * 1024:
        raise LifecycleError("plan_output_too_large", "Saved plans must fit the 2 MiB input bound. Reduce exact selectors or batch size and generate a new plan.", exit_code=2)
    destination = canonical_entry(output)
    boundaries = [manager.state_root]

    def add_path(value):
        if not isinstance(value, str) or not value or not Path(value).is_absolute():
            raise LifecycleError("unsafe_plan_output", "Unknown or malformed plan path; refusing to save.")
        boundaries.append(Path(value))

    def protected_paths(value, depth=0):
        if not isinstance(value, dict) or depth > 1:
            raise LifecycleError("unsafe_plan_output", "Unknown nested plan shape; refusing to save.")
        schema = value.get("schema_version")
        if schema == "skills-auditor-lifecycle-plan/v1":
            add_path(value["after"]["target"])
            if value["source"] is not None:
                add_path(value["source"])
        elif schema == "skills-auditor-invocation-override-plan/v1":
            add_path(value["target"])
        elif schema == "skills-auditor-lifecycle-batch-plan/v1":
            if depth or not isinstance(value.get("children"), list) or len(value["children"]) > 50:
                raise LifecycleError("unsafe_plan_output", "Unknown batch child shape; refusing to save.")
            for child in value["children"]:
                protected_paths(child["plan"], depth + 1)
        elif schema != "skills-auditor-lifecycle-retention-plan/v1":
            raise LifecycleError("unsafe_plan_output", "Unsupported plan output type; refusing to save.")

    protected_paths(plan)
    boundaries.extend(Path(record["data"]["target"]) for record in manager.repository.list("installation"))
    boundaries.extend(Path(record["data"]["source"]) for record in manager.repository.list("version"))
    for boundary in boundaries:
        if any(_overlap(destination, variant) for variant in (canonical_entry(boundary), boundary.resolve(strict=False))):
            raise LifecycleError("unsafe_plan_output", "Plan output must be outside candidates, managed state and installation entries.")
    if destination.is_symlink() or destination.exists() and not destination.is_file():
        raise LifecycleError("unsafe_plan_output", "Plan output must be a regular artifact file, not a symlink or directory.")
    return atomic_json(destination, plan)


def _exit_code(error):
    if error.code in {"stale_plan", "stale_batch", "retention_stale_plan"}:
        return 4
    if error.code in {"invalid_plan", "invalid_operation", "invalid_actor", "invalid_arguments", "invalid_input", "invalid_batch_plan", "retention_invalid_plan", "retention_invalid_input"}:
        return 2
    return error.exit_code


def _save_result(manager, result, arguments):
    if getattr(arguments, "plan_out", None):
        _save_plan(manager, result, arguments.plan_out)
    return result, 0


def _consumer_execute(manager, arguments):
    command = arguments.lifecycle_command
    if command == "incidents":
        return {"schema_version": "skills-auditor-lifecycle-incident-list/v1", "incidents": incidents.list_incidents(manager, installation_id=arguments.installation_id, state=arguments.state)}, 0
    if command == "investigate":
        return incidents.investigate(manager, arguments.incident_id, limit=arguments.limit, after_sequence=arguments.after_sequence), 0
    if command == "append-note":
        references = []
        for value in arguments.evidence_ref:
            kind, separator, identifier = value.partition(":")
            if not separator or not kind or not identifier:
                raise LifecycleError("invalid_input", "Evidence references must name an existing local KIND:ID, not a file path.", exit_code=2)
            references.append({"kind": kind, "id": identifier})
        return incidents.append_note(manager, arguments.incident_id, arguments.text, actor=arguments.actor, tool=arguments.tool, evidence_refs=references, event_id=arguments.event_id), 0
    if command == "resolve":
        return incidents.resolve(manager, arguments.incident_id, actor=arguments.actor, tool=arguments.tool,
                                 verification_id=arguments.verification_id, disposition=arguments.disposition, explanation=arguments.explanation), 0
    if command == "supersede":
        return incidents.supersede(manager, arguments.incident_id, arguments.replacement_id, explanation=arguments.explanation, actor=arguments.actor, tool=arguments.tool), 0
    if command == "invocation":
        operation = arguments.invocation_command
        if operation == "select":
            result = invocation.select(manager, arguments.installation_id, policy=arguments.policy, refresh=not arguments.cached,
                                       override_id=arguments.override_id, max_age_seconds=arguments.max_age_seconds, actor=arguments.actor, tool=arguments.tool)
            return result, result["exit_code"]
        if operation == "override-plan":
            return _save_result(manager, invocation.plan_override(manager, arguments.installation_id, reason=arguments.reason,
                                ttl_seconds=arguments.ttl_seconds, max_age_seconds=arguments.max_age_seconds), arguments)
        if operation == "override-apply":
            return invocation.apply_override(manager, _read_object(arguments.plan), approve_plan_id=arguments.approve_plan_id, actor=arguments.actor, tool=arguments.tool), 0
        if operation == "override-revoke":
            return invocation.revoke_override(manager, arguments.override_id, reason=arguments.reason, actor=arguments.actor, tool=arguments.tool), 0
        if operation == "override-get":
            return invocation.get_override(manager, arguments.override_id), 0
    if command == "retention":
        operation = arguments.retention_command
        if operation == "plan":
            plan = retention.plan_retention(manager, arguments.operation, receipt_ids=arguments.receipt_id, incident_ids=arguments.incident_id,
                                           pin_version_ids=[] if arguments.clear_pins else arguments.pin_version_id, keep_recent=arguments.keep_recent,
                                           object_ids=arguments.object_id, stage_names=arguments.stage_name, grace_seconds=arguments.grace_seconds)
            return _save_result(manager, plan, arguments)
        if operation == "apply":
            return retention.apply_retention(manager, _read_object(arguments.plan), approve_plan_id=arguments.approve_plan_id,
                                             permanent_delete=arguments.permanent_delete, transaction_id=arguments.transaction_id, actor=arguments.actor), 0
        if operation == "recover":
            return retention.recover_retention(manager, arguments.transaction_id, mode=arguments.mode,
                                               approve_plan_id=arguments.approve_plan_id, permanent_delete=arguments.permanent_delete), 0
    if command == "batch":
        batch = BatchManager(manager)
        operation = arguments.batch_command
        if operation == "plan":
            if len(arguments.plans) > 50:
                raise LifecycleError("invalid_batch_plan", "A batch contains at most fifty saved core plans.", exit_code=2)
            return _save_result(manager, batch.plan([_read_object(path) for path in arguments.plans]), arguments)
        if operation == "apply":
            return batch.apply(_read_object(arguments.plan), approve_plan_id=arguments.approve_plan_id, batch_id=arguments.batch_id, actor=arguments.actor), 0
        if operation == "inspect":
            return batch.inspect(arguments.batch_id), 0
        if operation == "resume":
            return batch.recover(arguments.batch_id, mode="resume", approve_plan_id=arguments.approve_plan_id, actor=arguments.actor), 0
        if operation == "compensate-plan":
            return _save_result(manager, batch.plan_compensation(arguments.batch_id), arguments)
    raise LifecycleError("invalid_arguments", "Unsupported lifecycle consumer command.", exit_code=2)


def execute(arguments):
    command = arguments.lifecycle_command
    # Validate requested ownership before any operation is allowed to create
    # state. An invalid root must not reach a second error while rendering.
    requested_context = project_context(arguments.project_root)
    arguments.project_root = requested_context["project_root"]
    try:
        manager = Manager(arguments.project_root, create=command in {"plan", "apply"})
    except LifecycleError as error:
        if command in {"status", "preflight"}:
            status = unknown_status(arguments.installation_id, reason=error.code, max_age_seconds=arguments.max_age_seconds,
                                    project_root=arguments.project_root)
            return (status if command == "status" else {"decision": "block", "exit_code": 3, "status": status}), 3
        raise
    try:
        arguments._context = manager_context(manager)
        if command == "plan":
            legacy = _read_object(arguments.legacy_receipt) if arguments.legacy_receipt else None
            plan = manager.plan(arguments.operation, source=arguments.source, target=arguments.target,
                                installation_id=arguments.installation_id, skill_id=arguments.skill_id,
                                name=arguments.name, version_id=arguments.version_id, legacy_receipt=legacy)
            if arguments.plan_out:
                _save_plan(manager, plan, arguments.plan_out)
            return plan, 0
        if command == "apply":
            return manager.apply(_read_object(arguments.plan), approve_plan_id=arguments.approve_plan_id,
                                 transaction_id=arguments.transaction_id, actor=arguments.actor), 0
        if command == "verify":
            result = manager.verify(arguments.installation_id)
            return result, 0 if result["valid"] else 3
        if command == "status":
            result = read_status(manager, arguments.installation_id, max_age_seconds=arguments.max_age_seconds)
            return result, {"ok": 0, "warning": 4, "error": 3}[result["severity"]]
        if command == "preflight":
            result = preflight(manager, arguments.installation_id, refresh=not arguments.cached, max_age_seconds=arguments.max_age_seconds)
            return result, result["exit_code"]
        if command == "inspect":
            if arguments.kind == "incident":
                return incidents.get_incident(manager, arguments.identifier), 0
            record = manager.repository.get(arguments.kind, arguments.identifier)
            if record is None:
                raise LifecycleError("record_missing", "Requested historical record does not exist.")
            return record["data"], 0
        if command == "recover":
            return manager.recover(arguments.transaction_id, mode=arguments.mode, approve_plan_id=arguments.approve_plan_id, actor=arguments.actor), 0
        if command == "list":
            if arguments.pending:
                return list_pending(manager, limit=50 if arguments.limit is None else arguments.limit,
                                    after_kind=arguments.after_kind, after_id=arguments.after_id), 0
            if any(value is not None for value in (arguments.limit, arguments.after_kind, arguments.after_id)):
                raise LifecycleError("invalid_pending_input", "Pending pagination requires --pending.", exit_code=2)
            return {"schema_version": "skills-auditor-lifecycle-list/v1", "installations": manager.list_installations()}, 0
        return _consumer_execute(manager, arguments)
    finally:
        manager.repository.close()


def _recovery_text(payload, project_root=None):
    """Project only a bounded recovery ID; never print nested error evidence.

    Parent batch recovery takes precedence over a child transaction. Every
    suggested command is read-only; none grants resume or deletion approval.
    """
    details = payload.get("details")
    if not isinstance(details, dict):
        return ""
    kind = "batch_id" if "batch_id" in details else "transaction_id"
    identifier = details.get(kind)
    if (not isinstance(identifier, str) or not identifier or len(identifier) > 200
            or identifier in {".", ".."} or any(ord(char) < 32 or ord(char) == 127 or char in "/\\" for char in identifier)):
        return ""
    command = []
    if kind == "batch_id":
        command += ["batch", "inspect"] + (["--"] if identifier.startswith("-") else []) + [identifier]
    else:
        if payload.get("code", "").startswith("retention_") or details.get("kind") == "retention-transaction":
            command.append("retention")
        command += (["recover", "--mode", "inspect", "--", identifier] if identifier.startswith("-")
                    else ["recover", identifier, "--mode", "inspect"])
    return "\nRecovery reference: {}={}\nNext (read-only): {}\nInspect recorded state first. Resume or compensate only with explicit approval; permanent purge also requires its separate deletion authorization.".format(
        kind, shlex.quote(identifier), action(project_root, command)["command"])


def _render(payload, *, project_root=None):
    if payload.get("schema_version") == "skills-auditor-invocation/v1":
        text = "{} Decision: {}\n{}\nInvocation reasons: {}\nSnapshot: {} (selected only; not executed)".format(
            {"proceed": "[OK]", "warn": "[WARN]", "block": "[BLOCK]"}[payload["decision"]], payload["decision"],
            render_status(payload["status"]), ", ".join(payload["reason_codes"]) or "none", payload["snapshot_path"] or "none")
        if payload.get("override"):
            text += "\n[OVERRIDE] used={} id={} expires={} reason={}".format(payload["override"]["used"], payload["override"]["override_id"], payload["override"]["expires_at"], payload["override"]["reason"])
        return text + "\n" + payload["notice"]
    if "decision" in payload and "status" in payload:
        return render_status(payload["status"]) + "\nDecision: " + payload["decision"]
    if "severity" in payload and "approval" in payload:
        return render_status(payload)
    if payload.get("schema_version") == "skills-auditor-lifecycle-error/v1":
        return "[BLOCK] {}: {}\nProject: {} (context verified={})".format(payload["code"], payload["message"],
            payload.get("project_root", project_root) or "unknown", payload.get("context_verified", False)) + _recovery_text(payload, project_root)
    if "approval" in payload:
        identifier = payload["installation_id"]
        navigation = action(project_root, ["status"] + (["--"] if identifier.startswith("-") else []) + [identifier])
        return "{} approval={}\nReasons: {}\nNext: {}\nRead current status, then generate a fresh lifecycle plan when required, review it, and explicitly approve its exact plan ID.\n{}".format(
            "[OK]" if payload.get("valid") else "[BLOCK]", payload["approval"]["state"],
            ", ".join(payload["approval"]["reason_codes"]) or "none", navigation["command"], json.dumps(payload, ensure_ascii=False, indent=2))
    if "plan_id" in payload and payload.get("schema_version", "").endswith("plan/v1"):
        return "[PLAN] {} {}\nReview sources, targets and before/after states; approval must name this exact plan ID.\n{}".format(payload.get("operation", payload["schema_version"]), payload["plan_id"], json.dumps(payload, ensure_ascii=False, indent=2))
    return json.dumps(payload, ensure_ascii=False, indent=2)


def main(argv=None, *, prog="skills-audit lifecycle"):
    argv = list(sys.argv[1:] if argv is None else argv)
    output_format = "text"
    project_root = None
    arguments = None
    for index, argument in enumerate(argv):
        if argument == "--format" and index + 1 < len(argv):
            output_format = argv[index + 1]
        elif argument.startswith("--format="):
            output_format = argument.split("=", 1)[1]
    try:
        arguments = configure(_Parser(prog=prog)).parse_args(argv)
        output_format = arguments.format
        project_root = arguments.project_root
        payload, code = execute(arguments)
    except LifecycleError as error:
        payload, code = error.to_dict(), _exit_code(error)
    except RecursionError:
        error = LifecycleError("invalid_input", "JSON nesting exceeds the supported input bound.", exit_code=2)
        payload, code = error.to_dict(), 2
    except (OSError, ValueError, TypeError, KeyError) as error:
        error = LifecycleError("operation_failed", "Managed operation failed; inspect recorded transactions and current state before retrying: {}".format(str(error)[:500]))
        payload, code = error.to_dict(), 3
    if payload.get("schema_version") == "skills-auditor-lifecycle-error/v1":
        try:
            context = ({"project_root": payload["details"].get("project_root"), "context_verified": False}
                       if payload["code"] == "project_context_changed" else
                       getattr(arguments, "_context", None) or project_context(project_root))
            payload = {**payload, **context}
        except (LifecycleError, OSError, ValueError, TypeError, RuntimeError):
            payload = {**payload, "project_root": None, "context_verified": False}
        project_root = payload["project_root"]
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True) if output_format == "json" else _render(payload, project_root=project_root))
    return code
