"""Host-neutral use-time selection, not execution, fallback or semantic trust.

Both policies select only the current authorized active snapshot. An explicit
staleness override never excuses failed integrity or changes cached freshness.
Live checks use the engine's durable observation fence; a crash cannot resurrect
cached permission. Decisions and overrides have local, non-authenticated audit
attribution. A host that ignores this adapter is not blocked by this module.
"""

from datetime import datetime, timedelta, timezone
import re
import uuid

from .common import LifecycleError, digest
from .context import manager_context
from .status import DEFAULT_MAX_AGE_SECONDS, read_status, unknown_status


_PREFIX = "skills-auditor-invocation-"
_BINDING = ("installation_id", "skill_id", "version_id", "grant_id", "target", "generation", "receipt_id")
_PLAN_FIELDS = {"schema_version", "project_root", "plan_id", "created_at", "expires_at", "ttl_seconds",
                "reason", "max_age_seconds", "expected_revision", *_BINDING}
_PROJECTION_FIELDS = {"schema_version", "override_id", "state", "plan_id", "expires_at", "created_at",
                      "reason", "max_age_seconds", "approval_receipt_id", "revocation_receipt_id", *_BINDING}


def _error(code, message, *, inputs=False):
    return LifecycleError(code, message, exit_code=2 if inputs else 3)


def _id(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}", value) is not None


def _text(value, maximum):
    try:
        return isinstance(value, str) and bool(value.strip()) and len(value.encode("utf-8")) <= maximum and not any(ord(char) < 32 for char in value)
    except UnicodeError:
        return False


def _labels(actor, tool):
    if not all(_text(value, 200) for value in (actor, tool)):
        raise _error("invalid_invocation_input", "Actor and tool must be bounded local attribution labels.", inputs=True)


def _clock(now=None, max_age_seconds=DEFAULT_MAX_AGE_SECONDS):
    if type(max_age_seconds) is not int or not 1 <= max_age_seconds <= 86400:
        raise _error("invalid_invocation_input", "max_age_seconds must be an integer from 1 to 86400.", inputs=True)
    if now is None:
        now = datetime.now(timezone.utc)
    if type(now) is not datetime or now.tzinfo is None or now.utcoffset() is None:
        raise _error("invalid_invocation_input", "now must be a timezone-aware datetime.", inputs=True)
    return now.astimezone(timezone.utc)


def _stamp(now):
    return now.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})", value):
        raise ValueError("invalid timestamp")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.utcoffset() is None:
        raise ValueError("missing timestamp timezone")
    return result


def _binding(installation):
    return {key: installation["authorization"]["grant_id"] if key == "grant_id" else installation[key] for key in _BINDING}


def _checked(manager, installation_id, now, max_age_seconds):
    """Capture identity, use the shared live fence, then require a final recheck."""
    record = manager.repository.get("installation", installation_id)
    status = read_status(manager, installation_id, now=now, max_age_seconds=max_age_seconds)
    if (not record or status["approval"]["state"] != "valid" or status["lifecycle_state"] != "active"
            or status["freshness"]["state"] not in {"fresh", "stale"}):
        raise _error("invocation_blocked", "Current active approval and coherent completed evidence are required.")
    if not manager.check_cached_integrity(installation_id):
        raise _error("invocation_integrity_failed", "Current snapshot or installation evidence failed verification.")
    return record


def _unchanged(manager, record, now, max_age_seconds):
    installation_id = record["data"]["installation_id"]
    if manager.repository.get("installation", installation_id) != record:
        raise _error("invocation_changed", "Installation changed during selection; check again.")
    status = read_status(manager, installation_id, now=now, max_age_seconds=max_age_seconds)
    if status["approval"]["state"] != "valid" or status["lifecycle_state"] != "active" or status["freshness"]["state"] not in {"fresh", "stale"}:
        raise _error("invocation_blocked", "Completed authorization evidence changed during selection.")
    return status


