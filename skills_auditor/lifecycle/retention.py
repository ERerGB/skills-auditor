"""Reviewed payload retention, recoverable quarantine, and explicit permanent purge.

Metadata, receipts and append-only events are never deleted. Expiration releases
only their payload-retention references. Collection moves exact unreferenced
objects to quarantine without freeing space. Purge is a separate grace-bound,
explicitly approved irreversible operation; partial deletion remains journaled.
"""

from contextlib import contextmanager, ExitStack
import copy
from datetime import datetime, timezone
import errno
import fcntl
import hashlib
import os
from pathlib import Path
import re
import stat
import uuid

from .common import LifecycleError, digest, durable_mkdir, fsync_directory, utc_now
from .locking import locked_paths
from .snapshots import locked_store, verify_snapshot, _read_metadata


_PREFIX = "skills-auditor-lifecycle-retention-"
_OPERATIONS = {"policy", "expire", "collect", "restore", "purge"}
_HASH = re.compile(r"^[a-f0-9]{64}$")
_STAGE = re.compile(r"^\.stage-([a-f0-9]{64})-[A-Za-z0-9_-]{6,64}$")
_PROOF_FIELDS = {"plan_id", "transaction_id", "receipt_id", "completion_event_sequence"}
_KINDS = ("skill", "version", "installation", "grant", "authorization", "receipt",
          "transaction", "batch", "verification", "incident", "payload-retention", "retention-policy", "invocation-override")


def _fail(code, message):
    raise LifecycleError("retention_" + code, message)


def _clock(value=None):
    if value is None:
        return datetime.now(timezone.utc)
    if not isinstance(value, datetime) or value.tzinfo is None:
        _fail("invalid_plan", "Retention time must be timezone-aware.")
    return value.astimezone(timezone.utc)


def _date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})", value):
        raise ValueError("RFC3339 timestamp required")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timestamp timezone required")
    return result.astimezone(timezone.utc)


def _ids(values):
    if not isinstance(values, (list, tuple)) or any(not isinstance(item, str) or not item or len(item) > 200
                                                   or "/" in item or "\\" in item or item in {".", ".."}
                                                   or any(ord(c) < 32 for c in item) for item in values):
        _fail("invalid_plan", "Selectors must be bounded IDs, never filesystem paths.")
    return sorted(set(values))


def _completion_evidence(manager, operation, projection):
    """Verify retained authorization history, not a current physical postcondition.

    Expiration tombstones release payload roots only. These checks detect
    inconsistent local records; they are neither signatures nor actor identity.
    """
    try:
        if (not _PROOF_FIELDS.issubset(projection)
                or any(not isinstance(projection[key], str) or not projection[key] for key in ("plan_id", "transaction_id", "receipt_id"))
                or type(projection["completion_event_sequence"]) is not int or projection["completion_event_sequence"] < 1):
            raise ValueError("missing retention approval references")
        row = manager.repository.get("retention-transaction", projection["transaction_id"])
        if not row:
            raise ValueError("missing retention transaction")
        tx = row["data"]
        _validate_transaction(manager, tx, projection["transaction_id"])
        plan = tx["plan"]
        if (tx["state"] != "completed" or plan["operation"] != operation
                or tx["approved_plan_id"] != projection["plan_id"] or plan["plan_id"] != projection["plan_id"]
                or tx["receipt_id"] != projection["receipt_id"]
                or tx["completion_event_sequence"] != projection["completion_event_sequence"]):
            raise ValueError("retention transaction is not the approved completion")
        row = manager.repository.get("retention-receipt", projection["receipt_id"])
        expected = {"schema_version": _PREFIX + "receipt/v1", "receipt_id": projection["receipt_id"],
                    "transaction_id": projection["transaction_id"], "plan_id": plan["plan_id"], "operation": operation,
                    "status": "completed", "objects": plan["objects"], "expires": plan["expires"], "permanently_deleted": operation == "purge"}
        if not row or any(row["data"].get(key) != value for key, value in expected.items()):
            raise ValueError("retention receipt does not bind the approved operation")
        receipt = row["data"]
        _date(receipt["completed_at"])
        events = manager.repository.events("retention:" + projection["transaction_id"], limit=1,
                                           after_sequence=projection["completion_event_sequence"] - 1)
        if (not events or events[0]["sequence"] != projection["completion_event_sequence"]
                or events[0]["event_type"] != "retention_completed" or events[0]["actor"] != tx["actor"]
                or events[0]["tool"] != "retention"
                or events[0]["payload"] != {"receipt_id": projection["receipt_id"], "operation": operation}):
            raise ValueError("retention completion event is missing or inconsistent")
        return plan, receipt
    except (KeyError, TypeError, ValueError, LifecycleError) as exc:
        raise LifecycleError("retention_evidence_invalid", "Retention metadata lacks coherent explicit approval and completion evidence.") from exc


def _policy(manager):
    record = manager.repository.get("retention-policy", "default")
    if not record:
        return {"keep_recent": 3, "pin_version_ids": []}
    data = record["data"]
    if set(data) != {"keep_recent", "pin_version_ids"} | _PROOF_FIELDS:
        _fail("references_invalid", "Retention policy is missing approval evidence.")
    value = {key: data[key] for key in ("keep_recent", "pin_version_ids")}
    if (type(value["keep_recent"]) is not int
            or not 0 <= value["keep_recent"] <= 100 or _ids(value["pin_version_ids"]) != value["pin_version_ids"]):
        _fail("references_invalid", "Retention policy is malformed.")
    plan, _ = _completion_evidence(manager, "policy", data)
    if plan["policy_after"] != value:
        _fail("evidence_invalid", "Retention policy differs from the explicitly approved values.")
    return value


