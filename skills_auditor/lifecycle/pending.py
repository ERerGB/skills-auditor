"""Read-only discovery of project-local unfinished intents, never recovery.

Discovery record fetches and returned pages are bounded. Finding pending rows,
parent references and their proofs can scan historical metadata. Existing
batch proof readers may load a whole event stream; this is not a globally
bounded-memory or constant-I/O catalog, or a filesystem/stage scanner. Cursors
order by kind and ID, not wall-clock time.
"""

from .common import LifecycleError, digest
from .context import action, manager_context


KINDS = ("batch", "retention-transaction", "transaction")
_STATES = {
    "batch": {"prepared", "applying", "recovery_needed", "completed", "compensating", "compensated"},
    "transaction": {"prepared", "applying", "recovery_needed", "completed", "compensated"},
    "retention-transaction": {"prepared", "recovery_needed", "completed", "compensated"},
}


def _identifier(value):
    return (isinstance(value, str) and 0 < len(value) <= 200 and value not in {".", ".."}
            and not any(ord(char) < 32 or ord(char) == 127 or char in "/\\" for char in value))


def _rows(repository, kind, after_id=None):
    while True:
        page = repository.page(kind, limit=100, after_id=after_id)
        for row in page:
            yield row
        if len(page) < 100:
            return
        after_id = page[-1]["id"]


def _has_event(repository, stream, event_type, payload, actor, tool):
    after = 0
    while True:
        events = repository.events(stream, limit=100, after_sequence=after)
        if any(event["event_type"] == event_type and event["payload"] == payload
               and (actor is None or event["actor"] == actor) and event["tool"] == tool for event in events):
            return True
        if len(events) < 100:
            return False
        after = events[-1]["sequence"]


def _core_terminal(manager, tx):
    """Terminal classification needs retained facts, never current bytes."""
    state, plan = tx["state"], tx["plan"]
    if state not in {"completed", "compensated"}:
        return
    if any(step["state"] != state for step in tx["steps"]):
        raise ValueError("terminal intent has unfinished steps")
    if state == "compensated":
        # Recovery may have a different local actor than the original intent.
        if tx["receipt_id"] is not None or not _has_event(manager.repository, tx["transaction_id"], "recovery_authorized",
                {"mode": "compensate", "plan_id": plan["plan_id"]}, None, "lifecycle"):
            raise ValueError("compensated intent lacks explicit recovery authorization")
        return
    receipt = manager.repository.get("receipt", tx["receipt_id"])
    grant_id = tx["grant_id"] or plan["after"]["authorization"]["grant_id"]
    expected = {"schema_version": "skills-auditor-lifecycle-receipt/v1", "status": "completed",
                "receipt_id": tx["receipt_id"], "transaction_id": tx["transaction_id"], "plan_id": plan["plan_id"],
                "operation": plan["operation"], "installation_id": plan["installation_id"],
                "skill_id": plan["skill"]["skill_id"], "version_id": plan["version"]["version_id"],
                "grant_id": grant_id, "steps": tx["steps"]}
    if not receipt or any(receipt["data"].get(key) != value for key, value in expected.items()):
        raise ValueError("completed intent lacks its exact immutable receipt")
    if not _has_event(manager.repository, plan["installation_id"], "transaction_completed",
                      {"transaction_id": tx["transaction_id"], "receipt_id": tx["receipt_id"], "operation": plan["operation"]},
                      tx["actor"], "lifecycle"):
        raise ValueError("completed intent lacks its append-only completion event")