def _validate_plan(plan):
    try:
        if not isinstance(plan, dict) or set(plan) != _PLAN_FIELDS or plan["schema_version"] != _PREFIX + "override-plan/v1":
            raise ValueError("plan fields")
        if plan["plan_id"] != digest({key: value for key, value in plan.items() if key != "plan_id"}):
            raise ValueError("plan checksum")
        if not all(_id(plan[key]) for key in ("installation_id", "skill_id", "grant_id", "receipt_id")):
            raise ValueError("record identity")
        if not isinstance(plan["version_id"], str) or not re.fullmatch(r"[a-f0-9]{64}", plan["version_id"]):
            raise ValueError("version identity")
        if not all(_text(plan[key], 4096) and plan[key].startswith("/") for key in ("project_root", "target")):
            raise ValueError("absolute paths")
        if any(type(plan[key]) is not int or plan[key] < 1 for key in ("generation", "expected_revision")):
            raise ValueError("revision or generation")
        if type(plan["ttl_seconds"]) is not int or not 1 <= plan["ttl_seconds"] <= 3600 or not _text(plan["reason"], 1024):
            raise ValueError("bounded expiry and reason")
        if type(plan["max_age_seconds"]) is not int or not 1 <= plan["max_age_seconds"] <= 86400:
            raise ValueError("freshness policy")
        if (_date(plan["expires_at"]) - _date(plan["created_at"])).total_seconds() != plan["ttl_seconds"]:
            raise ValueError("expiry does not match approved TTL")
    except (KeyError, ValueError, TypeError, AttributeError, LifecycleError) as error:
        raise _error("invalid_override_plan", "Override plan is malformed or its checksum does not match.", inputs=True) from error


def plan_override(manager, installation_id, *, reason, ttl_seconds=300, now=None, max_age_seconds=DEFAULT_MAX_AGE_SECONDS):
    """Inspect a stale-observation exception; no override exists until approval."""
    now = _clock(now, max_age_seconds)
    if not _id(installation_id) or type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 3600 or not _text(reason, 1024):
        raise _error("invalid_invocation_input", "A valid installation, bounded reason and integer TTL from 1 to 3600 are required.", inputs=True)
    record = _checked(manager, installation_id, now, max_age_seconds)
    with manager.repository.atomic():
        status = _unchanged(manager, record, now, max_age_seconds)
        if status["freshness"]["state"] != "stale":
            raise _error("override_not_applicable", "Only an otherwise valid stale cached observation may be overridden.")
        plan = {"schema_version": _PREFIX + "override-plan/v1", "project_root": str(manager.project_root),
                **_binding(record["data"]), "expected_revision": record["revision"], "reason": reason,
                "created_at": _stamp(now), "expires_at": _stamp(now + timedelta(seconds=ttl_seconds)),
                "ttl_seconds": ttl_seconds, "max_age_seconds": max_age_seconds}
        plan["plan_id"] = digest(plan)
        return plan


def _projection(plan, override_id, receipt_id):
    return {"schema_version": _PREFIX + "override/v1", "override_id": override_id,
            **{key: plan[key] for key in _BINDING}, "state": "active", "plan_id": plan["plan_id"],
            "expires_at": plan["expires_at"], "created_at": plan["created_at"], "reason": plan["reason"],
            "max_age_seconds": plan["max_age_seconds"], "approval_receipt_id": receipt_id, "revocation_receipt_id": None}


def _receipt(override_id, plan_id, action, reason, actor, tool, now):
    return {"schema_version": _PREFIX + "override-receipt/v1", "receipt_id": uuid.uuid4().hex,
            "override_id": override_id, "plan_id": plan_id, "action": action, "reason": reason,
            "actor": actor, "tool": tool, "created_at": _stamp(now)}