def _references(manager):
    """Follow only effective retention roots, not every historical version edge."""
    repository = manager.repository
    records = {kind: {record["id"]: record for record in repository.list(kind)} for kind in _KINDS}
    protected = {}
    visited = set()
    evidence_events = {}
    policy = _policy(manager)
    expirations = set()

    def get(kind, identifier):
        value = records.get(kind, {}).get(identifier)
        if value is None:
            raise ValueError("missing {} reference {}".format(kind, identifier))
        return value["data"]

    def expired(kind, identifier):
        if (kind, identifier) in expirations:
            return True
        marker = records["payload-retention"].get(kind + ":" + identifier)
        if marker is None:
            return False
        data = marker["data"]
        if (set(data) != {"kind", "id", "state", "expired_at"} | _PROOF_FIELDS
                or data.get("kind") != kind or data.get("id") != identifier or data.get("state") != "expired"):
            raise ValueError("invalid expiration marker")
        plan, receipt = _completion_evidence(manager, "expire", data)
        if {"kind": kind, "id": identifier} not in plan["expires"] or data["expired_at"] != receipt["completed_at"]:
            raise ValueError("expiration does not bind this evidence and completion time")
        expirations.add((kind, identifier))
        return True

    def protect(kind, identifier, reason):
        if not isinstance(identifier, str):
            raise ValueError("invalid reference identity")
        key = (kind, identifier, reason)
        if key in visited:
            return
        visited.add(key)
        if kind == "capture-evidence":
            from .capture import get_record
            # Committed local diagnostics have no Skill version/payload edge.
            # Validate the leaf without treating its log path as an object root.
            get_record(repository, identifier, manager.project_root)
            return
        data = get(kind, identifier)
        if kind == "version":
            snapshot = data["snapshot"]
            tree_hash = snapshot["snapshot_tree_sha256"]
            if not isinstance(tree_hash, str) or not _HASH.fullmatch(tree_hash):
                raise ValueError("invalid version digest")
            if data["version_id"] != identifier or snapshot["path"] != str(manager.store_root / tree_hash / "tree"):
                raise ValueError("foreign version storage")
            protected.setdefault(tree_hash, set()).add(reason)
        elif kind in {"receipt", "grant", "verification"}:
            protect("version", data["version_id"], reason)
        elif kind == "installation":
            protect("version", data["version_id"], reason)
            protect("receipt", data["receipt_id"], reason)
        elif kind == "skill":
            for version_id, version in records["version"].items():
                if version["data"]["skill_id"] == identifier:
                    protect("version", version_id, reason)
        elif kind == "transaction":
            protect_plan(data["plan"], reason)
        elif kind == "incident":
            protect("version", data["version_id"], reason)
            for field, target_kind in (("opening_verification_id", "verification"),
                                       ("latest_verification_id", "verification"), ("grant_id", "grant")):
                if data.get(field):
                    protect(target_kind, data[field], reason)
            for field, target_kind in (("verification_id", "verification"), ("receipt_id", "receipt"),
                                       ("transaction_id", "transaction"), ("grant_id", "grant")):
                if (data.get("resolution") or {}).get(field):
                    protect(target_kind, data["resolution"][field], reason)
            events = repository.events("incident:" + identifier)
            evidence_events[identifier] = events
            for event in events:
                for reference in event["payload"].get("evidence_refs", []):
                    if set(reference) != {"kind", "id"} or reference["kind"] not in {"skill", "installation", "version", "receipt", "grant", "verification", "transaction", "incident", "capture-evidence"}:
                        raise ValueError("unknown incident evidence reference")
                    protect(reference["kind"], reference["id"], reason)
        else:
            raise ValueError("unknown retention reference kind")

    def protect_plan(plan, reason):
        # A parent batch owns reviewed candidate bytes before a version or child
        # transaction exists. Do not require a registered candidate version.
        snapshot = plan["version"]["snapshot"]
        tree_hash = snapshot["snapshot_tree_sha256"]
        if not isinstance(tree_hash, str) or not _HASH.fullmatch(tree_hash) or snapshot["path"] != str(manager.store_root / tree_hash / "tree"):
            raise ValueError("invalid transaction snapshot")
        protected.setdefault(tree_hash, set()).add(reason)
        if plan["before"]:
            protect("version", plan["before"]["version_id"], reason)

    try:
        for key, marker in records["payload-retention"].items():
            data = marker["data"]
            if data["kind"] not in {"receipt", "incident"} or key != data["kind"] + ":" + data["id"]:
                raise ValueError("unknown expiration target")
            get(data["kind"], data["id"])
            expired(data["kind"], data["id"])
        for identifier, record in records["version"].items():
            data = record["data"]
            if data["version_id"] != identifier or not isinstance(data["skill_id"], str):
                raise ValueError("invalid version identity")
            snapshot = data["snapshot"]
            if (set(snapshot) != {"source_tree_sha256", "snapshot_tree_sha256", "normalization", "path"}
                    or any(not isinstance(snapshot[key], str) or not _HASH.fullmatch(snapshot[key]) for key in ("source_tree_sha256", "snapshot_tree_sha256"))
                    or snapshot["normalization"] != "remove-write-bits/v1"
                    or snapshot["path"] != str(manager.store_root / snapshot["snapshot_tree_sha256"] / "tree")):
                raise ValueError("invalid version snapshot")
            if _date(data["created_at"]) > _clock():
                raise ValueError("future version creation time")
        for identifier, record in records["installation"].items():
            data = record["data"]
            if data["installation_id"] != identifier or data["state"] not in {"active", "disabled", "archived", "uninstalled"}:
                raise ValueError("invalid installation state")
            get("version", data["version_id"])
            if data["state"] != "uninstalled":
                protect("version", data["version_id"], "installation:" + identifier)
                protect("receipt", data["receipt_id"], "current-receipt:" + identifier)
                if data["authorization"]["grant_id"]:
                    protect("grant", data["authorization"]["grant_id"], "current-grant:" + identifier)
        for identifier, record in records["receipt"].items():
            if record["data"]["receipt_id"] != identifier or record["data"]["status"] != "completed":
                raise ValueError("invalid completed receipt")
            if not expired("receipt", identifier):
                protect("receipt", identifier, "receipt:" + identifier)
        for identifier, record in records["transaction"].items():
            data = record["data"]
            if data["transaction_id"] != identifier or data["state"] not in {"prepared", "applying", "recovery_needed", "completed", "compensated"}:
                raise ValueError("unknown transaction state")
            if data["state"] not in {"completed", "compensated"}:
                protect("transaction", identifier, "in-flight:" + identifier)
            elif data["state"] == "completed":
                receipt = get("receipt", data["receipt_id"])
                if receipt["transaction_id"] != identifier:
                    raise ValueError("transaction receipt mismatch")
                if not expired("receipt", data["receipt_id"]):
                    protect("receipt", data["receipt_id"], "transaction:" + identifier)
        for identifier in records["batch"]:
            from .batch import BatchManager
            # This metadata-only reader requires real completion/inverse proof;
            # a merely terminal-shaped projection cannot release payload roots.
            data = BatchManager(manager).inspect(identifier)
            if data["state"] not in {"completed", "compensated"}:
                reason = "in-flight-batch:" + identifier
                for child in data["plan"]["children"]:
                    protect_plan(child["plan"], reason)
                    if child["compensates_transaction_id"] is not None:
                        protect("transaction", child["compensates_transaction_id"], reason)
                for unresolved in data["plan"]["uncompensated"]:
                    protect("transaction", unresolved["transaction_id"], reason)
        for identifier, record in records["incident"].items():
            from .incidents import get_incident
            data = get_incident(manager, identifier)
            if data["incident_id"] != identifier or data["state"] not in {"open", "investigating", "resolved", "superseded"}:
                raise ValueError("invalid incident")
            if data["state"] in {"open", "investigating"} or not expired("incident", identifier):
                protect("incident", identifier, "incident:" + identifier)
        for identifier, record in records["invocation-override"].items():
            from .invocation import get_override
            data = get_override(manager, identifier)
            if data["state"] not in {"active", "revoked"}:
                raise ValueError("unknown invocation override state")
            get("version", data["version_id"])
            if data["state"] == "active" and _date(data["expires_at"]) > _clock():
                protect("version", data["version_id"], "invocation-override:" + identifier)
        for version_id in policy["pin_version_ids"]:
            protect("version", version_id, "explicit-pin")
        by_skill = {}
        for record in records["version"].values():
            version = record["data"]
            by_skill.setdefault(version["skill_id"], []).append(version)
        for versions in by_skill.values():
            for version in sorted(versions, key=lambda value: (_date(value["created_at"]), value["version_id"]), reverse=True)[:policy["keep_recent"]]:
                protect("version", version["version_id"], "recent-rollback")
        fingerprint = digest({"records": records, "incident_events": evidence_events})
    except (KeyError, TypeError, ValueError, RecursionError) as exc:
        raise LifecycleError("retention_references_invalid", "Retention references are incomplete or malformed.", details={"error": str(exc)}) from exc
    return {key: sorted(value) for key, value in sorted(protected.items())}, fingerprint, records, policy