def _validate(manager, kind, row):
    """Validate metadata without source reads, current-byte checks or effects."""
    value = row["data"]
    try:
        plan = value["plan"]
        key = "batch_id" if kind == "batch" else "transaction_id"
        if (not _identifier(row["id"]) or value[key] != row["id"] or value["state"] not in _STATES[kind]
                or not isinstance(plan, dict) or plan["project_root"] != str(manager.project_root)
                or plan["plan_id"] != digest({key: item for key, item in plan.items() if key != "plan_id"})
                or value["approved_plan_id"] != plan["plan_id"]):
            raise ValueError("intent identity, state, project or exact approval disagrees")
        if kind == "batch":
            from .batch import BatchManager
            value = BatchManager(manager).inspect(row["id"])
        elif kind == "retention-transaction":
            from .retention import _completion_evidence, _validate_transaction
            _validate_transaction(manager, value, row["id"])
            if value["state"] == "completed":
                _completion_evidence(manager, plan["operation"], {"plan_id": plan["plan_id"], "transaction_id": row["id"],
                    "receipt_id": value["receipt_id"], "completion_event_sequence": value.get("completion_event_sequence")})
            elif value["state"] == "compensated":
                if (any(step["state"] != "compensated" for step in value["objects"])
                        or not _has_event(manager.repository, "retention:" + row["id"], "retention_compensated",
                                          {"plan_id": plan["plan_id"]}, value["actor"], "retention")):
                    raise ValueError("compensated retention intent lacks final journal evidence")
        else:
            manager._validate_plan(plan, physical_paths=False, metadata_only=True)
            if (type(value.get("steps")) is not list or len(value["steps"]) != len(plan["steps"])
                    or any(any(step.get(key) != item for key, item in expected.items())
                           for step, expected in zip(value["steps"], plan["steps"]))):
                raise ValueError("core step intent differs from the approved plan")
            _core_terminal(manager, value)
        if value["state"] == "completed" and not _identifier(value.get("receipt_id")):
            raise ValueError("completed intent has no receipt identity")
    except (LifecycleError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as error:
        raise LifecycleError("pending_corrupt", "Cannot safely classify a recorded intent; inspect the owning project state.",
                             details={"kind": kind, "id": row["id"],
                                      "batch_id" if kind == "batch" else "transaction_id": row["id"]}) from error
    return value


def _parents(manager, identifier):
    parents = []
    for row in _rows(manager.repository, "batch"):
        batch = _validate(manager, "batch", row)
        if any(child["transaction_id"] == identifier for child in batch["children"]):
            parents.append({"kind": "batch", "id": row["id"]})
        if len(parents) > 100:
            raise LifecycleError("pending_corrupt", "Intent parent references exceed the bounded discovery contract.")
    return parents


def list_pending(manager, *, limit=50, after_kind=None, after_id=None):
    """List unfinished intents only; missing/corrupt storage is never empty OK."""
    if (type(limit) is not int or not 1 <= limit <= 100 or (after_kind is None) != (after_id is None)
            or (after_kind is not None and (after_kind not in KINDS or not _identifier(after_id)))):
        raise LifecycleError("invalid_pending_input", "Pending limit must be 1–100; cursor needs an exact kind and bounded ID.", exit_code=2)
    manager_context(manager)
    found = []
    for kind in KINDS:
        if after_kind is not None and kind < after_kind:
            continue
        for row in _rows(manager.repository, kind, after_id if kind == after_kind else None):
            value = _validate(manager, kind, row)
            if value["state"] in {"completed", "compensated"}:
                continue
            found.append((kind, row["id"], value))
            if len(found) > limit:
                break
        if len(found) > limit:
            break
    context = manager_context(manager)
    entries = []
    for kind, identifier, value in found[:limit]:
        if kind == "batch":
            arguments = ["batch", "inspect"] + (["--"] if identifier.startswith("-") else []) + [identifier]
        else:
            arguments = ["retention"] if kind == "retention-transaction" else []
            arguments += ["recover", "--mode", "inspect", "--", identifier]
        entries.append({"kind": kind, "id": identifier, "state": value["state"], "plan_id": value["plan"]["plan_id"],
                        "parents": _parents(manager, identifier) if kind == "transaction" else [],
                        "children": [{"kind": "transaction", "id": child["transaction_id"]} for child in value["children"]] if kind == "batch" else [],
                        "inspection": action(context["project_root"], arguments)})
    has_more = len(found) > limit
    manager_context(manager)
    return {"schema_version": "skills-auditor-lifecycle-pending-list/v1", **context,
            "pending": entries, "limit": limit, "has_more": has_more,
            "continuation": {"after_kind": entries[-1]["kind"], "after_id": entries[-1]["id"]} if has_more else None}