def get_override(manager, override_id):
    """Read the projection only with its immutable approval/receipt evidence."""
    if not _id(override_id):
        raise _error("invalid_invocation_input", "A bounded override ID is required.", inputs=True)
    try:
        record = manager.repository.get("invocation-override", override_id)
        grant = manager.repository.get("invocation-override-grant", override_id)
        if not record or not grant:
            raise ValueError("missing override evidence")
        data, evidence = record["data"], grant["data"]
        if (set(evidence) != {"schema_version", "override_id", "plan", "approved_plan_id", "receipt_id", "actor", "tool", "created_at", "event_sequence"}
                or evidence["schema_version"] != _PREFIX + "override-grant/v1"):
            raise ValueError("grant fields")
        _labels(evidence["actor"], evidence["tool"])
        _date(evidence["created_at"])
        plan = evidence["plan"]
        _validate_plan(plan)
        if (set(data) != _PROJECTION_FIELDS or data["state"] not in {"active", "revoked"}
                or override_id != "override-" + plan["plan_id"] or evidence["override_id"] != override_id
                or evidence["approved_plan_id"] != plan["plan_id"]):
            raise ValueError("override binding")
        expected = _projection(plan, override_id, evidence["receipt_id"])
        expected.update(state=data["state"], revocation_receipt_id=data["revocation_receipt_id"])
        if expected != data:
            raise ValueError("override projection disagrees with approval")
        revocation = manager.repository.get("invocation-override-revocation", override_id)
        if data["state"] == "active" and revocation is not None:
            raise ValueError("active projection contradicts durable revocation")
        if data["state"] == "revoked" and (not revocation or set(revocation["data"]) != {"receipt_id", "event_sequence"}
                                            or revocation["data"]["receipt_id"] != data["revocation_receipt_id"]):
            raise ValueError("revoked projection has no durable revocation reference")
        for action, receipt_id in (("approve", data["approval_receipt_id"]), ("revoke", data["revocation_receipt_id"])):
            if action == "revoke" and data["state"] == "active":
                if receipt_id is not None:
                    raise ValueError("active override has a revocation")
                continue
            receipt = manager.repository.get("invocation-override-receipt", receipt_id)
            if not receipt or any(receipt["data"].get(key) != value for key, value in
                                  {"receipt_id": receipt_id, "override_id": override_id, "plan_id": plan["plan_id"], "action": action}.items()):
                raise ValueError("override receipt evidence")
            payload = receipt["data"]
            if (set(payload) != {"schema_version", "receipt_id", "override_id", "plan_id", "action", "reason", "actor", "tool", "created_at"}
                    or payload["schema_version"] != _PREFIX + "override-receipt/v1" or not _text(payload["reason"], 1024)):
                raise ValueError("receipt fields")
            _labels(payload["actor"], payload["tool"])
            _date(payload["created_at"])
            if action == "approve" and any(payload[key] != evidence[key] for key in ("actor", "tool", "created_at")):
                raise ValueError("grant attribution disagrees with approval receipt")
            if action == "approve" and (payload["reason"] != plan["reason"] or not _date(plan["created_at"]) <= _date(payload["created_at"]) < _date(plan["expires_at"])):
                raise ValueError("approval receipt outside approved window")
            sequence = evidence["event_sequence"] if action == "approve" else revocation["data"]["event_sequence"]
            if type(sequence) is not int or sequence < 1:
                raise ValueError("invalid event sequence")
            events = manager.repository.events(data["installation_id"], limit=1, after_sequence=sequence - 1)
            expected_payload = {"override_id": override_id, "receipt_id": receipt_id}
            if action == "approve":
                expected_payload["plan_id"] = data["plan_id"]
            if (len(events) != 1 or events[0]["sequence"] != sequence
                    or events[0]["stream"] != data["installation_id"] or events[0]["payload"] != expected_payload
                    or events[0]["event_type"] != "invocation_override_" + ("approved" if action == "approve" else "revoked")
                    or any(events[0][key] != payload[key] for key in ("actor", "tool"))):
                raise ValueError("override receipt lacks its append-only event")
        return data
    except (KeyError, TypeError, ValueError, LifecycleError) as error:
        raise _error("override_invalid", "Override approval or receipt evidence is missing or inconsistent.") from error