def _open_dir(path, **kwargs):
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, **kwargs)


def _inventory(path):
    """Capture non-following inode/content inventory for exact recovery/deletion."""
    root_fd = _open_dir(path)
    root_info = os.fstat(root_fd)
    entries = []

    def walk(fd, prefix):
        for name in sorted(os.listdir(fd)):
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            relative = prefix + name
            row = {"path": relative, "identity": [info.st_dev, info.st_ino], "mode": stat.S_IMODE(info.st_mode)}
            if stat.S_ISDIR(info.st_mode):
                row["kind"] = "directory"
                child = _open_dir(name, dir_fd=fd)
                try:
                    walk(child, relative + "/")
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode):
                row["kind"] = "file"
                child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                try:
                    if [os.fstat(child).st_dev, os.fstat(child).st_ino] != row["identity"]:
                        _fail("object_changed", "Object changed during inventory.")
                    content = hashlib.sha256()
                    while True:
                        chunk = os.read(child, 1024 * 1024)
                        if not chunk:
                            break
                        content.update(chunk)
                    row["sha256"] = content.hexdigest()
                finally:
                    os.close(child)
            elif stat.S_ISLNK(info.st_mode):
                row.update(kind="symlink", link=os.readlink(name, dir_fd=fd))
            else:
                _fail("unsafe_object", "Special files cannot be retained or purged.")
            entries.append(row)

    try:
        walk(root_fd, "")
        return {"identity": [root_info.st_dev, root_info.st_ino], "root_mode": stat.S_IMODE(root_info.st_mode), "inventory": entries}
    finally:
        os.close(root_fd)


@contextmanager
def _lease(stage):
    fd = _open_dir(stage)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EACCES):
                _fail("stage_busy", "Stage belongs to a live materializer; preserve it.")
            raise
        yield
    finally:
        os.close(fd)


@contextmanager
def _locks(manager):
    manager._state_safety()
    with locked_paths([manager.state_root / "registry"]):
        if manager.store_root.exists():
            with locked_store(manager.store_root):
                yield
        else:
            yield


def _safe_store(manager):
    root = manager.store_root
    if root.is_symlink():
        _fail("unsafe_object", "Store root may not be a symlink.")
    if not root.exists():
        return
    for path in root.iterdir():
        if path.is_symlink() or not path.is_dir():
            _fail("unknown_store", "Store contains an unknown or symlink entry.")
        if path.name != ".quarantine" and not _HASH.fullmatch(path.name) and not path.name.startswith(".stage-"):
            _fail("unknown_store", "Store contains an unknown object name.")


def _object(manager, name, created_at):
    path = manager.store_root / name
    stage = _STAGE.fullmatch(name)
    if not _HASH.fullmatch(name) and not stage:
        _fail("unsafe_object", "Only exact digest objects or recognized staging IDs are selectable.")
    intent = _read_metadata(path / "stage.json")
    expected_hash = stage.group(1) if stage else name
    if (not isinstance(intent, dict) or intent.get("schema_version") != "skills-auditor-snapshot-stage/v1"
            or intent.get("snapshot_tree_sha256") != expected_hash
            or not isinstance(intent.get("source_tree_sha256"), str)
            or not _HASH.fullmatch(intent["source_tree_sha256"])
            or intent.get("normalization") != "remove-write-bits/v1"):
        _fail("unsafe_object", "Stage intent does not establish this exact object's provenance.")
    if not stage:
        verify_snapshot({key: intent[key] for key in ("source_tree_sha256", "snapshot_tree_sha256", "normalization")}
                        | {"path": str(path / "tree")})
    captured = _inventory(path)
    qid = digest({"name": name, "identity": captured["identity"], "created_at": created_at})
    return {"object_id": name, "name": name, "kind": "stage" if stage else "snapshot",
            "snapshot_tree_sha256": expected_hash, "quarantine_id": qid,
            "quarantine_path": str(manager.store_root / ".quarantine" / qid), **captured}


def _incomplete(repository, except_id=None):
    for record in repository.list("retention-transaction"):
        if record["id"] != except_id and record["data"].get("state") not in {"completed", "compensated"}:
            raise LifecycleError("retention_recovery_required", "Inspect and resume the incomplete retention transaction first.",
                                 details={"transaction_id": record["id"]})


