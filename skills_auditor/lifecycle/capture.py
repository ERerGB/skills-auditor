"""Immutable, explicitly requested capture diagnostics; never authorization.

Only allowlisted health metadata is retained. Sensor records, tool inputs,
settings paths and free-form error descriptions never enter this repository.
"""

from datetime import datetime
import os
from pathlib import Path
import re
import uuid

from .. import skill_trace
from .common import LifecycleError, canonical_json, digest, utc_now
from .context import manager_context


SCHEMA_VERSION = "skills-auditor-lifecycle-capture-evidence/v1"
MAX_EVIDENCE_BYTES = 32768
_FIELDS = {"schema_version", "evidence_id", "project_root", "context_verified", "task_cwd", "session_id",
           "log_root", "observed_at", "preference", "health", "host_trust", "actor", "tool", "completion_event_sequence"}
_SCOPE = ("project_root", "task_cwd", "session_id", "log_root", "actor", "tool")
_HOOKS = {"SessionStart", "PreToolUse", "PostToolUse", "Stop"}
_STATES = {"disabled", "healthy", "unverified", "stale", "error"}
_SOURCES = {"default", "environment", "file", "unknown"}


def _text(value, limit=200):
    return (isinstance(value, str) and bool(value.strip()) and len(value) <= limit
            and not any(ord(char) < 32 or ord(char) == 127 for char in value))


def _identifier(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}", value) is not None


def _path(value):
    if (not _text(value, 4096) or not Path(value).is_absolute()
            or os.path.normpath(value) != value or ".." in Path(value).parts):
        raise ValueError("invalid absolute path")
    return value


def _timestamp(value):
    if (not isinstance(value, str) or len(value) > 40
            or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})", value)):
        raise ValueError("invalid timestamp")
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.utcoffset() is None:
        raise ValueError("timestamp is not timezone aware")
    return stamp


def _validate(value, evidence_id, project_root):
    if len(canonical_json(value).encode("utf-8")) > MAX_EVIDENCE_BYTES:
        raise ValueError("capture evidence exceeds its serialized bound")
    if (type(value) is not dict or set(value) != _FIELDS or value["schema_version"] != SCHEMA_VERSION
            or value["evidence_id"] != evidence_id or not _identifier(evidence_id)
            or value["context_verified"] is not True or value["project_root"] != project_root
            or value["host_trust"] != "unverified" or not _text(value["actor"]) or not _text(value["tool"])
            or type(value["completion_event_sequence"]) is not int or value["completion_event_sequence"] < 1):
        raise ValueError("invalid capture envelope")
    for key in ("project_root", "task_cwd", "log_root"):
        _path(value[key])
    if value["session_id"] is not None and not _text(value["session_id"]):
        raise ValueError("invalid task session")
    observed = _timestamp(value["observed_at"])
    preference, health = value["preference"], value["health"]
    if (type(preference) is not dict or set(preference) != {"enabled", "source", "updated_at"}
            or preference["enabled"] is not None and type(preference["enabled"]) is not bool
            or not isinstance(preference["source"], str) or preference["source"] not in _SOURCES):
        raise ValueError("invalid capture preference")
    if preference["updated_at"] is not None:
        _timestamp(preference["updated_at"])
    if ((preference["source"] == "unknown") != (preference["enabled"] is None)
            or (preference["source"] == "file") != (preference["updated_at"] is not None)):
        raise ValueError("capture preference provenance disagrees")
    if (type(health) is not dict or set(health) != {"status", "observed_hooks"}
            or not isinstance(health["status"], str) or health["status"] not in _STATES
            or type(health["observed_hooks"]) is not dict or not set(health["observed_hooks"]) <= _HOOKS):
        raise ValueError("invalid capture health")
    for stamp in health["observed_hooks"].values():
        if _timestamp(stamp) > observed:
            raise ValueError("future capture observation")
    state, hooks, enabled = health["status"], health["observed_hooks"], preference["enabled"]
    if state == "error":
        return
    if preference["source"] == "unknown" or (state == "disabled") != (enabled is False):
        raise ValueError("capture health disagrees with preference")
    if state in {"disabled", "unverified"} and hooks:
        raise ValueError("unobserved capture has hook evidence")
    if state in {"healthy", "stale"} and (value["session_id"] is None or not hooks):
        raise ValueError("capture health lacks task observations")
    if state == "healthy" and not {"PreToolUse", "PostToolUse"} <= set(hooks):
        raise ValueError("healthy capture lacks both tool hooks")


def _event_payload(value):
    body = {key: item for key, item in value.items() if key != "completion_event_sequence"}
    return {"evidence_id": value["evidence_id"], "evidence_sha256": digest(body)}


def get_record(repository, evidence_id, project_root):
    """Validate immutable local evidence using metadata only, never sensor I/O."""
    if not _identifier(evidence_id):
        raise LifecycleError("invalid_capture_input", "Capture evidence ID must be a bounded identifier.", exit_code=2)
    try:
        project_root = _path(os.fspath(project_root))
        if str(repository.root) != str(Path(project_root) / ".skills-auditor-local" / "lifecycle"):
            raise ValueError("repository belongs to a different project")
        row = repository.get("capture-evidence", evidence_id)
        if row is None:
            raise LifecycleError("capture_evidence_missing", "No capture evidence with this ID exists in the selected project.",
                                 details={"evidence_id": evidence_id})
        if row["kind"] != "capture-evidence" or row["id"] != evidence_id or type(row["revision"]) is not int or row["revision"] != 1:
            raise ValueError("capture evidence is not its immutable first revision")
        value = row["data"]
        _validate(value, evidence_id, project_root)
        events = repository.events("capture-evidence:" + evidence_id, limit=2)
        if len(events) != 1:
            raise ValueError("capture completion event missing or duplicated")
        event = events[0]
        expected = {"stream": "capture-evidence:" + evidence_id, "event_type": "capture_evidence_recorded",
                    "sequence": value["completion_event_sequence"], "actor": value["actor"], "tool": value["tool"],
                    "payload": _event_payload(value)}
        if any(event.get(key) != item for key, item in expected.items()):
            raise ValueError("capture completion event disagrees")
        _timestamp(event["created_at"])
        return value
    except LifecycleError as error:
        if error.code == "capture_evidence_missing":
            raise
        raise LifecycleError("capture_evidence_invalid", "Capture evidence cannot be validated against its immutable local completion proof.",
                             details={"evidence_id": evidence_id}) from error
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as error:
        raise LifecycleError("capture_evidence_invalid", "Capture evidence cannot be validated against its immutable local completion proof.",
                             details={"evidence_id": evidence_id}) from error