def apply_override(manager, plan, *, approve_plan_id=None, actor="local-operator", tool="lifecycle", now=None):
    _validate_plan(plan)
    supplied_now = now
    now = _clock(now, plan["max_age_seconds"])
    _labels(actor, tool)
    if approve_plan_id != plan["plan_id"]:
        raise _error("approval_required", "Explicit approval must name this exact override plan ID.")
    if plan["project_root"] != str(manager.project_root) or not _date(plan["created_at"]) <= now < _date(plan["expires_at"]):
        raise _error("override_expired", "Override plan is foreign, not yet valid or already expired.")
    record = _checked(manager, plan["installation_id"], now, plan["max_age_seconds"])
    with manager.repository.atomic():
        if supplied_now is None:
            now = _clock(None, plan["max_age_seconds"])
        if not _date(plan["created_at"]) <= now < _date(plan["expires_at"]):
            raise _error("override_expired", "Override expired during live inspection.")
        status = _unchanged(manager, record, now, plan["max_age_seconds"])
        if (record["revision"] != plan["expected_revision"] or _binding(record["data"]) != {key: plan[key] for key in _BINDING}
                or status["freshness"]["state"] != "stale"):
            raise _error("override_changed", "Override approval no longer matches this stale active installation.")
        override_id = "override-" + plan["plan_id"]
        if manager.repository.get("invocation-override", override_id):
            existing = get_override(manager, override_id)
            if existing["state"] != "active":
                raise _error("override_revoked", "A retry cannot reactivate a revoked override.")
            return existing
        receipt = _receipt(override_id, plan["plan_id"], "approve", plan["reason"], actor, tool, now)
        event = manager.repository.append_event(plan["installation_id"], "invocation_override_approved",
                                                {"override_id": override_id, "receipt_id": receipt["receipt_id"], "plan_id": plan["plan_id"]}, actor, tool)
        grant = {"schema_version": _PREFIX + "override-grant/v1", "override_id": override_id, "plan": plan,
                 "approved_plan_id": plan["plan_id"], "receipt_id": receipt["receipt_id"], "actor": actor, "tool": tool, "created_at": _stamp(now),
                 "event_sequence": event["sequence"]}
        projection = _projection(plan, override_id, receipt["receipt_id"])
        manager.repository.put("invocation-override-grant", override_id, grant)
        manager.repository.put("invocation-override-receipt", receipt["receipt_id"], receipt)
        manager.repository.put("invocation-override", override_id, projection)
        return projection


def revoke_override(manager, override_id, *, reason, actor="local-operator", tool="lifecycle", now=None):
    now = _clock(now)
    _labels(actor, tool)
    if not _text(reason, 1024):
        raise _error("invalid_invocation_input", "An explicit bounded revocation reason is required.", inputs=True)
    with manager.repository.atomic():
        current = get_override(manager, override_id)
        if current["state"] == "revoked":
            return current
        record = manager.repository.get("invocation-override", override_id)
        receipt = _receipt(override_id, current["plan_id"], "revoke", reason, actor, tool, now)
        projection = {**current, "state": "revoked", "revocation_receipt_id": receipt["receipt_id"]}
        event = manager.repository.append_event(current["installation_id"], "invocation_override_revoked",
                                                {"override_id": override_id, "receipt_id": receipt["receipt_id"]}, actor, tool)
        manager.repository.put("invocation-override-receipt", receipt["receipt_id"], receipt)
        manager.repository.put("invocation-override-revocation", override_id, {"receipt_id": receipt["receipt_id"], "event_sequence": event["sequence"]})
        manager.repository.put("invocation-override", override_id, projection, expected_revision=record["revision"])
        return projection