def _build(manager, operation, options, created_at, *, now):
    _safe_store(manager)
    protected, fingerprint, records, current_policy = _references(manager)
    result = {"schema_version": _PREFIX + "plan/v1", "operation": operation,
              "project_root": str(manager.project_root), "created_at": created_at,
              "options": options, "reference_fingerprint": fingerprint, "objects": [],
              "protected": protected, "policy_before": current_policy, "policy_after": current_policy,
              "expires": []}
    if operation == "policy":
        after = {"keep_recent": current_policy["keep_recent"] if options["keep_recent"] is None else options["keep_recent"],
                 "pin_version_ids": current_policy["pin_version_ids"] if options["pin_version_ids"] is None else options["pin_version_ids"]}
        if any(identifier not in records["version"] for identifier in after["pin_version_ids"]):
            _fail("references_invalid", "Pinned version is unknown.")
        result["policy_after"] = after
    elif operation == "expire":
        for kind, identifiers in (("receipt", options["receipt_ids"]), ("incident", options["incident_ids"])):
            for identifier in identifiers:
                record = records[kind].get(identifier)
                if record is None:
                    _fail("references_invalid", "Cannot expire unknown evidence.")
                if kind == "incident" and record["data"]["state"] not in {"resolved", "superseded"}:
                    _fail("referenced", "Unresolved incidents may not expire payload retention.")
                if kind == "receipt":
                    for installation in records["installation"].values():
                        data = installation["data"]
                        if data["state"] != "uninstalled" and (data["receipt_id"] == identifier
                                or data["authorization"]["grant_id"] == record["data"]["grant_id"]):
                            _fail("referenced", "Current installation/grant receipts must retain their payloads.")
                result["expires"].append({"kind": kind, "id": identifier})
    elif operation == "collect":
        names = sorted(path.name for path in manager.store_root.iterdir()) if manager.store_root.exists() else []
        requested = options["object_ids"]
        if any(not _HASH.fullmatch(name) or name not in names for name in requested):
            _fail("unsafe_object", "Selected digest object does not exist.")
        if any(name not in names or not _STAGE.fullmatch(name) for name in options["stage_names"]):
            _fail("unsafe_object", "Selected stage does not exist or has unknown ownership.")
        for name in names:
            if name == ".quarantine":
                continue
            if name.startswith(".stage-") and name not in options["stage_names"]:
                result["protected"][name] = ["stage-requires-explicit-selection"]
                continue
            if (requested or options["stage_names"]) and _HASH.fullmatch(name) and name not in requested:
                continue
            if name in protected:
                if name in requested:
                    _fail("referenced", "Selected snapshot has retained references.")
                continue
            with ExitStack() as stack:
                if name.startswith(".stage-"):
                    stack.enter_context(_lease(manager.store_root / name))
                obj = _object(manager, name, created_at)
                if obj["snapshot_tree_sha256"] in protected:
                    if obj["kind"] == "stage":
                        _fail("referenced", "Selected stage is referenced by a live transaction/version.")
                    continue
                result["objects"].append(obj)
    else:
        object_records = {row["id"]: row["data"] for row in manager.repository.list("retention-object")}
        for identifier, record in object_records.items():
            if record.get("state") not in {"quarantined", "purging", "purged", "restored"}:
                _fail("unsafe_object", "Quarantine metadata has an unknown state.")
            _validate_object(manager, record["object"])
            if record["object"]["quarantine_id"] != identifier:
                _fail("unsafe_object", "Quarantine metadata identity does not match its record.")
        selected = options["object_ids"] or sorted(key for key, value in object_records.items() if value["state"] == "quarantined")
        for identifier in selected:
            record = object_records.get(identifier)
            if not record or record.get("state") != "quarantined":
                _fail("invalid_transition", "Only retained quarantined objects may restore or purge.")
            obj = record["object"]
            if obj["quarantine_id"] != identifier or obj["quarantine_path"] != str(manager.store_root / ".quarantine" / identifier):
                _fail("unsafe_object", "Quarantine identity or path is invalid.")
            if _inventory(Path(obj["quarantine_path"])) != {key: obj[key] for key in ("identity", "root_mode", "inventory")}:
                _fail("object_changed", "Quarantined object changed since collection.")
            if operation == "restore":
                if os.path.lexists(manager.store_root / obj["name"]):
                    _fail("foreign_state", "Restore destination is already occupied.")
            else:
                if obj["snapshot_tree_sha256"] in protected:
                    _fail("referenced", "Quarantined object acquired a retained reference.")
                age = (now - _date(record["quarantined_at"])).total_seconds()
                if age < options["grace_seconds"]:
                    _fail("grace_pending", "Quarantined object has not passed its approved grace period.")
            result["objects"].append(obj)
    result["plan_id"] = digest(result)
    return result


def plan_retention(manager, operation, *, receipt_ids=(), incident_ids=(), pin_version_ids=None,
                   keep_recent=None, object_ids=(), stage_names=(), grace_seconds=604800, now=None):
    """Create a read-only plan; None policy values preserve current settings."""
    if not isinstance(operation, str) or operation not in _OPERATIONS:
        _fail("invalid_plan", "Unknown retention operation.")
    options = {"receipt_ids": _ids(receipt_ids), "incident_ids": _ids(incident_ids),
               "pin_version_ids": None if pin_version_ids is None else _ids(pin_version_ids),
               "keep_recent": keep_recent, "object_ids": _ids(object_ids), "stage_names": _ids(stage_names),
               "grace_seconds": grace_seconds}
    _validate_options(operation, options)
    timestamp = _clock(now)
    with _locks(manager):
        _incomplete(manager.repository)
        try:
            return _build(manager, operation, options, timestamp.isoformat().replace("+00:00", "Z"), now=timestamp)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise LifecycleError("retention_inspection_failed", "Cannot safely inspect retention state.", details={"error": str(exc)[:1000]}) from exc


def _validate_options(operation, options):
    if set(options) != {"receipt_ids", "incident_ids", "pin_version_ids", "keep_recent", "object_ids", "stage_names", "grace_seconds"}:
        _fail("invalid_plan", "Unexpected retention options.")
    for key in ("receipt_ids", "incident_ids", "object_ids", "stage_names"):
        if _ids(options[key]) != options[key]:
            _fail("invalid_plan", "Selectors must be canonical unique lists.")
    if options["pin_version_ids"] is not None and _ids(options["pin_version_ids"]) != options["pin_version_ids"]:
        _fail("invalid_plan", "Pins must be canonical unique lists.")
    if options["keep_recent"] is not None and (type(options["keep_recent"]) is not int or not 0 <= options["keep_recent"] <= 100):
        _fail("invalid_plan", "Recent rollback retention must be an integer from 0 to 100.")
    if type(options["grace_seconds"]) is not int or not 0 <= options["grace_seconds"] <= 31536000:
        _fail("invalid_plan", "Purge grace must be an integer from 0 to 31536000 seconds.")
    if ((operation != "policy" and (options["keep_recent"] is not None or options["pin_version_ids"] is not None))
            or (operation != "expire" and (options["receipt_ids"] or options["incident_ids"]))
            or (operation not in {"collect", "restore", "purge"} and options["object_ids"])
            or (operation != "collect" and options["stage_names"])):
        _fail("invalid_plan", "Options do not belong to this retention operation.")


