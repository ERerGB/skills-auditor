"""Atomic status projections and host-neutral, point-in-time preflight policy.

The cached reader performs no filesystem inspection and never grants approval.
Only a durable completed verification publishes a derived projection. A failed
projection cannot roll back a denial; an in-progress run fences cached use.
Freshness is recomputed at read time, not trusted from disk. A host
that skips preflight is not blocked by this module; known bytes are not a claim
of semantic safety or continuous enforcement.
"""

from datetime import datetime, timezone
from shlex import quote

from .common import LifecycleError


SCHEMA_VERSION = "skills-auditor-status/v1"
DEFAULT_MAX_AGE_SECONDS = 300
_AUTHORIZATION = {"valid", "invalidated", "revoked", "unknown"}
_LIFECYCLE = {"active", "disabled", "archived", "uninstalled", "unknown"}
_REFERENCES = ("installation_id", "skill_id", "version_id", "receipt_id", "grant_id")
_CORE_CHECKS = {"snapshot_tree", "target_link", "receipt_record", "transaction_record", "grant_binding"}


def _text(value):
    return isinstance(value, str) and 0 < len(value) <= 512 and not any(ord(char) < 32 for char in value)


def _reasons(value):
    return isinstance(value, list) and len(value) <= 100 and all(_text(item) for item in value)


def _timestamp(value):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("invalid timestamp")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("timestamp must include timezone")
    return result.astimezone(timezone.utc)


def _policy(now, max_age_seconds):
    if type(max_age_seconds) is not int or not 1 <= max_age_seconds <= 86400:
        raise LifecycleError("invalid_status_policy", "max_age_seconds must be an integer from 1 to 86400.", exit_code=2)
    if now is None:
        now = datetime.now(timezone.utc)
    if type(now) is not datetime or now.tzinfo is None or now.utcoffset() is None:
        raise LifecycleError("invalid_status_policy", "now must be a timezone-aware datetime.", exit_code=2)
    return now.astimezone(timezone.utc)


def _installation(value):
    if not isinstance(value, dict):
        return False
    authorization = value.get("authorization", {})
    return (all(_text(value.get(key)) for key in ("installation_id", "skill_id", "version_id", "receipt_id", "target"))
            and value.get("state") in _LIFECYCLE and isinstance(authorization, dict)
            and authorization.get("state") in _AUTHORIZATION and _text(authorization.get("grant_id"))
            and _reasons(authorization.get("reason_codes"))
            and type(value.get("generation")) is int and value["generation"] > 0)


def _verification(value, installation):
    if not isinstance(value, dict):
        return False
    approval, integrity = value.get("approval", {}), value.get("integrity", {})
    if not isinstance(approval, dict) or not isinstance(integrity, dict):
        return False
    checks = integrity.get("checks")
    expected = {key: installation[key] for key in _REFERENCES if key != "grant_id"}
    expected["grant_id"] = installation["authorization"]["grant_id"]
    if (value.get("schema_version") != "skills-auditor-lifecycle-verification/v1"
            or not _text(value.get("verification_id"))
            or any(value.get(key) != item for key, item in expected.items())
            or approval.get("state") != installation["authorization"]["state"]
            or type(approval.get("requires_reapproval")) is not bool
            or approval["requires_reapproval"] != (approval["state"] != "valid")
            or not _reasons(approval.get("reason_codes"))
            or (approval["state"] == "valid" and bool(approval["reason_codes"]))
            or type(integrity.get("valid")) is not bool or type(value.get("valid")) is not bool
            or not isinstance(checks, list) or not 1 <= len(checks) <= 100
            or any(not isinstance(check, dict) or not _text(check.get("code")) or type(check.get("valid")) is not bool for check in checks)):
        return False
    codes = [check["code"] for check in checks]
    if (not _CORE_CHECKS.issubset(codes) or len(set(codes)) != len(codes)
            or integrity["valid"] != all(check["valid"] for check in checks)
            or value["valid"] != (integrity["valid"] and approval["state"] == "valid" and not approval["reason_codes"])):
        return False
    _timestamp(value.get("observed_at"))
    return True