def select(manager, installation_id, *, policy="strict", refresh=True, override_id=None, now=None,
           max_age_seconds=DEFAULT_MAX_AGE_SECONDS, actor="local-adapter", tool="lifecycle"):
    """Return an audited path decision; no Skill, prompt or host is executed."""
    supplied_now = now
    now = _clock(now, max_age_seconds)
    _labels(actor, tool)
    if not _id(installation_id) or not isinstance(policy, str) or policy not in {"strict", "last-known-good"} or type(refresh) is not bool or override_id is not None and not _id(override_id):
        raise _error("invalid_invocation_input", "Invalid installation, selection policy, refresh flag or override ID.", inputs=True)
    context = manager_context(manager)
    result = {"schema_version": "skills-auditor-invocation/v1", "invocation_id": uuid.uuid4().hex,
              **context,
              "installation_id": installation_id, "version_id": None, "policy": policy, "decision": "block", "exit_code": 3,
              "snapshot_path": None, "override": None, "reason_codes": [], "assessed_at": _stamp(now),
              "status": unknown_status(installation_id, now=now, max_age_seconds=max_age_seconds,
                                       **context),
              "audit": {"recorded": False, "event_sequence": None},
              "notice": "Selection only, not execution or continuous enforcement. Current approved bytes are not semantic safety; actor/tool labels are not authenticated identities."}
    try:
        if refresh:
            manager.verify(installation_id)
            if supplied_now is None:
                now = _clock(None, max_age_seconds)
                result["assessed_at"] = _stamp(now)
        record = _checked(manager, installation_id, now, max_age_seconds)
        with manager.repository.atomic():
            if supplied_now is None:
                now = _clock(None, max_age_seconds)
                result["assessed_at"] = _stamp(now)
            status = _unchanged(manager, record, now, max_age_seconds)
            result.update(status=status, version_id=record["data"]["version_id"], reason_codes=list(status["reason_codes"]))
            stale = status["freshness"]["state"] == "stale"
            override = get_override(manager, override_id) if override_id is not None else None
            if override:
                if (override["state"] != "active" or not _date(override["created_at"]) <= now < _date(override["expires_at"])
                        or override["max_age_seconds"] != max_age_seconds
                        or {key: override[key] for key in _BINDING} != _binding(record["data"])):
                    raise _error("override_inapplicable", "Override is revoked, expired or bound to different active authorization.")
                result["override"] = {"override_id": override_id, "used": stale, "expires_at": override["expires_at"], "reason": override["reason"]}
            proceed = not stale or override is not None
            result["decision"] = "proceed" if proceed else "block" if policy == "strict" else "warn"
            result["exit_code"] = {"proceed": 0, "warn": 4, "block": 3}[result["decision"]]
            if proceed:
                result["snapshot_path"] = manager.repository.get("version", record["data"]["version_id"])["data"]["snapshot"]["path"]
            _audit(manager, result, actor, tool)
        manager_context(manager)
        return result
    except (LifecycleError, OSError, KeyError, TypeError, ValueError) as error:
        result.update(decision="block", exit_code=3, snapshot_path=None, override=None)
        result["reason_codes"] = list(dict.fromkeys(result["reason_codes"] + [getattr(error, "code", "invocation_failed")]))
        result["audit"] = {"recorded": False, "event_sequence": None}
        result["status"] = read_status(manager, installation_id, now=now, max_age_seconds=max_age_seconds)
        result["reason_codes"] = list(dict.fromkeys(result["reason_codes"] + result["status"]["reason_codes"]))
        try:
            with manager.repository.atomic():
                _audit(manager, result, actor, tool)
        except (LifecycleError, OSError, KeyError, TypeError, ValueError):
            result["audit"] = {"recorded": False, "event_sequence": None}
            result["reason_codes"].append("invocation_audit_failed")
        manager_context(manager)
        return result


def _audit(manager, result, actor, tool):
    manager_context(manager)
    event = manager.repository.append_event(result["installation_id"], "invocation_selected",
                                            {"invocation_id": result["invocation_id"], "decision": result["decision"],
                                             "version_id": result["version_id"], "override": result["override"],
                                             "reason_codes": result["reason_codes"]}, actor, tool)
    result["audit"] = {"recorded": True, "event_sequence": event["sequence"]}
    manager.repository.put("invocation", result["invocation_id"], result)