def _validate_object(manager, obj):
    """Validate stored descriptors before using any persisted path or selector."""
    if (not isinstance(obj, dict)
            or set(obj) != {"object_id", "name", "kind", "snapshot_tree_sha256", "quarantine_id", "quarantine_path", "identity", "root_mode", "inventory"}
            or obj["object_id"] != obj["name"] or not _HASH.fullmatch(obj["quarantine_id"])
            or not _HASH.fullmatch(obj["snapshot_tree_sha256"])
            or obj["quarantine_path"] != str(manager.store_root / ".quarantine" / obj["quarantine_id"])
            or not (_HASH.fullmatch(obj["name"]) or _STAGE.fullmatch(obj["name"]))
            or obj["kind"] != ("stage" if obj["name"].startswith(".stage-") else "snapshot")
            or obj["snapshot_tree_sha256"] != (_STAGE.fullmatch(obj["name"]).group(1) if obj["kind"] == "stage" else obj["name"])):
        raise ValueError("unsafe object")

    def identity(value):
        return isinstance(value, list) and len(value) == 2 and all(type(item) is int and item >= 0 for item in value)

    def mode(value):
        return type(value) is int and 0 <= value <= 0o7777

    if not identity(obj["identity"]) or not mode(obj["root_mode"]) or not isinstance(obj["inventory"], list):
        raise ValueError("invalid object inventory")
    seen = {}
    for entry in obj["inventory"]:
        kind = entry["kind"]
        fields = {"path", "kind", "identity", "mode"} | ({"sha256"} if kind == "file" else {"link"} if kind == "symlink" else set())
        if kind not in {"file", "directory", "symlink"} or set(entry) != fields or not identity(entry["identity"]) or not mode(entry["mode"]):
            raise ValueError("invalid inventory entry")
        path = Path(entry["path"])
        if (path.is_absolute() or not path.parts or any(part in {".", ".."} for part in path.parts)
                or str(path) != entry["path"] or entry["path"] in seen or any(ord(c) < 32 for c in entry["path"])):
            raise ValueError("unsafe inventory path")
        if (kind == "file" and not _HASH.fullmatch(entry["sha256"])) or (kind == "symlink" and not isinstance(entry["link"], str)):
            raise ValueError("invalid inventory content")
        seen[entry["path"]] = (kind, len(seen))
    for path, (_, index) in seen.items():
        for parent in Path(path).parents:
            if str(parent) != "." and (str(parent) not in seen or seen[str(parent)][0] != "directory" or seen[str(parent)][1] <= index):
                raise ValueError("inventory must contain postorder parent directories")


def _validate_plan(manager, plan):
    fields = {"schema_version", "operation", "project_root", "created_at", "options", "reference_fingerprint",
              "objects", "protected", "policy_before", "policy_after", "expires", "plan_id"}
    try:
        if not isinstance(plan, dict) or set(plan) != fields or plan["operation"] not in _OPERATIONS:
            raise ValueError("plan shape")
        if (plan["schema_version"] != _PREFIX + "plan/v1" or plan["project_root"] != str(manager.project_root)
                or digest({key: value for key, value in plan.items() if key != "plan_id"}) != plan["plan_id"]):
            raise ValueError("plan checksum or project")
        _date(plan["created_at"])
        _validate_options(plan["operation"], plan["options"])
        for obj in plan["objects"]:
            _validate_object(manager, obj)
    except (KeyError, TypeError, ValueError) as exc:
        raise LifecycleError("retention_invalid_plan", "Malformed retention plan.", details={"error": str(exc)}) from exc


def _validate_transaction(manager, tx, transaction_id):
    try:
        _validate_plan(manager, tx["plan"])
        if (tx["transaction_id"] != transaction_id or tx["state"] not in {"prepared", "recovery_needed", "completed", "compensated"}
                or tx["approved_plan_id"] != tx["plan"]["plan_id"]
                or len(tx["objects"]) != len(tx["plan"]["objects"])):
            raise ValueError("invalid transaction identity or state")
        for obj, step in zip(tx["plan"]["objects"], tx["objects"]):
            if (set(step) != {"object", "state", "deleted", "pending_delete"} or step["object"] != obj
                    or step["state"] not in {"pending", "intent", "completed", "compensating", "compensated"}):
                raise ValueError("journal step differs from approved object")
            paths = [entry["path"] for entry in obj["inventory"]]
            deleted = step["deleted"]
            pending = step["pending_delete"]
            if (not isinstance(deleted, list) or deleted != paths[:len(deleted)]
                    or (pending is not None and (len(deleted) >= len(paths) or pending != paths[len(deleted)]))
                    or (tx["plan"]["operation"] != "purge" and (deleted or pending is not None))):
                raise ValueError("journal deletion cursor differs from approved inventory")
    except (KeyError, TypeError, ValueError) as exc:
        raise LifecycleError("retention_evidence_invalid", "Retention recovery journal is malformed.", details={"error": str(exc)}) from exc


def _put(repository, kind, identifier, value):
    old = repository.get(kind, identifier)
    return repository.put(kind, identifier, value, expected_revision=old["revision"] if old else 0)


def _save(manager, tx):
    _put(manager.repository, "retention-transaction", tx["transaction_id"], tx)


def _checkpoint(callback, name, tx):
    if callback:
        callback(name, copy.deepcopy(tx))


def _remaining(path, obj, deleted, *, temporary_root=False, delete_modes=False):
    if not os.path.lexists(path):
        return not [entry for entry in obj["inventory"] if entry["path"] not in deleted]
    actual = _inventory(path)
    modes = {obj["root_mode"], obj["root_mode"] | 0o700} if temporary_root or delete_modes else {obj["root_mode"]}
    if actual["identity"] != obj["identity"] or actual["root_mode"] not in modes:
        return False
    expected = [copy.deepcopy(entry) for entry in obj["inventory"] if entry["path"] not in deleted]
    observed = actual["inventory"]
    # Only the exact journaled owner-access transition is ours. Do not mask
    # arbitrary permission differences (e.g. a foreign world-writable mode).
    if delete_modes:
        expected_by_path = {entry["path"]: entry for entry in expected}
        for entry in observed:
            planned = expected_by_path.get(entry["path"])
            if planned and entry["kind"] == planned["kind"] == "directory":
                if entry["mode"] not in {planned["mode"], planned["mode"] | 0o700}:
                    return False
                entry["mode"] = planned["mode"]
    return expected == observed


def _restore_root_mode(path, obj):
    descriptor = _open_dir(path)
    try:
        current = os.fstat(descriptor)
        if ([current.st_dev, current.st_ino] != obj["identity"]
                or stat.S_IMODE(current.st_mode) not in {obj["root_mode"], obj["root_mode"] | 0o700}):
            _fail("foreign_state", "Object identity or mode differs from the journaled transition.")
        if stat.S_IMODE(current.st_mode) != obj["root_mode"]:
            os.fchmod(descriptor, obj["root_mode"])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _delete_entry(path, entry, root_identity, directories):
    """Delete exactly one journaled child through non-following directory FDs."""
    root_fd = _open_dir(path)
    opened = [root_fd]
    try:
        if [os.fstat(root_fd).st_dev, os.fstat(root_fd).st_ino] != root_identity:
            _fail("foreign_state", "Purge object root was replaced.")
        fd = root_fd
        parent_parts = []
        for component in Path(entry["path"]).parts[:-1]:
            fd = _open_dir(component, dir_fd=fd)
            opened.append(fd)
            parent_parts.append(component)
            if [os.fstat(fd).st_dev, os.fstat(fd).st_ino] != directories["/".join(parent_parts)]:
                _fail("foreign_state", "Purge parent directory was replaced.")
        leaf = Path(entry["path"]).name
        info = os.stat(leaf, dir_fd=fd, follow_symlinks=False)
        if [info.st_dev, info.st_ino] != entry["identity"]:
            _fail("foreign_state", "Purge entry inode changed; preserve it.")
        if entry["kind"] == "file":
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != entry["mode"]:
                _fail("foreign_state", "Purge file type or mode changed.")
            file_fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            try:
                if [os.fstat(file_fd).st_dev, os.fstat(file_fd).st_ino] != entry["identity"]:
                    _fail("foreign_state", "Purge file changed while opening.")
                content = hashlib.sha256()
                while True:
                    chunk = os.read(file_fd, 1024 * 1024)
                    if not chunk:
                        break
                    content.update(chunk)
                if content.hexdigest() != entry["sha256"]:
                    _fail("foreign_state", "Purge file bytes changed after intent; preserve them.")
            finally:
                os.close(file_fd)
        elif entry["kind"] == "symlink" and (not stat.S_ISLNK(info.st_mode) or os.readlink(leaf, dir_fd=fd) != entry["link"]):
            _fail("foreign_state", "Purge symlink changed after intent.")
        current = os.stat(leaf, dir_fd=fd, follow_symlinks=False)
        if [current.st_dev, current.st_ino] != entry["identity"]:
            _fail("foreign_state", "Purge entry was replaced before deletion.")
        os.fchmod(fd, stat.S_IMODE(os.fstat(fd).st_mode) | 0o700)
        if entry["kind"] == "directory":
            os.rmdir(leaf, dir_fd=fd)
        else:
            os.unlink(leaf, dir_fd=fd)
        os.fsync(fd)
    finally:
        for fd in reversed(opened):
            os.close(fd)