def _finish(status, now, max_age_seconds):
    reasons = status["reason_codes"]
    freshness = {"state": "unknown", "age_seconds": None, "max_age_seconds": max_age_seconds}
    if status["observed_at"] is not None:
        age = (now - _timestamp(status["observed_at"])).total_seconds()
        if age < 0:
            reasons.append("observation_in_future")
        else:
            freshness.update(state="fresh" if age <= max_age_seconds else "stale", age_seconds=age)
            if freshness["state"] == "stale":
                reasons.append("observation_stale")
    state = status["lifecycle_state"]
    if state != "active":
        reasons.append("lifecycle_" + state)
    blocked = status["approval"]["state"] != "valid" or state != "active" or freshness["state"] == "unknown"
    status["severity"] = "error" if blocked else "warning" if freshness["state"] == "stale" else "ok"
    status["freshness"] = freshness
    status["assessed_at"] = now.isoformat(timespec="microseconds").replace("+00:00", "Z")
    status["reason_codes"] = list(dict.fromkeys(reasons))
    identifier = quote(status["installation_id"] or "<installation-id>")
    if state in {"disabled", "archived"}:
        command = "skills-audit lifecycle plan enable --installation-id " + identifier
        message = "This installation is not active. Investigate any integrity failure first; valid retained bytes and an explicitly approved enable plan are required before use."
    elif state == "uninstalled":
        command = "skills-audit lifecycle plan install --source <candidate> --target <installation-target>"
        message = "This installation is historical. A new installation requires a reviewed and explicitly approved plan."
    elif status["approval"]["state"] == "invalidated":
        operation = "update" if "snapshot_tree" in reasons else "renew"
        command = "skills-audit lifecycle plan " + operation + " --installation-id " + identifier
        if operation == "update":
            command += " --source <reviewed-candidate>"
        message = "Investigate the referenced verification; repair or replace the candidate, review a new plan and explicitly approve it."
    else:
        command = "skills-audit lifecycle verify " + identifier
        message = "Verify immediately before use. This is a point-in-time observation, not continuous enforcement or semantic safety."
    status["recommended_next_action"] = {"command": command, "message": message}
    return status


def _base(installation_id, installation=None):
    installation = installation or {}
    authorization = installation.get("authorization", {})
    authorization_state = authorization.get("state", "unknown")
    invalidated = authorization_state in {"invalidated", "revoked"}
    reason_codes = list(authorization.get("reason_codes", []))
    if invalidated:
        reason_codes.append("approval_" + authorization_state)
    return {
        "schema_version": SCHEMA_VERSION, "installation_id": installation_id,
        "skill_id": installation.get("skill_id"), "version_id": installation.get("version_id"),
        "receipt_id": installation.get("receipt_id"), "grant_id": authorization.get("grant_id"),
        "verification_id": None, "plan_id": None, "observed_at": None,
        "incident_ids": [],
        "lifecycle_state": installation.get("state", "unknown"), "authorization_state": authorization_state,
        "approval": {"state": "invalidated" if invalidated else "unknown", "requires_reapproval": True,
                     "reason_codes": list(dict.fromkeys(reason_codes))},
        "integrity": {"valid": None}, "reason_codes": reason_codes,
    }


def unknown_status(installation_id=None, *, reason="repository_missing", now=None, max_age_seconds=DEFAULT_MAX_AGE_SECONDS):
    """Build a fail-closed status even when opening read-only state failed."""
    now = _policy(now, max_age_seconds)
    if (installation_id is not None and not _text(installation_id)) or not _text(reason):
        raise LifecycleError("invalid_status_input", "Status identity and reason must be bounded strings.", exit_code=2)
    status = _base(installation_id)
    status["reason_codes"].append(reason)
    return _finish(status, now, max_age_seconds)


def _project(installation, verification, plan_id, now, max_age_seconds):
    status = _base(installation["installation_id"], installation)
    status.update(verification_id=verification["verification_id"], plan_id=plan_id, observed_at=verification["observed_at"])
    approval = verification["approval"]
    status["approval"] = {"state": "invalidated" if approval["state"] == "revoked" else approval["state"],
                          "requires_reapproval": approval["requires_reapproval"], "reason_codes": list(dict.fromkeys(approval["reason_codes"]))}
    status["integrity"] = {"valid": verification["integrity"]["valid"]}
    status["reason_codes"].extend(approval["reason_codes"])
    return _finish(status, now, max_age_seconds)


