"""Durable fault incidents and bounded, local Agent investigation packets.

Incidents are consumers of already-durable verification, never its authority.
Publishing an incident cannot roll back a previously observed denial. One
installation/grant/version/failure signature owns one incident; a repeated
observation appends evidence without creating another incident. Restored bytes
with a sticky invalidation link history but neither open a new fault nor resolve
one. Explicit non-remediation dispositions remain historical decisions, not
approval or permission to use an invalid installation.

Only allowlisted check evidence and explicitly supplied notes are collected.
No prompt, environment, credential or arbitrary file content is read. Local
actor/tool labels are attribution, not authenticated identities.
"""

import copy
from datetime import datetime
import re
import uuid

from .common import LifecycleError, canonical_json, digest, utc_now
from .context import action, manager_context


_SCHEMA = "skills-auditor-incident/v1"
_STATES = {"open", "investigating", "resolved", "superseded"}
_KINDS = {"skill", "installation", "version", "grant", "receipt", "transaction", "verification", "incident"}
_EVIDENCE_KEYS = {
    "kind", "link", "path", "state", "status", "source_tree_sha256", "snapshot_tree_sha256", "sha256",
    "version_id", "grant_id", "receipt_id", "transaction_id", "installation_id", "skill_id", "plan_id",
    "generation", "installation_generation", "valid", "exists", "mode", "normalization",
}
_FIELDS = {
    "schema_version", "incident_id", "signature", "installation_id", "skill_id", "version_id", "grant_id",
    "state", "opened_at", "updated_at", "opening_verification_id", "latest_verification_id",
    "observation_count", "reason_codes", "evidence", "resolution", "superseded_by",
}


def _invalid(message, code="invalid_incident_input"):
    return LifecycleError(code, message, exit_code=2 if code == "invalid_incident_input" else 3)


def _identifier(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}", value) is not None


def _text(value, maximum=4096):
    try:
        return isinstance(value, str) and bool(value.strip()) and len(value.encode("utf-8")) <= maximum and "\x00" not in value
    except UnicodeError:
        return False


def _actor(actor, tool):
    if not all(_text(value, 200) and not any(ord(char) < 32 for char in value) for value in (actor, tool)):
        raise _invalid("actor and tool must be bounded local attribution labels.")


def _time(value):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("invalid timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include timezone")
    return parsed


def _safe(value, depth=0):
    """Bound necessary filesystem facts; arbitrary nested fields are omitted."""
    if value is None or type(value) in {bool, int}:
        return value
    if isinstance(value, str):
        return value[:1000]
    if isinstance(value, dict) and depth < 3:
        return {key: _safe(item, depth + 1) for key, item in value.items() if key in _EVIDENCE_KEYS}
    return None


def _failures(verification):
    try:
        checks = verification["integrity"]["checks"]
        approval = verification["approval"]
        if not isinstance(checks, list) or not 1 <= len(checks) <= 100 or type(verification["valid"]) is not bool:
            raise ValueError("invalid verification checks")
        if (not isinstance(approval, dict) or approval.get("state") not in {"valid", "invalidated", "revoked", "unknown"}
                or type(approval.get("requires_reapproval")) is not bool
                or approval["requires_reapproval"] != (approval["state"] != "valid")
                or not isinstance(approval.get("reason_codes"), list)
                or any(not _identifier(code) for code in approval["reason_codes"])):
            raise ValueError("invalid verification approval")
        result = []
        for check in checks:
            if not isinstance(check, dict) or not _identifier(check.get("code")) or type(check.get("valid")) is not bool:
                raise ValueError("invalid check")
            if check["valid"]:
                continue
            if verification["valid"]:
                raise ValueError("successful verification contradicts a failed check")
            error = check.get("error", {})
            details = error.get("details", {}) if isinstance(error, dict) else {}
            details = details if isinstance(details, dict) else {}
            evidence = {"code": check["code"], "expected": _safe(check.get("expected", details.get("expected"))),
                        "actual": _safe(check.get("actual", details.get("actual")))}
            # Paths locate evidence, not source contents. Error prose, actors,
            # timestamps, inode identities and random IDs do not fingerprint it.
            path = check.get("path", details.get("path"))
            if isinstance(path, str):
                evidence["path"] = path[:1000]
            result.append(evidence)
        if (type(verification["integrity"].get("valid")) is not bool
                or verification["integrity"]["valid"] != all(check["valid"] for check in checks)
                or verification["valid"] != (all(check["valid"] for check in checks) and approval["state"] == "valid" and not approval["reason_codes"])):
            raise ValueError("verification result contradicts its evidence")
        unique = {canonical_json(item): item for item in result}
        result = [unique[key] for key in sorted(unique)]
        if len(canonical_json(result).encode("utf-8")) > 16384:
            raise ValueError("check evidence exceeds bounded incident packet size")
        return result
    except (KeyError, TypeError, ValueError) as error:
        raise _invalid("Verification must contain bounded, consistent check evidence.") from error