def _rename_object(source, destination, obj):
    # macOS requires owner write permission on a directory moved between
    # parents (its '..' changes). The durable intent precedes this temporary
    # root-mode change; payload child permissions and bytes remain untouched.
    descriptor = _open_dir(source)
    try:
        if [os.fstat(descriptor).st_dev, os.fstat(descriptor).st_ino] != obj["identity"]:
            _fail("foreign_state", "Retention object was replaced before rename.")
        if stat.S_IMODE(os.fstat(descriptor).st_mode) not in {obj["root_mode"], obj["root_mode"] | 0o700}:
            _fail("foreign_state", "Retention root mode differs from the journaled transition.")
        os.fchmod(descriptor, obj["root_mode"] | 0o700)
        try:
            os.rename(source, destination)
            fsync_directory(source.parent)
            fsync_directory(destination.parent)
        finally:
            if stat.S_IMODE(os.fstat(descriptor).st_mode) not in {obj["root_mode"], obj["root_mode"] | 0o700}:
                _fail("foreign_state", "Retention root acquired a foreign permission change.")
            os.fchmod(descriptor, obj["root_mode"])
            os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _execute(manager, tx, checkpoint):
    repository, plan = manager.repository, tx["plan"]
    operation = plan["operation"]
    try:
        _checkpoint(checkpoint, "retention:prepared", tx)
        if operation in {"policy", "expire"}:
            with repository.atomic():
                receipt = _complete(manager, tx)
                proof = {"plan_id": plan["plan_id"], "transaction_id": tx["transaction_id"],
                         "receipt_id": receipt["receipt_id"], "completion_event_sequence": tx["completion_event_sequence"]}
                if operation == "policy":
                    _put(repository, "retention-policy", "default", {**plan["policy_after"], **proof})
                else:
                    for reference in plan["expires"]:
                        marker = {**reference, "state": "expired", "expired_at": receipt["completed_at"], **proof}
                        _put(repository, "payload-retention", reference["kind"] + ":" + reference["id"], marker)
                return receipt
        for index, step in enumerate(tx["objects"]):
            obj = step["object"]
            protected, _, _, _ = _references(manager)
            if operation != "restore" and obj["snapshot_tree_sha256"] in protected:
                _fail("referenced", "Object acquired a retained reference; do not continue.")
            original = manager.store_root / obj["name"]
            quarantine = Path(obj["quarantine_path"])
            if step["state"] == "completed":
                destination = original if operation == "restore" else quarantine
                if operation == "purge":
                    if os.path.lexists(quarantine):
                        _fail("foreign_state", "Purged object path was recreated.")
                elif not _remaining(destination, obj, []):
                    _fail("foreign_state", "Completed retention object changed.")
                continue
            step["state"] = "intent"
            _save(manager, tx)
            _checkpoint(checkpoint, "retention:{}:intent".format(index), tx)
            protected, _, _, _ = _references(manager)
            if operation != "restore" and obj["snapshot_tree_sha256"] in protected:
                _fail("referenced", "Object acquired a retained reference at the effect boundary.")
            if operation == "purge":
                record = repository.get("retention-object", obj["quarantine_id"])
                if not record or record["data"]["state"] not in {"quarantined", "purging"}:
                    _fail("invalid_transition", "Purge requires its retained quarantine tombstone.")
                _put(repository, "retention-object", obj["quarantine_id"], {**record["data"], "state": "purging"})
                pending = step.get("pending_delete")
                if pending and not os.path.lexists(quarantine / pending):
                    step["deleted"].append(pending)
                    step["pending_delete"] = None
                    _save(manager, tx)
                if not _remaining(quarantine, obj, step["deleted"], delete_modes=True):
                    _fail("foreign_state", "Purge remainder differs from the reviewed inventory.")
                for entry in obj["inventory"]:
                    if entry["path"] in step["deleted"]:
                        continue
                    step["pending_delete"] = entry["path"]
                    _save(manager, tx)
                    _checkpoint(checkpoint, "retention:{}:delete_intent".format(index), tx)
                    protected, _, _, _ = _references(manager)
                    if obj["snapshot_tree_sha256"] in protected:
                        _fail("referenced", "Purge acquired a retained reference before deletion.")
                    _delete_entry(quarantine, entry, obj["identity"],
                                  {item["path"]: item["identity"] for item in obj["inventory"] if item["kind"] == "directory"})
                    _checkpoint(checkpoint, "retention:{}:delete_effect".format(index), tx)
                    step["deleted"].append(entry["path"])
                    step["pending_delete"] = None
                    _save(manager, tx)
                if os.path.lexists(quarantine):
                    if not _remaining(quarantine, obj, step["deleted"], delete_modes=True):
                        _fail("foreign_state", "Purge root acquired foreign entries.")
                    quarantine.rmdir()
                    fsync_directory(quarantine.parent)
            else:
                source, destination = (original, quarantine) if operation == "collect" else (quarantine, original)
                with ExitStack() as stack:
                    if obj["kind"] == "stage" and source.exists():
                        stack.enter_context(_lease(source))
                    if source.exists():
                        if not _remaining(source, obj, [], temporary_root=True):
                            _fail("foreign_state", "Retention source changed.")
                        if os.path.lexists(destination):
                            _fail("foreign_state", "Retention destination is occupied.")
                        if operation == "collect":
                            durable_mkdir(destination.parent)
                        _rename_object(source, destination, obj)
                    elif not _remaining(destination, obj, [], temporary_root=True):
                        _fail("foreign_state", "Neither exact retention before nor after state exists.")
                    _restore_root_mode(destination, obj)
            _checkpoint(checkpoint, "retention:{}:effect".format(index), tx)
            with repository.atomic():
                existing = repository.get("retention-object", obj["quarantine_id"])
                quarantined_at = existing["data"]["quarantined_at"] if existing else utc_now()
                state = {"collect": "quarantined", "restore": "restored", "purge": "purged"}[operation]
                _put(repository, "retention-object", obj["quarantine_id"], {"object": obj, "state": state,
                     "quarantined_at": quarantined_at, "updated_at": utc_now(), "transaction_id": tx["transaction_id"]})
                step["state"] = "completed"
                _save(manager, tx)
            _checkpoint(checkpoint, "retention:{}:completed".format(index), tx)
        for step in tx["objects"]:
            obj = step["object"]
            destination = manager.store_root / obj["name"] if operation == "restore" else Path(obj["quarantine_path"])
            if ((operation == "purge" and os.path.lexists(destination))
                    or (operation != "purge" and not _remaining(destination, obj, []))):
                _fail("foreign_state", "Final retention postcondition changed; no success receipt is published.")
            protected, _, _, _ = _references(manager)
            if operation != "restore" and obj["snapshot_tree_sha256"] in protected:
                _fail("referenced", "Retention acquired references before final publication.")
        return _complete(manager, tx)
    except Exception as exc:
        try:
            old = repository.get("retention-transaction", tx["transaction_id"])
        except Exception as journal_error:
            raise LifecycleError("retention_journal_failed", "Retention failed and its durable completion state cannot be read; inspect before retrying or compensating.",
                                 details={"transaction_id": tx["transaction_id"], "primary": str(exc)[:1000], "journal": str(journal_error)[:1000]}) from exc
        if old and old["data"]["state"] == "completed":
            raise LifecycleError("retention_committed_response_failed", "Retention committed; inspect its receipt.",
                                 details={"transaction_id": tx["transaction_id"]}) from exc
        tx["state"] = "recovery_needed"
        tx["receipt_id"] = None
        tx.pop("completion_event_sequence", None)
        tx["errors"].append({"code": getattr(exc, "code", "io_error"), "message": str(exc)[:1000]})
        try:
            _save(manager, tx)
        except Exception as journal_error:
            raise LifecycleError("retention_journal_failed", "Retention failed and failure recording also failed.",
                                 details={"transaction_id": tx["transaction_id"], "primary": str(exc)[:1000], "journal": str(journal_error)[:1000]}) from exc
        raise LifecycleError("retention_incomplete", "Retention is incomplete; inspect and explicitly resume.",
                             details={"transaction_id": tx["transaction_id"], "primary": getattr(exc, "code", "io_error")}) from exc