def publish_status(repository, installation, verification):
    """Publish a derived projection after durable verification/authorization."""
    try:
        if not _installation(installation) or not _verification(verification, installation):
            raise ValueError("installation or verification is inconsistent")
        receipt = repository.get("receipt", installation["receipt_id"])
        plan_id = receipt["data"].get("plan_id") if receipt else None
        if not _text(plan_id) and verification["valid"]:
            raise ValueError("completed receipt plan reference is missing")
        if not _text(plan_id):
            plan_id = None
        status = _project(installation, verification, plan_id, _timestamp(verification["observed_at"]), DEFAULT_MAX_AGE_SECONDS)
        _attach_incidents(repository, status)
    except (KeyError, TypeError, ValueError) as error:
        raise LifecycleError("invalid_status", "Cannot publish an inconsistent completed verification.") from error
    previous = repository.get("status", installation["installation_id"])
    repository.put("status", installation["installation_id"], status, expected_revision=previous["revision"] if previous else 0)
    return status


def _attach_incidents(repository, status):
    """Derive investigation links from the current durable observation index."""
    index = repository.get("verification-incidents", status["verification_id"])
    identifiers = index["data"].get("incident_ids") if index else []
    if not _reasons(identifiers) or len(set(identifiers)) != len(identifiers):
        raise ValueError("invalid verification incident index")
    for identifier in identifiers:
        incident = repository.get("incident", identifier)
        if not incident or any(incident["data"].get(key) != status[key] for key in ("installation_id", "grant_id", "version_id")):
            raise ValueError("incident reference belongs to another authorization")
    status["incident_ids"] = identifiers
    return status


def read_status(manager, installation_id, *, now=None, max_age_seconds=DEFAULT_MAX_AGE_SECONDS):
    """Read a projection conservatively; never inspect candidates or write state."""
    now = _policy(now, max_age_seconds)
    if not _text(installation_id):
        raise LifecycleError("invalid_status_input", "An installation ID is required.", exit_code=2)
    status = _base(installation_id)
    failure = "status_unreadable"
    try:
        repository = manager.repository
        record = repository.get("installation", installation_id)
        if record is None:
            return unknown_status(installation_id, reason="installation_unknown", now=now, max_age_seconds=max_age_seconds)
        installation = record["data"]
        if not _installation(installation) or installation["installation_id"] != installation_id:
            raise ValueError("invalid installation")
        status = _base(installation_id, installation)
        run = repository.get("verification-run", installation_id)
        if run and run["data"].get("state") != "completed":
            status["reason_codes"].append("verification_incomplete")
            return _finish(status, now, max_age_seconds)
        run_binding = {"grant_id": installation["authorization"]["grant_id"], "version_id": installation["version_id"], "generation": installation["generation"]}
        if run and any(run["data"].get(key) != value for key, value in run_binding.items()):
            status["reason_codes"].append("verification_binding_changed")
            return _finish(status, now, max_age_seconds)
        stored = repository.get("status", installation_id)
        if stored is None:
            status["reason_codes"].append("never_verified")
            return _finish(status, now, max_age_seconds)
        failure = "status_invalid"
        cached = stored["data"]
        latest = repository.get("latest-verification", installation_id)
        if not latest or not _text(latest["data"].get("verification_id")):
            raise ValueError("missing latest verification")
        if not run or run["data"].get("verification_id") != latest["data"]["verification_id"]:
            raise ValueError("verification completion marker does not match latest evidence")
        observed = repository.get("verification", latest["data"]["verification_id"])
        if not observed or not _verification(observed["data"], installation):
            raise ValueError("verification does not match installation")
        verification = observed["data"]
        grant_id = installation["authorization"]["grant_id"]
        grant = repository.get("grant", grant_id)
        authorization = repository.get("authorization", grant_id)
        receipt = repository.get("receipt", installation["receipt_id"])
        binding = {key: installation[key] for key in ("installation_id", "skill_id", "version_id")}
        if installation["state"] == "active" and installation["authorization"]["state"] == "valid":
            binding.update(target=installation["target"], installation_generation=installation["generation"])
        if verification["valid"] and (not grant or not authorization or not receipt
                or any(grant["data"].get(key) != item for key, item in binding.items())
                or authorization["data"] != installation["authorization"]
                or receipt["data"].get("status") != "completed"
                or any(receipt["data"].get(key) != verification[key] for key in _REFERENCES if key != "receipt_id")
                or not _text(receipt["data"].get("plan_id"))):
            raise ValueError("grant or receipt does not match installation")
        if verification["valid"]:
            from .engine import Manager
            if not all(check["valid"] for check in Manager._evidence_checks(manager, installation)):
                raise ValueError("current transaction evidence no longer matches the completed verification")
        plan_id = receipt["data"].get("plan_id") if receipt else None
        if not _text(plan_id):
            plan_id = None
        expected = _project(installation, verification, plan_id, _timestamp(verification["observed_at"]), DEFAULT_MAX_AGE_SECONDS)
        # Additive fields are ignored, but required identity and authorization
        # fields cannot disagree with the underlying immutable evidence.
        for key in ("schema_version", *_REFERENCES, "verification_id", "plan_id", "observed_at", "lifecycle_state", "authorization_state", "approval", "integrity"):
            if cached.get(key) != expected[key]:
                raise ValueError("status projection contradicts evidence")
        return _attach_incidents(repository, _project(installation, verification, expected["plan_id"], now, max_age_seconds))
    except (LifecycleError, OSError) as error:
        status["reason_codes"].extend(["status_unreadable", getattr(error, "code", "io_error")])
    except (KeyError, TypeError, ValueError, AttributeError):
        status["reason_codes"].append(failure)
    return _finish(status, now, max_age_seconds)