def _resolution_valid(value):
    if not isinstance(value, dict) or value.get("kind") not in {"remediated", "non_remediation"}:
        return False
    _time(value.get("resolved_at"))
    if value["kind"] == "remediated":
        fields = {"kind", "resolved_at", "verification_id", "grant_id", "receipt_id", "transaction_id"}
        return set(value) == fields and all(_identifier(value.get(key)) for key in fields - {"kind", "resolved_at"})
    return set(value) == {"kind", "resolved_at", "disposition", "explanation"} and _identifier(value.get("disposition")) and _text(value.get("explanation"))


def _evidence_valid(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 100 or len(canonical_json(value).encode("utf-8")) > 16384:
        return False
    for item in value:
        if (not isinstance(item, dict) or not {"code", "expected", "actual"}.issubset(item)
                or not set(item).issubset({"code", "expected", "actual", "path"})
                or not _identifier(item["code"]) or _safe(item["expected"]) != item["expected"]
                or _safe(item["actual"]) != item["actual"]
                or ("path" in item and (not isinstance(item["path"], str) or len(item["path"]) > 1000))):
            return False
    return True


def _record(repository, incident_id, *, _seen=frozenset()):
    if not _identifier(incident_id):
        raise _invalid("A bounded incident ID is required.")
    if incident_id in _seen or len(_seen) >= 50:
        raise _invalid("Incident supersession chain is cyclic or exceeds the bounded reader depth.", "incident_corrupt")
    record = repository.get("incident", incident_id)
    if record is None:
        raise _invalid("Incident does not exist.", "incident_missing")
    value = record["data"]
    try:
        if (not _FIELDS.issubset(value) or value["schema_version"] != _SCHEMA or value["incident_id"] != incident_id
                or value["state"] not in _STATES or type(value["observation_count"]) is not int or value["observation_count"] < 1
                or not all(_identifier(value[key]) for key in ("installation_id", "skill_id", "version_id", "opening_verification_id", "latest_verification_id"))
                or (value["grant_id"] is not None and not _identifier(value["grant_id"]))
                or not _evidence_valid(value["evidence"])
                or value["reason_codes"] != sorted({item["code"] for item in value["evidence"]})
                or value["signature"] != digest({**{key: value[key] for key in ("installation_id", "grant_id", "version_id")}, "failures": value["evidence"]})
                or incident_id != "incident-" + value["signature"]
                or (value["state"] == "resolved" and not _resolution_valid(value["resolution"]))
                or (value["state"] != "resolved" and value["resolution"] is not None)
                or (value["state"] == "superseded" and (not _identifier(value["superseded_by"]) or value["superseded_by"] == incident_id))
                or (value["state"] != "superseded" and value["superseded_by"] is not None)):
            raise ValueError("invalid incident projection")
        _time(value["opened_at"])
        _time(value["updated_at"])
        if value["state"] == "resolved":
            _historical_resolution(repository, value)
        elif value["state"] == "superseded":
            _historical_supersession(repository, value, _seen | {incident_id})
    except (LifecycleError, KeyError, TypeError, ValueError) as error:
        raise _invalid("Incident projection is malformed; no healthy empty state is inferred.", "incident_corrupt") from error
    return record


def get_incident(manager, incident_id):
    return copy.deepcopy(_record(manager.repository, incident_id)["data"])


def list_incidents(manager, *, installation_id=None, state=None):
    if (installation_id is not None and not _identifier(installation_id)) or (state is not None and (not isinstance(state, str) or state not in _STATES)):
        raise _invalid("Invalid incident filter.")
    incidents = []
    for record in manager.repository.list("incident"):
        value = _record(manager.repository, record["id"])["data"]
        if (installation_id is None or value["installation_id"] == installation_id) and (state is None or value["state"] == state):
            incidents.append(copy.deepcopy(value))
    return incidents


def _append(repository, incident_id, event_type, payload, actor, tool, event_id):
    """An event ID is a retry key, not permission to change a previous event."""
    request = {"incident_id": incident_id, "event_type": event_type, "payload": payload, "actor": actor, "tool": tool}
    checksum = digest(request)
    previous = repository.get("incident-event", event_id)
    if previous:
        if previous["data"].get("request_hash") != checksum:
            raise _invalid("Event retry key was already used for different content.", "incident_event_conflict")
        return _durable_event(repository, incident_id, event_id, event_type, payload), False
    event = repository.append_event("incident:" + incident_id, event_type, {"event_id": event_id, "incident_id": incident_id, **payload}, actor, tool)
    repository.put("incident-event", event_id, {"request_hash": checksum, "event": event})
    return event, True


def record_verification(repository, installation, verification, *, actor="local-observer", tool="lifecycle"):
    """Consume a committed observation in a separate atomic incident batch."""
    _actor(actor, tool)
    try:
        verification_id = verification["verification_id"]
        if not _identifier(verification_id) or verification.get("schema_version") != "skills-auditor-lifecycle-verification/v1":
            raise ValueError("invalid verification identity")
        _time(verification["observed_at"])
        for key in ("installation_id", "skill_id", "version_id"):
            if not _identifier(verification[key]) or verification[key] != installation[key]:
                raise ValueError("verification and installation identities differ")
        if verification["grant_id"] != installation["authorization"]["grant_id"]:
            raise ValueError("verification grant differs")
        failures = _failures(verification)
    except (KeyError, TypeError, ValueError) as error:
        raise _invalid("Incident observation must identify one verified installation.") from error
    with repository.atomic():
        committed = repository.get("verification", verification_id)
        if not committed or committed["data"] != verification:
            raise _invalid("Only an exact durable verification record can become incident evidence.")
        prior = repository.get("verification-incidents", verification_id)
        if prior:
            identifiers = prior["data"].get("incident_ids")
            if (not isinstance(identifiers, list) or len(identifiers) > 100
                    or any(not _identifier(identifier) for identifier in identifiers)
                    or len(identifiers) != len(set(identifiers)) or (failures and not identifiers)):
                raise _invalid("Verification incident index is malformed.", "incident_corrupt")
            if failures:
                expected_id = "incident-" + digest({**{key: verification[key] for key in ("installation_id", "grant_id", "version_id")}, "failures": failures})
                if identifiers != [expected_id]:
                    raise _invalid("Verification incident index disagrees with the actual failure signature.", "incident_corrupt")
            elif verification["approval"]["state"] != "invalidated" and identifiers:
                raise _invalid("A clean non-invalidated observation cannot reference a fault.", "incident_corrupt")
            references = []
            for identifier in identifiers:
                incident = _record(repository, identifier)["data"]
                if any(incident[key] != verification[key] for key in ("installation_id", "grant_id", "version_id")):
                    raise _invalid("Verification incident index references another authorization.", "incident_corrupt")
                _durable_event(repository, identifier,
                               "observation-" + digest({"incident_id": identifier, "verification_id": verification_id}),
                               "observation", {"verification_id": verification_id, "observed_at": verification["observed_at"],
                                               "checks_failed": bool(failures), "reason_codes": sorted({item["code"] for item in failures})})
                references.append({"incident_id": identifier, "state": incident["state"]})
            return references
        records = []
        if failures:
            identity = {key: verification[key] for key in ("installation_id", "grant_id", "version_id")}
            signature = digest({**identity, "failures": failures})
            identifier = "incident-" + signature
            existing = repository.get("incident", identifier)
            if existing:
                records = [_record(repository, identifier)]
            else:
                value = {"schema_version": _SCHEMA, "incident_id": identifier, "signature": signature,
                         **identity, "skill_id": verification["skill_id"], "state": "open",
                         "opened_at": verification["observed_at"], "updated_at": verification["observed_at"],
                         "opening_verification_id": verification_id, "latest_verification_id": verification_id,
                         "observation_count": 0, "reason_codes": sorted({item["code"] for item in failures}),
                         "evidence": failures, "resolution": None, "superseded_by": None}
                records = [{"id": identifier, "revision": 0, "data": value}]
        elif verification["approval"]["state"] == "invalidated":
            # Historical sticky denial is not a fresh failure signature.
            records = [_record(repository, item["id"]) for item in repository.list("incident")
                       if all(item["data"].get(key) == verification[key] for key in ("installation_id", "grant_id", "version_id"))]
        references = []
        for record in records:
            incident = copy.deepcopy(record["data"])
            identifier = incident["incident_id"]
            _append(repository, identifier, "observation", {"verification_id": verification_id,
                    "observed_at": verification["observed_at"], "checks_failed": bool(failures),
                    "reason_codes": sorted({item["code"] for item in failures})}, actor, tool,
                    "observation-" + digest({"incident_id": identifier, "verification_id": verification_id}))
            incident.update(latest_verification_id=verification_id, updated_at=verification["observed_at"])
            incident["observation_count"] += 1
            repository.put("incident", identifier, incident, expected_revision=record["revision"])
            references.append({"incident_id": identifier, "state": incident["state"]})
        repository.put("verification-incidents", verification_id, {"incident_ids": [item["incident_id"] for item in references]})
        return references


def _references(repository, references):
    if not isinstance(references, list) or len(references) > 20:
        raise _invalid("Evidence references must be at most 20 known record identities.")
    result = []
    for reference in references:
        if (not isinstance(reference, dict) or set(reference) != {"kind", "id"} or not _identifier(reference["kind"]) or reference["kind"] not in _KINDS
                or not _identifier(reference["id"]) or repository.get(reference["kind"], reference["id"]) is None):
            raise _invalid("Evidence references must identify existing managed records, not arbitrary files.")
        if reference not in result:
            result.append(dict(reference))
    return result


def _event_view(event, incident_id):
    """Validate payload bounds even when a checksum-valid record is malformed."""
    try:
        payload = event["payload"]
        if payload.get("incident_id") != incident_id or not _identifier(payload.get("event_id")):
            raise ValueError("invalid event identity")
        _actor(event["actor"], event["tool"])
        _time(event["created_at"])
        event_type = event["event_type"]
        fields = {"note": {"text", "evidence_refs"}, "observation": {"verification_id", "observed_at", "checks_failed", "reason_codes"},
                  "resolved": {"resolution"}, "superseded": {"superseded_by", "explanation"}}
        if event_type not in fields or set(payload) != {"event_id", "incident_id"} | fields[event_type]:
            raise ValueError("event-specific fields are missing or unexpected")
        if event_type == "note":
            references = payload.get("evidence_refs")
            if not _text(payload.get("text")) or not isinstance(references, list) or len(references) > 20:
                raise ValueError("invalid note")
            if any(not isinstance(ref, dict) or set(ref) != {"kind", "id"} or not _identifier(ref["kind"]) or ref["kind"] not in _KINDS or not _identifier(ref["id"]) for ref in references):
                raise ValueError("invalid note references")
        elif event_type == "observation":
            _time(payload.get("observed_at"))
            if (not _identifier(payload.get("verification_id")) or type(payload.get("checks_failed")) is not bool
                    or not isinstance(payload.get("reason_codes"), list) or len(payload["reason_codes"]) > 100
                    or any(not _identifier(code) for code in payload["reason_codes"])):
                raise ValueError("invalid observation")
        elif event_type == "resolved":
            if not _resolution_valid(payload.get("resolution")):
                raise ValueError("invalid resolution")
        elif event_type == "superseded":
            if not _identifier(payload.get("superseded_by")) or not _text(payload.get("explanation")):
                raise ValueError("invalid supersession")
        else:
            raise ValueError("unknown incident event")
    except (LifecycleError, KeyError, TypeError, ValueError) as error:
        raise _invalid("Incident history is malformed; refusing an unbounded or misleading packet.", "incident_corrupt") from error
    allowed = {"event_id", "incident_id", "verification_id", "observed_at", "checks_failed", "reason_codes",
               "text", "evidence_refs", "resolution", "superseded_by", "explanation"}
    return {**{key: event[key] for key in ("sequence", "event_type", "actor", "tool", "created_at")},
            "payload": {key: value for key, value in payload.items() if key in allowed}}


def _durable_event(repository, incident_id, event_id, event_type, payload):
    """A mutable retry index is not evidence without its append-only event."""
    try:
        record = repository.get("incident-event", event_id)
        if not record or set(record["data"]) != {"request_hash", "event"}:
            raise ValueError("missing event index")
        event = record["data"]["event"]
        _event_view(event, incident_id)
        if (event["stream"] != "incident:" + incident_id or event["event_type"] != event_type
                or event["payload"] != {"event_id": event_id, "incident_id": incident_id, **payload}
                or type(event["sequence"]) is not int or event["sequence"] < 1):
            raise ValueError("event binding differs")
        request = {"incident_id": incident_id, "event_type": event_type, "payload": payload,
                   "actor": event["actor"], "tool": event["tool"]}
        if record["data"]["request_hash"] != digest(request):
            raise ValueError("retry checksum differs")
        # One exact sequence lookup: do not load an unbounded history stream.
        if repository.events(event["stream"], limit=1, after_sequence=event["sequence"] - 1) != [event]:
            raise ValueError("indexed event is absent from append-only history")
        return event
    except (LifecycleError, KeyError, TypeError, ValueError) as error:
        raise _invalid("Incident index has no matching durable event.", "incident_corrupt") from error


def _historical_resolution(repository, incident):
    """Validate immutable proof as it existed then, never current health."""
    from types import SimpleNamespace
    from .engine import Manager
    from .status import _verification
    resolution = incident["resolution"]
    proof = repository.get("incident-resolution", incident["incident_id"])
    if (not proof or set(proof["data"]) != {"event_id", "resolution"}
            or proof["data"]["resolution"] != resolution):
        raise ValueError("missing resolution event reference")
    event = _durable_event(repository, incident["incident_id"], proof["data"]["event_id"], "resolved", {"resolution": resolution})
    if _time(resolution["resolved_at"]) < _time(incident["opened_at"]) or _time(event["created_at"]) < _time(resolution["resolved_at"]):
        raise ValueError("resolution precedes its evidence")
    if resolution["kind"] == "non_remediation":
        return
    verification = repository.get("verification", resolution["verification_id"])["data"]
    grant = repository.get("grant", resolution["grant_id"])["data"]
    receipt = repository.get("receipt", resolution["receipt_id"])["data"]
    transaction = repository.get("transaction", resolution["transaction_id"])["data"]
    expected = {"installation_id": incident["installation_id"], "skill_id": incident["skill_id"],
                "grant_id": resolution["grant_id"], "receipt_id": resolution["receipt_id"], "version_id": grant["version_id"]}
    if (verification.get("schema_version") != "skills-auditor-lifecycle-verification/v1"
            or verification["verification_id"] != resolution["verification_id"]
            or any(verification[key] != value for key, value in expected.items())
            or not verification["valid"] or _failures(verification) or verification["approval"]["state"] != "valid"
            or grant["grant_id"] == incident["grant_id"] or grant["grant_id"] != resolution["grant_id"]
            or any(grant[key] != incident[key] for key in ("installation_id", "skill_id"))
            or receipt["receipt_id"] != resolution["receipt_id"] or receipt["transaction_id"] != resolution["transaction_id"]
            or transaction["transaction_id"] != resolution["transaction_id"]
            or not _time(incident["opened_at"]) <= _time(grant["approved_at"]) <= _time(verification["observed_at"]) <= _time(resolution["resolved_at"])):
        raise ValueError("historical resolution proof does not match this incident")
    historical = copy.deepcopy(transaction["plan"]["after"])
    historical.update(receipt_id=receipt["receipt_id"], last_transaction_id=transaction["transaction_id"],
                      generation=grant["installation_generation"], authorization={"state": "valid", "grant_id": grant["grant_id"], "reason_codes": []})
    if (historical["state"] != "active" or grant["target"] != historical["target"] or not _verification(verification, historical)
            or not all(check["valid"] for check in Manager._evidence_checks(SimpleNamespace(repository=repository), historical))):
        raise ValueError("historical completed receipt and approved transaction are inconsistent")


def _historical_supersession(repository, incident, seen):
    proof = repository.get("incident-supersession", incident["incident_id"])
    if (not proof or set(proof["data"]) != {"event_id", "replacement_id", "explanation"}
            or proof["data"]["replacement_id"] != incident["superseded_by"]):
        raise ValueError("supersession has no committed event reference")
    event = _durable_event(repository, incident["incident_id"], proof["data"]["event_id"], "superseded",
                           {"superseded_by": incident["superseded_by"], "explanation": proof["data"]["explanation"]})
    replacement = _record(repository, incident["superseded_by"], _seen=seen)["data"]
    if (replacement["installation_id"] != incident["installation_id"]
            or any(_time(item["opened_at"]) > _time(event["created_at"]) for item in (incident, replacement))):
        raise ValueError("supersession points to an unrelated or later incident")


def append_note(manager, incident_id, text, *, actor, tool, evidence_refs=None, event_id=None):
    _actor(actor, tool)
    if not _text(text) or (event_id is not None and not _identifier(event_id)):
        raise _invalid("Notes must contain 1–4096 UTF-8 bytes; retry IDs must be bounded identifiers.")
    repository = manager.repository
    with repository.atomic():
        record = _record(repository, incident_id)
        references = _references(repository, [] if evidence_refs is None else evidence_refs)
        event, created = _append(repository, incident_id, "note", {"text": text, "evidence_refs": references}, actor, tool, event_id or uuid.uuid4().hex)
        if created:
            incident = copy.deepcopy(record["data"])
            if incident["state"] == "open":
                incident["state"] = "investigating"
            incident["updated_at"] = event["created_at"]
            repository.put("incident", incident_id, incident, expected_revision=record["revision"])
        return event


def investigate(manager, incident_id, *, limit=50, after_sequence=None):
    """Return an ascending history page; never read referenced file contents."""
    if (type(limit) is not int or not 1 <= limit <= 50
            or after_sequence is not None and (type(after_sequence) is not int or after_sequence < 0)):
        raise _invalid("Investigation limit must be an integer from 1 to 50 and cursor a nonnegative integer.")
    manager_context(manager)
    incident = get_incident(manager, incident_id)
    events = manager.repository.events("incident:" + incident_id, limit=limit + 1, after_sequence=after_sequence)
    selected = events[:limit]
    has_more = len(events) > limit
    # Repository checks checksums and append-only guards. Project only our
    # documented payload fields, never generic external record contents.
    packet_events = [_event_view(event, incident_id) for event in selected]
    arguments = ["investigate", incident_id]
    if has_more:
        arguments += ["--after-sequence", str(selected[-1]["sequence"]), "--limit", str(limit)]
    context = manager_context(manager)
    return {"schema_version": "skills-auditor-investigation/v1", "incident": {key: incident[key] for key in _FIELDS},
            **context,
            "events": packet_events, "returned_events": len(selected), "has_more": has_more, "truncated": has_more,
            "continuation": {"after_sequence": selected[-1]["sequence"], "project_root": context["project_root"],
                             "incident_id": incident_id, "limit": limit} if has_more else None,
            "next_action": action(context["project_root"], arguments)["command"],
            "limits": {"max_events": limit, "note_max_bytes": 4096},
            "notice": "Local actor/tool labels are attribution, not authenticated identity. Disposition is not approval; verify and explicitly approve a new plan to restore authorization."}


def _proof(manager, incident, verification_id):
    from .status import _verification, read_status
    if not _identifier(verification_id):
        raise _invalid("A verification ID is required for remediation.")
    repository = manager.repository
    try:
        current = manager.get_installation(incident["installation_id"])
        status = read_status(manager, incident["installation_id"])
        if (status["severity"] != "ok" or status["verification_id"] != verification_id
                or current["skill_id"] != incident["skill_id"]
                or current["authorization"]["state"] != "valid"
                or current["authorization"]["grant_id"] == incident["grant_id"]):
            raise ValueError("a fresh current observation and new explicit grant are required")
        verification = repository.get("verification", verification_id)["data"]
        if (not _verification(verification, current) or not verification["valid"] or _failures(verification)
                or verification["approval"]["state"] != "valid"
                or verification["approval"]["requires_reapproval"] is not False
                or verification["approval"]["reason_codes"]):
            raise ValueError("verification is not clean")
        if not all(check["valid"] for check in manager._evidence_checks(current)):
            raise ValueError("completed receipt and explicitly approved transaction are not coherent")
        grant = repository.get("grant", current["authorization"]["grant_id"])["data"]
        receipt = repository.get("receipt", current["receipt_id"])["data"]
        grant_tx = repository.get("transaction", grant["transaction_id"])["data"]
        if (grant_tx["state"] != "completed" or grant_tx["grant_id"] != grant["grant_id"]
                or grant_tx["approved_plan_id"] != grant["plan_id"]
                or grant_tx["plan"]["plan_id"] != grant["plan_id"]
                or _time(grant["approved_at"]) < _time(incident["opened_at"])
                or _time(verification["observed_at"]) < _time(grant["approved_at"])):
            raise ValueError("grant has no later explicitly approved completed transaction")
        return {"kind": "remediated", "verification_id": verification_id, "grant_id": grant["grant_id"],
                "receipt_id": receipt["receipt_id"], "transaction_id": receipt["transaction_id"]}
    except (LifecycleError, KeyError, TypeError, ValueError, OSError) as error:
        raise _invalid("Incident resolution needs a fresh clean latest verification, a new explicit grant and completed receipt/transaction evidence.", "incident_resolution_unproven") from error


def resolve(manager, incident_id, *, actor, tool, verification_id=None, disposition=None, explanation=None):
    """Resolve evidence, never authorization; remediation requires fresh proof."""
    _actor(actor, tool)
    if (verification_id is None) == (disposition is None):
        raise _invalid("Choose remediation verification or an explicit non-remediation disposition.")
    if disposition is not None and (not _identifier(disposition) or not _text(explanation)):
        raise _invalid("Non-remediation requires a disposition code and bounded explanation.")
    repository = manager.repository
    with repository.atomic():
        record = _record(repository, incident_id)
        incident = copy.deepcopy(record["data"])
        if incident["state"] == "resolved":
            prior = incident["resolution"]
            if (verification_id is not None and prior.get("verification_id") == verification_id) or (disposition is not None and prior.get("disposition") == disposition and prior.get("explanation") == explanation):
                return incident
            raise _invalid("A resolved incident cannot replace its resolution evidence.", "incident_transition_invalid")
        if incident["state"] == "superseded":
            raise _invalid("Follow the superseding incident instead.", "incident_transition_invalid")
        resolution = (_proof(manager, incident, verification_id) if verification_id is not None else
                      {"kind": "non_remediation", "disposition": disposition, "explanation": explanation})
        resolution["resolved_at"] = utc_now()
        event, _ = _append(repository, incident_id, "resolved", {"resolution": resolution}, actor, tool, uuid.uuid4().hex)
        repository.put("incident-resolution", incident_id, {"event_id": event["payload"]["event_id"], "resolution": resolution})
        incident.update(state="resolved", resolution=resolution, updated_at=event["created_at"])
        repository.put("incident", incident_id, incident, expected_revision=record["revision"])
        return incident


def supersede(manager, incident_id, replacement_id, *, explanation, actor, tool):
    _actor(actor, tool)
    if not _text(explanation) or incident_id == replacement_id:
        raise _invalid("Superseding requires another related incident and an explanation.")
    repository = manager.repository
    with repository.atomic():
        record = _record(repository, incident_id)
        replacement = _record(repository, replacement_id)["data"]
        incident = copy.deepcopy(record["data"])
        if incident["state"] not in {"open", "investigating"} or replacement["state"] not in {"open", "investigating"} or incident["installation_id"] != replacement["installation_id"]:
            raise _invalid("Only open related incidents can supersede each other.", "incident_transition_invalid")
        event, _ = _append(repository, incident_id, "superseded", {"superseded_by": replacement_id, "explanation": explanation}, actor, tool, uuid.uuid4().hex)
        repository.put("incident-supersession", incident_id, {"event_id": event["payload"]["event_id"], "replacement_id": replacement_id, "explanation": explanation})
        incident.update(state="superseded", superseded_by=replacement_id, updated_at=event["created_at"])
        repository.put("incident", incident_id, incident, expected_revision=record["revision"])
        return incident