def _complete(manager, tx):
    receipt = {"schema_version": _PREFIX + "receipt/v1", "receipt_id": uuid.uuid4().hex,
               "transaction_id": tx["transaction_id"], "plan_id": tx["plan"]["plan_id"],
               "operation": tx["plan"]["operation"], "status": "completed", "completed_at": utc_now(),
               "permanently_deleted": tx["plan"]["operation"] == "purge",
               "objects": tx["plan"]["objects"], "expires": tx["plan"]["expires"]}
    with manager.repository.atomic():
        manager.repository.put("retention-receipt", receipt["receipt_id"], receipt)
        event = manager.repository.append_event("retention:" + tx["transaction_id"], "retention_completed",
                                               {"receipt_id": receipt["receipt_id"], "operation": receipt["operation"]}, actor=tx["actor"], tool="retention")
        tx["state"], tx["receipt_id"] = "completed", receipt["receipt_id"]
        tx["completion_event_sequence"] = event["sequence"]
        _save(manager, tx)
    return receipt


def _completed_receipt(manager, tx):
    plan = tx["plan"]
    _safe_store(manager)
    _completion_evidence(manager, plan["operation"], {"plan_id": plan["plan_id"], "transaction_id": tx["transaction_id"],
                                                   "receipt_id": tx["receipt_id"], "completion_event_sequence": tx.get("completion_event_sequence")})
    record = manager.repository.get("retention-receipt", tx["receipt_id"])
    expected = {"schema_version": _PREFIX + "receipt/v1", "receipt_id": tx["receipt_id"],
                "transaction_id": tx["transaction_id"], "plan_id": plan["plan_id"],
                "operation": plan["operation"], "status": "completed", "objects": plan["objects"],
                "expires": plan["expires"], "permanently_deleted": plan["operation"] == "purge"}
    if not record or any(record["data"].get(key) != value for key, value in expected.items()):
        _fail("evidence_invalid", "Completed retention receipt does not bind this transaction.")
    for obj in plan["objects"]:
        latest = manager.repository.get("retention-object", obj["quarantine_id"])
        state = {"collect": "quarantined", "restore": "restored", "purge": "purged"}[plan["operation"]]
        if not latest or latest["data"].get("state") != state or latest["data"].get("transaction_id") != tx["transaction_id"]:
            _fail("stale_transaction", "Later retention work superseded this receipt.")
        destination = manager.store_root / obj["name"] if plan["operation"] == "restore" else Path(obj["quarantine_path"])
        try:
            matches = not os.path.lexists(destination) if plan["operation"] == "purge" else _remaining(destination, obj, [])
        except OSError as exc:
            raise LifecycleError("retention_evidence_invalid", "Cannot inspect completed retention postconditions.") from exc
        if not matches:
            _fail("foreign_state", "Completed retention payload no longer matches its receipt.")
    if plan["operation"] == "policy" and _policy(manager) != plan["policy_after"]:
        _fail("stale_transaction", "A later retention policy superseded this receipt.")
    if plan["operation"] == "expire":
        _, _, records, _ = _references(manager)
        if any(reference["kind"] + ":" + reference["id"] not in records["payload-retention"] for reference in plan["expires"]):
            _fail("evidence_invalid", "Completed expiration is missing its retained tombstone.")
    return record["data"]