def get(manager, evidence_id):
    """A read does not initialize state, resample capture or follow saved paths."""
    context = manager_context(manager)
    value = get_record(manager.repository, evidence_id, context["project_root"])
    manager_context(manager)
    return value


def _scope(manager, log_dir, actor, tool):
    context = manager_context(manager)
    try:
        if not _text(actor) or not _text(tool):
            raise ValueError("invalid attribution")
        if log_dir is not None and not _text(os.fspath(log_dir), 4096):
            raise ValueError("invalid log root")
        cwd = _path(str(Path.cwd().resolve()))
        session = os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_SESSION_ID") or None
        if session is not None and not _text(session):
            raise ValueError("invalid task session")
        log_root = _path(str(skill_trace.log_root(Path(cwd), log_dir).resolve()))
        return {"project_root": context["project_root"], "task_cwd": cwd, "session_id": session,
                "log_root": log_root, "actor": actor, "tool": tool}
    except (OSError, ValueError, TypeError, RuntimeError) as error:
        raise LifecycleError("invalid_capture_input", "Capture request context must contain bounded local paths and attribution.", exit_code=2) from error


def _sample(scope):
    try:
        result = skill_trace.check_health(log_dir=scope["log_root"], session_id=scope["session_id"])
        if (type(result) is not dict or result.get("session_id") != (scope["session_id"] or "")
                or result.get("log_dir") != scope["log_root"]):
            raise ValueError("capture result context differs from the request")
        updated_at = result.get("updated_at")
        preference = {"enabled": result.get("enabled"), "source": result.get("source", "unknown"),
                      "updated_at": None if updated_at == "" else updated_at}
        return {"observed_at": utc_now(), "preference": preference,
                "health": {"status": result["status"], "observed_hooks": result["observed_hooks"]}}
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as error:
        raise LifecycleError("invalid_capture_result", "Capture health returned no valid bounded diagnostic snapshot.") from error


def record(manager, *, evidence_id=None, log_dir=None, actor="local-observer", tool="lifecycle"):
    """Explicitly save one advisory observation; same scoped retry never samples."""
    if evidence_id is None:
        evidence_id = uuid.uuid4().hex
    if not _identifier(evidence_id):
        raise LifecycleError("invalid_capture_input", "Capture evidence ID must be a bounded identifier.", exit_code=2)
    scope = _scope(manager, log_dir, actor, tool)
    try:
        with manager.repository.atomic():
            if manager.repository.get("capture-evidence", evidence_id) is not None:
                previous = get_record(manager.repository, evidence_id, scope["project_root"])
                if any(previous[key] != scope[key] for key in _SCOPE):
                    raise LifecycleError("capture_evidence_conflict", "This capture evidence ID already belongs to a different scoped request.",
                                         details={"evidence_id": evidence_id})
                manager_context(manager)
                return previous
            if manager.repository.events("capture-evidence:" + evidence_id, limit=1):
                raise LifecycleError("capture_evidence_invalid", "An existing capture completion event has lost its immutable record.",
                                     details={"evidence_id": evidence_id})
            value = {"schema_version": SCHEMA_VERSION, "evidence_id": evidence_id, **scope, **_sample(scope),
                     "context_verified": True, "host_trust": "unverified", "completion_event_sequence": 1}
            try:
                _validate(value, evidence_id, scope["project_root"])
            except (ValueError, TypeError, KeyError, AttributeError, RecursionError) as error:
                raise LifecycleError("invalid_capture_result", "Capture health returned no valid bounded diagnostic snapshot.") from error
            if _scope(manager, log_dir, actor, tool) != scope:
                raise LifecycleError("capture_context_changed", "Task capture context changed while sampling; no evidence was recorded.")
            event = manager.repository.append_event("capture-evidence:" + evidence_id, "capture_evidence_recorded",
                                                     _event_payload(value), actor, tool)
            value["completion_event_sequence"] = event["sequence"]
            _validate(value, evidence_id, scope["project_root"])
            manager_context(manager)
            manager.repository.put("capture-evidence", evidence_id, value)
            # A successful adapter response is not the persisted proof. Check
            # both rows before the outer transaction commits or reports success.
            get_record(manager.repository, evidence_id, scope["project_root"])
            manager_context(manager)
        return value
    except LifecycleError as error:
        if error.code in {"capture_evidence_conflict", "capture_evidence_invalid", "invalid_capture_result", "capture_context_changed", "project_context_changed"}:
            raise
        raise LifecycleError("capture_evidence_write_failed", "Capture evidence could not be committed; retry the same scoped evidence ID.",
                             details={"evidence_id": evidence_id}) from error
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as error:
        raise LifecycleError("capture_evidence_write_failed", "Capture evidence could not be committed; retry the same scoped evidence ID.",
                             details={"evidence_id": evidence_id}) from error