def preflight(manager, installation_id, *, refresh=False, now=None, max_age_seconds=DEFAULT_MAX_AGE_SECONDS):
    """Fresh valid active: proceed/0; stale cached valid: warn/4; other: block/3."""
    supplied_now = now
    now = _policy(now, max_age_seconds)
    if type(refresh) is not bool or not _text(installation_id):
        raise LifecycleError("invalid_status_input", "refresh must be a boolean and an installation ID is required.", exit_code=2)
    if refresh:
        try:
            manager.verify(installation_id)
        except (LifecycleError, OSError, ValueError):
            status = unknown_status(installation_id, reason="verification_failed", now=now, max_age_seconds=max_age_seconds)
            return {"decision": "block", "exit_code": 3, "status": status}
        if supplied_now is None:
            now = _policy(None, max_age_seconds)
    status = read_status(manager, installation_id, now=now, max_age_seconds=max_age_seconds)
    decision, exit_code = {"ok": ("proceed", 0), "warning": ("warn", 4), "error": ("block", 3)}[status["severity"]]
    return {"decision": decision, "exit_code": exit_code, "status": status}


def render_status(status):
    """ASCII markers remain prominent without terminal color or host icons."""
    marker = {"ok": "[OK]", "warning": "[WARN]", "error": "[BLOCK]"}[status["severity"]]
    freshness = status["freshness"]
    age = "unknown" if freshness["age_seconds"] is None else "{:g}s".format(freshness["age_seconds"])
    rendered = "{} {} approval={} authorization={} lifecycle={} freshness={} age={}\nReasons: {}\nNext: {}\n{}\nCached point-in-time evidence; no continuous enforcement or semantic-safety guarantee.".format(
        marker, status["installation_id"] or "unknown installation", status["approval"]["state"], status["authorization_state"], status["lifecycle_state"], freshness["state"], age,
        ", ".join(status["reason_codes"]) or "none", status["recommended_next_action"]["command"], status["recommended_next_action"]["message"])
    for identifier in status.get("incident_ids", []):
        rendered += "\nInvestigate: skills-audit lifecycle inspect incident " + quote(identifier)
    return rendered