def _compensate(manager, tx, checkpoint):
    """Reverse only an incomplete collect; never erase the original failure."""
    tx["recovery_mode"] = "compensate"
    try:
        _save(manager, tx)
        for index in reversed(range(len(tx["objects"]))):
            step = tx["objects"][index]
            obj = step["object"]
            original, quarantine = manager.store_root / obj["name"], Path(obj["quarantine_path"])
            if os.path.lexists(original):
                if os.path.lexists(quarantine) or not _remaining(original, obj, [], temporary_root=step["state"] in {"intent", "compensating"}):
                    _fail("foreign_state", "Compensation refuses a foreign restore destination.")
            else:
                if not _remaining(quarantine, obj, [], temporary_root=True):
                    _fail("foreign_state", "Compensation cannot identify the owned quarantined object.")
                step["state"] = "compensating"
                _save(manager, tx)
                _checkpoint(checkpoint, "retention-compensate:{}:intent".format(index), tx)
                with ExitStack() as stack:
                    if obj["kind"] == "stage":
                        stack.enter_context(_lease(quarantine))
                    if os.path.lexists(original) or not _remaining(quarantine, obj, [], temporary_root=True):
                        _fail("foreign_state", "Compensation state changed before restoring.")
                    _rename_object(quarantine, original, obj)
                _checkpoint(checkpoint, "retention-compensate:{}:effect".format(index), tx)
            _restore_root_mode(original, obj)
            if not _remaining(original, obj, []):
                _fail("foreign_state", "Compensation restored object changed.")
            with manager.repository.atomic():
                old = manager.repository.get("retention-object", obj["quarantine_id"])
                _put(manager.repository, "retention-object", obj["quarantine_id"], {
                    "object": obj, "state": "restored", "quarantined_at": old["data"]["quarantined_at"] if old else None,
                    "updated_at": utc_now(), "transaction_id": tx["transaction_id"]})
                step["state"] = "compensated"
                _save(manager, tx)
        _compensated_postconditions(manager, tx)
        with manager.repository.atomic():
            tx["state"] = "compensated"
            _save(manager, tx)
            manager.repository.append_event("retention:" + tx["transaction_id"], "retention_compensated",
                                            {"plan_id": tx["plan"]["plan_id"]}, actor=tx["actor"], tool="retention")
        return tx
    except Exception as exc:
        tx["state"] = "recovery_needed"
        tx["errors"].append({"code": getattr(exc, "code", "io_error"), "message": str(exc)[:1000]})
        try:
            _save(manager, tx)
        except Exception as journal_error:
            raise LifecycleError("retention_journal_failed", "Compensation failed and failure recording failed.",
                                 details={"transaction_id": tx["transaction_id"], "primary": str(exc)[:1000], "journal": str(journal_error)[:1000]}) from exc
        raise LifecycleError("retention_compensation_failed", "Collection compensation needs further explicit recovery.",
                             details={"transaction_id": tx["transaction_id"], "primary": getattr(exc, "code", "io_error")}) from exc


def _compensated_postconditions(manager, tx):
    for obj in tx["plan"]["objects"]:
        if os.path.lexists(obj["quarantine_path"]) or not _remaining(manager.store_root / obj["name"], obj, []):
            _fail("foreign_state", "Compensated collection no longer has every exact original object.")


def apply_retention(manager, plan, *, approve_plan_id, permanent_delete=False, transaction_id=None,
                    actor="local-operator", checkpoint=None):
    """Execute only an exact approved plan; permanent purge requires a second gate."""
    _validate_plan(manager, plan)
    if approve_plan_id != plan["plan_id"]:
        _fail("approval_required", "Explicit approval must identify this exact retention plan.")
    if type(permanent_delete) is not bool or (plan["operation"] == "purge" and not permanent_delete):
        _fail("permanent_delete_required", "Permanent purge requires permanent_delete=True.")
    if not isinstance(actor, str) or not actor.strip() or len(actor) > 200:
        _fail("invalid_plan", "Actor must be a bounded local attribution label.")
    transaction_id = transaction_id or uuid.uuid4().hex
    if not _ids([transaction_id]):
        _fail("invalid_plan", "A transaction ID is required.")
    with _locks(manager):
        previous = manager.repository.get("retention-transaction", transaction_id)
        if previous:
            tx = previous["data"]
            _validate_transaction(manager, tx, transaction_id)
            if tx["plan"] != plan:
                _fail("transaction_conflict", "Transaction ID already belongs to another retention plan.")
            if tx["state"] == "completed":
                return _completed_receipt(manager, tx)
            raise LifecycleError("retention_recovery_required", "Resume the existing transaction explicitly.",
                                 details={"transaction_id": transaction_id})
        _incomplete(manager.repository)
        try:
            rebuilt = _build(manager, plan["operation"], plan["options"], plan["created_at"], now=_clock())
        except (LifecycleError, OSError, ValueError) as exc:
            raise LifecycleError("retention_stale_plan", "Retention references or objects changed; generate a new plan.") from exc
        if rebuilt != plan:
            _fail("stale_plan", "Retention plan changed; review current references and object identities.")
        tx = {"transaction_id": transaction_id, "plan": copy.deepcopy(plan), "actor": actor,
              "approved_plan_id": approve_plan_id,
              "state": "prepared", "receipt_id": None, "errors": [],
              "objects": [{"object": copy.deepcopy(obj), "state": "pending", "deleted": [], "pending_delete": None} for obj in plan["objects"]]}
        try:
            _save(manager, tx)
        except Exception as exc:
            raise LifecycleError("retention_journal_failed", "Retention intent could not be confirmed; no filesystem effect was started.",
                                 details={"transaction_id": transaction_id, "error": str(exc)[:1000]}) from exc
        return _execute(manager, tx, checkpoint)


def recover_retention(manager, transaction_id, *, mode="inspect", approve_plan_id=None,
                      permanent_delete=False, checkpoint=None):
    """Resume exact remaining work; purge cannot be compensated or auto-approved."""
    record = manager.repository.get("retention-transaction", transaction_id)
    if not record:
        _fail("unknown_transaction", "Retention transaction does not exist.")
    tx = record["data"]
    if mode == "inspect":
        return tx
    if mode not in {"resume", "compensate"}:
        _fail("invalid_transition", "Recovery supports inspect/resume, or compensation of incomplete collection.")
    _validate_transaction(manager, tx, transaction_id)
    if approve_plan_id != tx["plan"]["plan_id"]:
        _fail("approval_required", "Recovery requires explicit approval of the recorded plan.")
    if mode == "compensate" and (tx["plan"]["operation"] != "collect" or tx["state"] == "completed"):
        _fail("invalid_transition", "Only an incomplete collect may compensate; purge is irreversible.")
    if tx["plan"]["operation"] == "purge" and permanent_delete is not True:
        _fail("permanent_delete_required", "Permanent purge recovery requires permanent_delete=True.")
    with _locks(manager):
        tx = manager.repository.get("retention-transaction", transaction_id)["data"]
        _validate_transaction(manager, tx, transaction_id)
        if tx["state"] == "compensated":
            if mode == "compensate":
                _safe_store(manager)
                _compensated_postconditions(manager, tx)
                return tx
            _fail("invalid_transition", "Compensated collection cannot resume; review a new plan.")
        if tx["state"] == "completed":
            return _completed_receipt(manager, tx)
        _safe_store(manager)
        _references(manager)
        if mode == "compensate":
            return _compensate(manager, tx, checkpoint)
        if tx.get("recovery_mode") == "compensate":
            _fail("invalid_transition", "Continue the recorded compensation; do not switch to forward apply.")
        if tx["plan"]["operation"] in {"policy", "expire"}:
            rebuilt = _build(manager, tx["plan"]["operation"], tx["plan"]["options"], tx["plan"]["created_at"], now=_clock())
            if rebuilt != tx["plan"]:
                _fail("stale_plan", "Evidence or policy changed before metadata recovery.")
        return _execute(manager, tx, checkpoint)
