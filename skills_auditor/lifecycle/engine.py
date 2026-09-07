"""Explicitly approved, write-ahead managed installation transactions.

Filesystem effects and SQLite commits are not a distributed transaction. Each
effect therefore has durable before/after evidence and requires explicit
recovery after interruption. Advisory locks coordinate participating writers;
they do not prevent arbitrary external filesystem changes.
"""

import copy
import os
import re
import stat
import uuid
import unicodedata
from contextlib import ExitStack, contextmanager
from datetime import datetime
from pathlib import Path

from .common import LifecycleError, canonical_entry, digest, paths_overlap as _overlap, utc_now
from .locking import locked_paths
from .repository import Repository
from .snapshots import inspect_source, materialize, verify_snapshot


OPERATIONS = frozenset({
    "install", "update", "edit", "move", "rename", "disable", "enable",
    "archive", "uninstall", "renew", "revoke", "rollback", "migrate", "install-retained",
})
_CREATING = frozenset({"install", "migrate", "install-retained"})
_NEW_VERSION = frozenset({"install", "update", "edit", "migrate"})
_GRANTING = frozenset({"install", "update", "edit", "migrate", "enable", "renew", "rollback", "move", "rename", "install-retained"})
_SCHEMA = "skills-auditor-lifecycle-"


def _identifier(value, label="identifier"):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}", value):
        raise LifecycleError("invalid_plan", "Invalid {}.".format(label))
    return value


def _timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})", value):
        raise ValueError("timezone-qualified timestamp required")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("timestamp timezone required")


def _entry(path):
    """Observe a leaf without following it; I/O failures are never absence."""
    try:
        info = Path(path).lstat()
    except FileNotFoundError:
        return {"kind": "missing"}
    except OSError as error:
        raise LifecycleError("entry_unreadable", "Cannot inspect target: {}".format(path), details={"error": str(error)}) from error
    if stat.S_ISLNK(info.st_mode):
        try:
            link = os.readlink(path)
        except OSError as error:
            raise LifecycleError("entry_unreadable", "Cannot read target link.", details={"error": str(error)}) from error
        return {"kind": "symlink", "link": link,
                "identity": [info.st_dev, info.st_ino, info.st_ctime_ns]}
    return {"kind": "foreign", "mode": stat.S_IFMT(info.st_mode),
            "identity": [info.st_dev, info.st_ino, info.st_ctime_ns]}


def _matches(actual, expected):
    return all(actual.get(key) == value for key, value in expected.items())


def _parent_identity(path):
    info = Path(path).parent.stat()
    return [info.st_dev, info.st_ino]


def _entry_key(path):
    return (*_parent_identity(path), unicodedata.normalize("NFD", Path(path).name).casefold())


def _checkpoint(callback, name, transaction):
    if callback is not None:
        callback(name, copy.deepcopy(transaction))


def _error(error):
    result = {"code": getattr(error, "code", "io_error"), "message": str(error)[:1000]}
    details = getattr(error, "details", None)
    allowed = {"expected", "actual", "path", "stage", "published", "kind", "link", "error",
               "source_tree_sha256", "snapshot_tree_sha256", "normalization"}

    def bounded(value, depth=0):
        if isinstance(value, str):
            return value[:1000]
        if value is None or type(value) in {bool, int}:
            return value
        if isinstance(value, dict) and depth < 3:
            return {key: bounded(item, depth + 1) for key, item in value.items() if key in allowed}
        return None

    if isinstance(details, dict):
        result["details"] = bounded(details)
    return result


class Manager:
    """One project's managed state; target locks additionally span projects."""

    def __init__(self, project_root, *, create=True):
        self.project_root = Path(project_root).resolve()
        self.state_root = self.project_root / ".skills-auditor-local" / "lifecycle"
        self.store_root = self.state_root / "store" / "sha256"
        self._state_safety()
        self.repository = Repository(self.state_root, create=create)
        self._state_identity = [self.state_root.stat().st_dev, self.state_root.stat().st_ino]

    def _state_safety(self):
        current = self.project_root
        for part in self.store_root.relative_to(self.project_root).parts:
            current = current / part
            if current.is_symlink():
                raise LifecycleError("unsafe_state_path", "Managed state and store ancestors must not be symlinks.")
        if hasattr(self, "_state_identity"):
            info = self.state_root.stat()
            if [info.st_dev, info.st_ino] != self._state_identity:
                raise LifecycleError("unsafe_state_path", "Managed state directory was replaced.")

    def _get(self, kind, identifier):
        _identifier(identifier)
        record = self.repository.get(kind, identifier)
        if record is None:
            raise LifecycleError("record_missing", "{} {} does not exist.".format(kind, identifier))
        return record

    def get_installation(self, installation_id):
        return self._get("installation", installation_id)["data"]

    def list_installations(self):
        return [record["data"] for record in self.repository.list("installation")]

    def get_skill(self, skill_id):
        return self._get("skill", skill_id)["data"]

    def inspect_transaction(self, transaction_id):
        return self._get("transaction", transaction_id)["data"]

    def _target(self, path, source=None, *, require_parent=True):
        target = Path(canonical_entry(path))
        if _overlap(target, self.state_root) or (source is not None and _overlap(target, source)):
            raise LifecycleError("unsafe_overlap", "Source, target and managed state must be disjoint.")
        if require_parent and not target.parent.is_dir():
            raise LifecycleError("target_parent_missing", "Target parent must already exist.")
        return target

    def _version(self, version_id):
        return self._get("version", version_id)["data"]

    def plan(self, operation, *, source=None, target=None, installation_id=None,
             skill_id=None, name=None, version_id=None, legacy_receipt=None):
        arguments = {"source": source, "target": target, "installation_id": installation_id,
                     "skill_id": skill_id, "name": name, "version_id": version_id, "legacy_receipt": legacy_receipt}
        if isinstance(operation, str) and operation in OPERATIONS - _CREATING - {"revoke"}:
            with self._inspection_context(installation_id) as (_, lock_error):
                return self._plan_locked(operation, _lock_error=lock_error, **arguments)
        return self._build_plan(operation, **arguments)

    def _plan_locked(self, operation, *, _lock_error=None, **arguments):
        """Plan under the caller's coordinator/current-target locks."""
        observation = None
        if operation not in _CREATING | {"revoke"}:
            installation_id = arguments["installation_id"]
            record = self._get("installation", installation_id)
            if record["data"]["state"] == "active":
                observation = {"version_id": record["data"]["version_id"], "target_path": record["data"]["target"]}
                self._verify_locked(installation_id, observation=observation, lock_error=_lock_error)
        return self._build_plan(operation, _observation=observation, **arguments)

    def _build_plan(self, operation, *, source=None, target=None, installation_id=None,
                    skill_id=None, name=None, version_id=None, legacy_receipt=None, _observation=None):
        if not isinstance(operation, str) or operation not in OPERATIONS:
            raise LifecycleError("invalid_operation", "Unknown managed operation.")
        if legacy_receipt is not None and operation != "migrate":
            raise LifecycleError("invalid_plan", "Only migration accepts a legacy receipt.")
        if name is not None and (not isinstance(name, str) or not name.strip() or len(name) > 200):
            raise LifecycleError("invalid_plan", "Name must be a nonempty bounded string.")
        new = operation in _CREATING
        if new:
            if installation_id is not None:
                raise LifecycleError("invalid_transition", "New installation must receive a new identity.")
            if target is None or (operation != "install-retained" and source is None):
                raise LifecycleError("invalid_plan", "Install requires a target and a source or explicitly retained version.")
            if operation == "install-retained":
                retained = self._version(version_id)
                if skill_id is not None and skill_id != retained["skill_id"]:
                    raise LifecycleError("invalid_plan", "Retained version belongs to another Skill.")
                skill_id = retained["skill_id"]
            before, expected_revision = None, 0
            installation_id = uuid.uuid4().hex
            if skill_id is not None:
                skill = self.get_skill(skill_id)
            else:
                skill_id = uuid.uuid4().hex
                skill = {"skill_id": skill_id, "name": name or Path(source).name, "created_at": utc_now()}
        else:
            record = self._get("installation", installation_id)
            before, expected_revision = record["data"], record["revision"]
            if skill_id is not None and skill_id != before["skill_id"]:
                raise LifecycleError("invalid_plan", "Installation Skill identity cannot be replaced.")
            skill_id = before["skill_id"]
            skill = self.get_skill(skill_id)
            if before["state"] == "uninstalled":
                raise LifecycleError("invalid_transition", "Uninstalled identity is historical; create a new installation.")
            if operation in {"update", "edit", "move", "disable", "renew", "rollback"} and before["state"] != "active":
                raise LifecycleError("invalid_transition", "Operation requires an active installation.")
            if operation == "enable" and before["state"] not in {"disabled", "archived"}:
                raise LifecycleError("invalid_transition", "Enable requires a disabled or archived installation.")
            if operation == "move" and target is None:
                raise LifecycleError("invalid_plan", "Move requires a destination target.")
            if operation != "move" and target is not None and str(canonical_entry(target)) != before["target"]:
                raise LifecycleError("invalid_plan", "Only move may change the installation target.")
            target = target or before["target"]
        if not isinstance(name or skill["name"], str) or not (name or skill["name"]).strip() or len(name or skill["name"]) > 200:
            raise LifecycleError("invalid_plan", "Name must be a nonempty bounded string.")

        source_path = None
        if operation in _NEW_VERSION:
            if source is None:
                raise LifecycleError("invalid_plan", "Operation requires a replacement candidate source.")
            source_path = Path(source).resolve()
            if _overlap(source_path, self.state_root):
                raise LifecycleError("unsafe_overlap", "Candidate source and managed state must be disjoint.")
            inspection = inspect_source(source_path)
            version_id = digest({"skill_id": skill_id, **inspection})
            version = {
                "version_id": version_id, "skill_id": skill_id,
                "parent_version_id": before["version_id"] if before else None,
                "source": str(source_path), "created_at": utc_now(),
                "snapshot": {**inspection, "path": str(self.store_root / inspection["snapshot_tree_sha256"] / "tree")},
                "provenance": {"operation": operation, "legacy_receipt_id": None},
            }
            existing_version = self.repository.get("version", version_id)
            if existing_version:
                version = existing_version["data"]
        else:
            if source is not None:
                raise LifecycleError("invalid_plan", "This operation does not accept a candidate source.")
            if operation in {"rollback", "install-retained"}:
                if version_id is None:
                    raise LifecycleError("invalid_plan", "Rollback requires an explicitly selected retained version.")
            elif version_id is not None and version_id != before["version_id"]:
                raise LifecycleError("invalid_plan", "Only rollback may select another retained version.")
            version = self._version(version_id or before["version_id"])
            if version["skill_id"] != skill_id:
                raise LifecycleError("invalid_plan", "Retained version belongs to another Skill.")
            if operation not in {"revoke", "disable", "archive", "uninstall"}:
                if _observation and version["version_id"] == _observation["version_id"]:
                    if _observation.get("snapshot_error"):
                        raise _observation["snapshot_error"]
                else:
                    verify_snapshot(version["snapshot"])

        target_path = Path(before["target"]) if operation == "revoke" else self._target(target, source_path)
        if before and source_path and _overlap(source_path, before["target"]):
            raise LifecycleError("unsafe_overlap", "Source overlaps the previous installation target.")
        def observed_target(path):
            if _observation and str(path) == _observation["target_path"]:
                if _observation.get("target_read_error"):
                    raise _observation["target_read_error"]
                return _observation["target"]
            return _entry(path)

        observation = {"kind": "missing"} if operation == "revoke" else observed_target(target_path)
        if new and operation != "migrate" and observation["kind"] != "missing":
            raise LifecycleError("target_conflict", "New target is occupied; no unmanaged entry will be overwritten.")
        if before:
            if operation == "move" and str(target_path) != before["target"]:
                if observation["kind"] != "missing":
                    raise LifecycleError("target_conflict", "Move destination is occupied.")
            elif operation != "revoke":
                self._owned_observation(before, observation)

        legacy = None
        if operation == "migrate":
            legacy = self._legacy(legacy_receipt, source_path, target_path)
            if existing_version is None:
                version["provenance"]["legacy_receipt_id"] = legacy["receipt_id"]

        after = copy.deepcopy(before) if before else {
            "schema_version": _SCHEMA + "installation/v1", "installation_id": installation_id,
            "skill_id": skill_id, "created_at": utc_now(), "authorization": {"state": "unknown", "grant_id": None},
        }
        after.update({"target": str(target_path), "version_id": version["version_id"],
                      "name": name or (before["name"] if before else skill["name"])})
        after["state"] = {"disable": "disabled", "archive": "archived", "uninstall": "uninstalled"}.get(operation, "active" if operation in _CREATING | {"enable"} else before["state"])
        desired = {"kind": "symlink", "link": version["snapshot"]["path"]} if after["state"] == "active" else {"kind": "missing"}
        if operation == "revoke":
            desired = {key: value for key, value in observation.items() if key != "identity"}
        steps = [] if operation == "revoke" else [{"path": str(target_path), "parent_identity": _parent_identity(target_path), "before": observation, "after": desired}]
        if operation == "move" and str(target_path) != before["target"]:
            old_target = self._target(before["target"])
            old_observation = observed_target(old_target)
            self._owned_observation(before, old_observation)
            steps.append({"path": str(old_target), "parent_identity": _parent_identity(old_target), "before": old_observation, "after": {"kind": "missing"}})
        plan = {
            "schema_version": _SCHEMA + "plan/v1", "operation": operation,
            "project_root": str(self.project_root), "created_at": utc_now(),
            "installation_id": installation_id, "skill": skill, "expected_revision": expected_revision,
            "before": before, "after": after, "version": version,
            "source": str(source_path) if source_path else None, "legacy_receipt": legacy,
            "steps": steps,
        }
        plan["plan_id"] = digest(plan)
        return plan

    def _legacy(self, receipt, source, target):
        from ..integration import IntegrationError, verify_receipt
        if not isinstance(receipt, dict):
            raise LifecycleError("legacy_receipt_required", "Migration requires a complete legacy receipt object.")
        try:
            result = verify_receipt(receipt)
        except (IntegrationError, OSError, ValueError) as error:
            raise LifecycleError("legacy_invalid", "Legacy receipt cannot be validated.", details=_error(error)) from error
        if result["status"] != "passed":
            raise LifecycleError("legacy_invalid", "Only a currently clean legacy receipt can migrate.")
        matching = [item for item in receipt["results"]
                    if item.get("expected_target") == str(source) and str(Path(item["root"]) / item["name"]) == str(target)]
        if not matching:
            raise LifecycleError("legacy_mismatch", "Legacy receipt does not bind this exact source and target.")
        return copy.deepcopy(receipt)

    def _owned_observation(self, installation, observation):
        if installation["state"] != "active":
            expected = {"kind": "missing"}
        else:
            version = self._version(installation["version_id"])
            expected = {"kind": "symlink", "link": version["snapshot"]["path"]}
        if not _matches(observation, expected):
            raise LifecycleError("target_conflict", "Managed target no longer matches its owned pointer; investigate before changing it.",
                                 details={"path": installation["target"], "expected": expected,
                                          "actual": {key: value for key, value in observation.items() if key != "identity"}})

    def _validate_plan(self, plan, *, physical_paths=True, metadata_only=False, resolve_source=True):
        """Validate exact structure; historical readers never resolve live aliases."""
        required = {"schema_version", "operation", "project_root", "created_at", "installation_id", "skill",
                    "expected_revision", "before", "after", "version", "source", "legacy_receipt", "steps", "plan_id"}
        if not isinstance(plan, dict) or set(plan) != required:
            raise LifecycleError("invalid_plan", "Plan has missing or unexpected fields.")
        body = {key: value for key, value in plan.items() if key != "plan_id"}
        try:
            pending = [(body, 0)]
            while pending:
                value, depth = pending.pop()
                if depth > 100:
                    raise ValueError("plan nesting exceeds the supported bound")
                if type(value) is dict:
                    if any(type(key) is not str for key in value):
                        raise ValueError("JSON object keys must be strings")
                    pending.extend((item, depth + 1) for item in value.values())
                elif type(value) is list:
                    pending.extend((item, depth + 1) for item in value)
                elif value is not None and type(value) not in {str, int, float, bool}:
                    raise ValueError("non-JSON value")
            checksum = digest(body)
        except (TypeError, ValueError, RecursionError) as error:
            raise LifecycleError("invalid_plan", "Plan must contain only finite JSON values.") from error
        if plan["schema_version"] != _SCHEMA + "plan/v1" or plan["plan_id"] != checksum:
            raise LifecycleError("invalid_plan", "Plan schema or checksum is invalid.")
        if plan["project_root"] != str(self.project_root) or not isinstance(plan["operation"], str) or plan["operation"] not in OPERATIONS:
            raise LifecycleError("invalid_plan", "Plan belongs to another project or operation.")
        try:
            if any(type(plan[field]) is not dict for field in ("skill", "version", "after")) or type(plan["steps"]) is not list:
                raise ValueError("record and step container types")
            if plan["before"] is not None and type(plan["before"]) is not dict:
                raise ValueError("prior installation type")
            _identifier(plan["installation_id"])
            _identifier(plan["skill"]["skill_id"])
            _identifier(plan["version"]["version_id"])
            if set(plan["skill"]) != {"skill_id", "name", "created_at"} or set(plan["version"]) != {"version_id", "skill_id", "parent_version_id", "source", "created_at", "snapshot", "provenance"}:
                raise ValueError("identity fields")
            origin = plan["version"]
            if not isinstance(origin["source"], str) or not Path(origin["source"]).is_absolute():
                raise ValueError("version origin source")
            if origin["parent_version_id"] is not None and (not isinstance(origin["parent_version_id"], str) or not re.fullmatch(r"[a-f0-9]{64}", origin["parent_version_id"])):
                raise ValueError("version origin parent")
            provenance = origin["provenance"]
            if (type(provenance) is not dict or set(provenance) != {"operation", "legacy_receipt_id"}
                    or not isinstance(provenance["operation"], str) or provenance["operation"] not in _NEW_VERSION):
                raise ValueError("version origin provenance")
            if provenance["operation"] == "migrate":
                if not isinstance(provenance["legacy_receipt_id"], str) or not provenance["legacy_receipt_id"]:
                    raise ValueError("migration origin receipt")
            elif provenance["legacy_receipt_id"] is not None:
                raise ValueError("non-migration origin receipt")
            if not isinstance(plan["skill"]["name"], str) or not plan["skill"]["name"].strip() or len(plan["skill"]["name"]) > 200:
                raise ValueError("Skill name")
            for timestamp in (plan["created_at"], plan["skill"]["created_at"], plan["version"]["created_at"], plan["after"]["created_at"]):
                _timestamp(timestamp)
            existing_skill = self.repository.get("skill", plan["skill"]["skill_id"])
            if existing_skill and existing_skill["data"] != plan["skill"]:
                raise ValueError("immutable Skill identity changed")
            if type(plan["expected_revision"]) is not int or plan["expected_revision"] < 0:
                raise ValueError("revision")
            if plan["after"]["installation_id"] != plan["installation_id"] or plan["after"]["skill_id"] != plan["skill"]["skill_id"]:
                raise ValueError("identity")
            if plan["version"]["skill_id"] != plan["skill"]["skill_id"] or plan["after"]["version_id"] != plan["version"]["version_id"]:
                raise ValueError("version identity")
            operation, before, after = plan["operation"], plan["before"], plan["after"]
            new = operation in _CREATING
            if new != (before is None) or (new and plan["expected_revision"] != 0) or (not new and plan["expected_revision"] == 0):
                raise ValueError("operation and prior state")
            if (operation in _NEW_VERSION and (not isinstance(plan["source"], str) or not Path(plan["source"]).is_absolute())) or (operation not in _NEW_VERSION and plan["source"] is not None):
                raise ValueError("operation and source")
            if (operation == "migrate" and type(plan["legacy_receipt"]) is not dict) or (operation != "migrate" and plan["legacy_receipt"] is not None):
                raise ValueError("migration receipt")
            if not new:
                if before["state"] == "uninstalled":
                    raise ValueError("terminal prior state")
                if operation in {"update", "edit", "move", "disable", "renew", "rollback"} and before["state"] != "active":
                    raise ValueError("active prior state required")
                if operation == "enable" and before["state"] not in {"disabled", "archived"}:
                    raise ValueError("disabled prior state required")
                if operation != "move" and after["target"] != before["target"]:
                    raise ValueError("target change outside move")
                if operation not in _NEW_VERSION | {"rollback"} and after["version_id"] != before["version_id"]:
                    raise ValueError("version change outside update")
                expected_after = copy.deepcopy(before)
            else:
                expected_after = {"schema_version": _SCHEMA + "installation/v1", "installation_id": plan["installation_id"],
                                  "skill_id": plan["skill"]["skill_id"], "created_at": after["created_at"],
                                  "authorization": {"state": "unknown", "grant_id": None}}
            expected_state = {"disable": "disabled", "archive": "archived", "uninstall": "uninstalled"}.get(operation, "active" if operation in _CREATING | {"enable"} else before["state"])
            expected_after.update(target=after["target"], version_id=after["version_id"], name=after["name"], state=expected_state)
            if after != expected_after or not isinstance(after["name"], str) or not after["name"].strip() or len(after["name"]) > 200:
                raise ValueError("impossible installation postcondition")
            snapshot = plan["version"]["snapshot"]
            if set(snapshot) != {"source_tree_sha256", "snapshot_tree_sha256", "normalization", "path"} or snapshot["normalization"] != "remove-write-bits/v1":
                raise ValueError("snapshot fields")
            if not re.fullmatch(r"[a-f0-9]{64}", snapshot["source_tree_sha256"]):
                raise ValueError("source hash")
            expected_path = str(self.store_root / snapshot["snapshot_tree_sha256"] / "tree")
            if not re.fullmatch(r"[a-f0-9]{64}", snapshot["snapshot_tree_sha256"]) or snapshot["path"] != expected_path:
                raise ValueError("snapshot path")
            if plan["source"]:
                lexical_source = Path(plan["source"])
                if (_overlap(lexical_source, self.state_root) or ".." in lexical_source.parts
                        or str(lexical_source) != plan["source"]
                        or (not metadata_only and resolve_source and str(lexical_source.resolve()) != plan["source"])):
                    raise ValueError("source path")
            if (operation == "revoke" and plan["steps"] != []) or (operation != "revoke" and not 1 <= len(plan["steps"]) <= 2):
                raise ValueError("steps")
            existing_version = self.repository.get("version", plan["version"]["version_id"])
            expected_version_id = digest({"skill_id": plan["skill"]["skill_id"], **{key: value for key, value in snapshot.items() if key != "path"}})
            if plan["version"]["version_id"] != expected_version_id:
                raise ValueError("version content identity")
            same_origin = bool(existing_version and existing_version["data"] == plan["version"])
            if existing_version:
                if any(existing_version["data"].get(key) != plan["version"][key] for key in ("version_id", "skill_id", "snapshot")):
                    raise ValueError("immutable version content changed")
                if operation not in _NEW_VERSION and not same_origin:
                    raise ValueError("immutable retained version origin changed")
            elif operation not in _NEW_VERSION:
                raise ValueError("retained version missing")
            if operation in _NEW_VERSION and not same_origin:
                # An independently saved adoption may race first registration.
                # Its exact origin must still describe this reviewed occurrence;
                # shared content never authorizes arbitrary metadata changes.
                if plan["version"]["source"] != plan["source"] or plan["version"]["parent_version_id"] != (before["version_id"] if before else None):
                    raise ValueError("new version identity or lineage")
                if plan["version"]["provenance"] != {"operation": operation, "legacy_receipt_id": plan["legacy_receipt"]["receipt_id"] if plan["legacy_receipt"] else None}:
                    raise ValueError("version provenance")
            paths = []
            for index, step in enumerate(plan["steps"]):
                if set(step) != {"path", "parent_identity", "before", "after"}:
                    raise ValueError("step path")
                if metadata_only:
                    historical_path = Path(step["path"])
                    if (not historical_path.is_absolute() or ".." in historical_path.parts or str(historical_path) != step["path"]
                            or _overlap(historical_path, self.state_root) or (plan["source"] and _overlap(historical_path, plan["source"]))):
                        raise ValueError("historical step path")
                elif str(self._target(step["path"], plan["source"], require_parent=physical_paths)) != step["path"]:
                    raise ValueError("step path")
                if len(step["parent_identity"]) != 2 or any(type(part) is not int for part in step["parent_identity"]):
                    raise ValueError("parent identity")
                if step["after"]["kind"] not in {"missing", "symlink"}:
                    raise ValueError("step state")
                if step["after"]["kind"] == "symlink" and step["after"].get("link") != expected_path:
                    raise ValueError("step target")
                expected_step_after = {"kind": "symlink", "link": expected_path} if index == 0 and expected_state == "active" else {"kind": "missing"}
                if step["after"] != expected_step_after:
                    raise ValueError("step disagrees with installation state")
                if new and operation != "migrate" and step["before"] != {"kind": "missing"}:
                    raise ValueError("unmanaged target overwrite")
                if step["before"]["kind"] not in {"missing", "symlink"}:
                    raise ValueError("foreign before state")
                expected_keys = {"kind"} if step["before"]["kind"] == "missing" else {"kind", "link", "identity"}
                if set(step["before"]) != expected_keys:
                    raise ValueError("before fields")
                if step["before"]["kind"] == "symlink" and (len(step["before"]["identity"]) != 3 or any(type(part) is not int for part in step["before"]["identity"])):
                    raise ValueError("before identity")
                if before and operation != "revoke":
                    if operation == "move" and index == 0 and after["target"] != before["target"]:
                        if step["before"] != {"kind": "missing"}:
                            raise ValueError("occupied move destination")
                    else:
                        self._owned_observation(before, step["before"])
                paths.append(step["path"])
            if len(set(paths)) != len(paths) or (paths and paths[0] != plan["after"]["target"]):
                raise ValueError("step identity")
            if len(paths) == 2 and (plan["operation"] != "move" or paths[1] != plan["before"]["target"]):
                raise ValueError("move steps")
            if operation == "move" and after["target"] != before["target"] and len(paths) != 2:
                raise ValueError("incomplete move")
            if len(paths) == 2 and _overlap(paths[0], paths[1]):
                raise ValueError("overlapping move targets")
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise LifecycleError("invalid_plan", "Malformed plan: {}.".format(error)) from error

    def _locks(self, plan):
        return self._operation_locks([plan])

    @contextmanager
    def _operation_locks(self, plans):
        """Coordinate structural admission before checking physical parents."""
        self._state_safety()
        with locked_paths([self.state_root / "registry"]):
            with ExitStack() as stack:
                try:
                    stack.enter_context(locked_paths([Path(step["path"]) for plan in plans for step in plan["steps"]]))
                except LifecycleError as error:
                    if error.code == "lock_parent_missing":
                        self._observe_missing_current_parents(plans)
                    raise
                yield

    def _observe_missing_current_parents(self, plans):
        """A missing current parent is evidence, unlike a missing destination."""
        for plan in plans:
            if plan["operation"] == "revoke":
                continue
            record = self.repository.get("installation", plan["installation_id"])
            if not record or record["data"]["state"] != "active":
                continue
            current = record["data"]
            exact_before = record["revision"] == plan["expected_revision"] and current == plan["before"]
            prior_tx = self.repository.get("transaction", current["last_transaction_id"])
            exact_completed = bool(prior_tx and prior_tx["data"]["state"] == "completed" and prior_tx["data"]["plan"] == plan)
            if not exact_before and not exact_completed:
                continue
            try:
                with locked_paths([Path(current["target"])]):
                    pass
            except LifecycleError as error:
                if error.code != "lock_parent_missing":
                    raise
                # Same shared run/check/deny path as ordinary verification;
                # there is no target lock to hold when its parent is absent.
                self._verify_locked(current["installation_id"])

    def _check_inflight(self, plan, *, batch_id=None, compensates_batch_id=None):
        """Reserve unfinished identities and targets before a new durable intent."""
        if plan["operation"] == "revoke":
            return
        targets = [step["path"] for step in plan["steps"]]
        def conflicts(other):
            return (other["installation_id"] == plan["installation_id"]
                    or any(_overlap(target, step["path"]) for target in targets for step in other["steps"]))
        # A parent WAL can reserve children before their core intents exist.
        # Only the internal batch coordinator may exempt its own approved parent
        # or the explicitly claimed original parent of an inverse plan. Report
        # that coordinator first even after a child's own WAL exists, so lost
        # response recovery does not hide the rest of the approved batch.
        for record in self.repository.list("batch"):
            pending = record["data"]
            if pending["state"] in {"completed", "compensated"}:
                continue
            if pending["batch_id"] in {batch_id, compensates_batch_id} or (batch_id and pending["compensation_batch_id"] == batch_id):
                continue
            if any(conflicts(child["plan"]) for child in pending["plan"]["children"]):
                raise LifecycleError("pending_batch", "An unfinished batch reserves this installation or target; explicitly inspect and recover its parent before starting another mutation.",
                                     details={"batch_id": pending["batch_id"]})
        for record in self.repository.list("transaction"):
            pending = record["data"]
            if pending["state"] not in {"completed", "compensated"} and conflicts(pending["plan"]):
                raise LifecycleError("pending_transaction", "An unfinished transaction reserves this installation or target; explicitly inspect and recover it before starting another mutation.",
                                     details={"transaction_id": pending["transaction_id"]})

    def _revocation_only_since(self, before, current):
        """Prove a completed denial-only lineage without reading filesystem data."""
        try:
            if not before or current["authorization"]["state"] != "revoked" or current["authorization"]["grant_id"] != before["authorization"]["grant_id"]:
                return False
            mutable = {"authorization", "generation", "receipt_id", "last_transaction_id", "updated_at"}
            if {key: value for key, value in current.items() if key not in mutable} != {key: value for key, value in before.items() if key not in mutable}:
                return False
            cursor = current
            for _ in range(50):
                if {key: value for key, value in cursor.items() if key != "authorization"} == {key: value for key, value in before.items() if key != "authorization"}:
                    return cursor["authorization"]["grant_id"] == before["authorization"]["grant_id"]
                if not all(check["valid"] for check in self._evidence_checks(cursor)):
                    return False
                tx = self._get("transaction", cursor["last_transaction_id"])["data"]
                plan = tx["plan"]
                if plan["operation"] != "revoke" or tx["grant_id"] is not None or tx["steps"] != [] or not plan["before"]:
                    return False
                self._validate_plan(plan)
                expected = {**plan["after"], "authorization": {**plan["after"]["authorization"], "state": "revoked", "reason_codes": ["explicit_revocation"]},
                            "generation": plan["before"]["generation"] + 1, "receipt_id": tx["receipt_id"], "last_transaction_id": tx["transaction_id"], "updated_at": cursor["updated_at"]}
                if cursor != expected:
                    return False
                cursor = plan["before"]
        except (LifecycleError, KeyError, TypeError, ValueError):
            return False
        return False

    def _preconditions(self, plan, *, targets=True, source=True, compensating=False, observation=None):
        self._state_safety()
        for step in plan["steps"]:
            if str(canonical_entry(step["path"])) != step["path"] or _parent_identity(step["path"]) != step["parent_identity"]:
                raise LifecycleError("stale_plan", "Target parent identity changed after planning.")
        current = self.repository.get("installation", plan["installation_id"])
        if (current["revision"] if current else 0) != plan["expected_revision"] or (current["data"] if current else None) != plan["before"]:
            authorization_only = (compensating and current and plan["before"]
                                  and current["data"]["authorization"]["state"] in {"invalidated", "revoked"}
                                  and {key: value for key, value in current["data"].items() if key != "authorization"}
                                  == {key: value for key, value in plan["before"].items() if key != "authorization"})
            revocation_only = compensating and current and self._revocation_only_since(plan["before"], current["data"])
            if not authorization_only and not revocation_only:
                raise LifecycleError("stale_plan", "Installation state changed after planning. Inspect and compensate owned effects before generating a fresh approval plan.")
        if source and plan["source"]:
            try:
                if str(Path(plan["source"]).resolve()) != plan["source"]:
                    raise ValueError("reviewed source path now resolves to another entry")
                actual = inspect_source(plan["source"])
            except (LifecycleError, OSError, ValueError) as error:
                raise LifecycleError("stale_plan", "Candidate source is unreadable or changed.", details=_error(error)) from error
            if any(actual.get(key) != plan["version"]["snapshot"].get(key) for key in actual):
                raise LifecycleError("stale_plan", "Candidate content changed after planning.")
        if targets and plan["legacy_receipt"]:
            self._legacy(plan["legacy_receipt"], Path(plan["source"]), Path(plan["after"]["target"]))
        if targets:
            for step in plan["steps"]:
                if observation is not None and step["path"] == plan["before"]["target"]:
                    if observation.get("target_read_error"):
                        raise observation["target_read_error"]
                    actual = observation["target"]
                else:
                    actual = _entry(step["path"])
                if not _matches(actual, step["before"]):
                    raise LifecycleError("stale_plan", "Target changed after planning.", details={"target": step["path"]})

    def _admission_preconditions(self, plan):
        """Observe only the reviewed current boundary, before any new WAL/effect."""
        # A different generation, candidate error or stale historical plan is
        # not evidence against a newer/current approved installation.
        self._preconditions(plan, targets=False)
        observation = None
        if plan["before"] and plan["before"]["state"] == "active" and plan["operation"] != "revoke":
            observation = {}
            self._verify_locked(plan["installation_id"], observation=observation)
        # An observed invalidation changes the revision, demanding a new plan.
        # Already-reviewed denial may still be repaired by its exact new grant.
        self._preconditions(plan, source=False, observation=observation)

    def _persist_tx(self, tx):
        existing = self.repository.get("transaction", tx["transaction_id"])
        self.repository.put("transaction", tx["transaction_id"], tx,
                            expected_revision=existing["revision"] if existing else 0)

    def apply(self, plan, *, approve_plan_id, actor="local-operator", transaction_id=None, checkpoint=None):
        self._validate_plan(plan, physical_paths=False, resolve_source=False)
        if approve_plan_id != plan["plan_id"]:
            raise LifecycleError("approval_required", "Explicit approval must name this exact saved plan ID.")
        transaction_id = _identifier(transaction_id or uuid.uuid4().hex, "transaction ID")
        self._actor(actor)
        with self._locks(plan):
            return self._apply_locked(plan, approve_plan_id=approve_plan_id, actor=actor, transaction_id=transaction_id, checkpoint=checkpoint)

    def _apply_locked(self, plan, *, approve_plan_id, actor, transaction_id, checkpoint=None, batch_id=None):
        """Internal batch seam; caller holds coordinator and all plan targets."""
        self._validate_plan(plan, physical_paths=False, resolve_source=False)
        if approve_plan_id != plan["plan_id"]:
            raise LifecycleError("approval_required", "Explicit approval must name this exact saved plan ID.")
        _identifier(transaction_id)
        self._actor(actor)
        existing = self.repository.get("transaction", transaction_id)
        if existing:
            tx = existing["data"]
            if tx["plan"]["plan_id"] != plan["plan_id"]:
                raise LifecycleError("transaction_conflict", "Transaction ID is already bound to a different plan.")
            if tx["state"] == "completed":
                self._retry_health(tx)
                return self._get("receipt", tx["receipt_id"])["data"]
            raise LifecycleError("recovery_required", "Existing transaction needs explicit recovery, not an apply retry.", details={"transaction_id": transaction_id})
        self._check_inflight(plan, batch_id=batch_id)
        target_paths = {_entry_key(Path(step["path"])) for step in plan["steps"]}
        for index, step in enumerate(plan["steps"]):
            stage = Path(step["path"]).parent / (".skills-auditor-tx-{}-{}".format(transaction_id, index))
            if _entry_key(stage) in target_paths:
                raise LifecycleError("staging_conflict", "Transaction staging entry conflicts with a reviewed target; choose a different transaction ID.")
        self._admission_preconditions(plan)
        tx = {"schema_version": _SCHEMA + "transaction/v1", "transaction_id": transaction_id,
              "plan": copy.deepcopy(plan), "actor": actor, "approved_plan_id": approve_plan_id,
              "created_at": utc_now(), "state": "prepared", "grant_id": uuid.uuid4().hex if plan["operation"] in _GRANTING else None,
              "steps": [{**copy.deepcopy(step), "state": "pending"} for step in plan["steps"]],
              "error": None, "recovery_errors": [], "receipt_id": None, "snapshot_ready": False}
        try:
            self._persist_tx(tx)
        except Exception as error:
            raise LifecycleError("journal_write_failed", "Durable intent could not be confirmed; no filesystem effect was started. Inspect this transaction ID before retrying.", details={"transaction_id": transaction_id, "error": _error(error)}) from error
        return self._execute(tx, checkpoint)

    @staticmethod
    def _actor(actor):
        if not isinstance(actor, str) or not actor.strip() or len(actor) > 200:
            raise LifecycleError("invalid_actor", "Actor must be a nonempty local attribution label, at most 200 characters.")

    def _retry_health(self, tx):
        installation = self.get_installation(tx["plan"]["installation_id"])
        if installation.get("last_transaction_id") != tx["transaction_id"]:
            raise LifecycleError("stale_transaction", "A later transaction superseded this historical receipt.")
        if installation["state"] == "active" and tx["plan"]["operation"] != "revoke":
            verification = self._verify_locked(installation["installation_id"])
            if not verification["valid"]:
                raise LifecycleError("approval_invalidated", "Completed retry observed an invalid boundary or existing denial; inspect verification and explicitly re-plan before approval.",
                                     details={"transaction_id": tx["transaction_id"], "verification_id": verification["verification_id"], "reason_codes": verification["approval"]["reason_codes"]})
            return
        if not all(check["valid"] for check in self._evidence_checks(installation)):
            raise LifecycleError("historical_evidence_invalid", "Completed transaction evidence no longer matches this installation.")
        authorization = installation["authorization"]
        projection = self.repository.get("authorization", authorization["grant_id"])
        grant_record = self.repository.get("grant", authorization["grant_id"])
        binding = {"installation_id": installation["installation_id"], "skill_id": installation["skill_id"], "version_id": installation["version_id"]}
        if installation["state"] == "active" and authorization["state"] == "valid":
            binding.update(target=installation["target"], installation_generation=installation["generation"])
        if not projection or projection["data"] != authorization or not grant_record or not _matches(grant_record["data"], binding):
            raise LifecycleError("approval_invalidated", "Durable grant and authorization evidence is missing or inconsistent.")
        if tx["plan"]["operation"] == "revoke":
            if installation["authorization"]["state"] != "revoked":
                raise LifecycleError("stale_transaction", "Recorded revocation no longer matches current denial state.")
            return

    def _execute(self, tx, checkpoint, *, recovering=False):
        plan = tx["plan"]
        check_source = not recovering or not tx["snapshot_ready"]
        try:
            _checkpoint(checkpoint, "transaction:prepared", tx)
            self._preconditions(plan, targets=False, source=check_source)
            if plan["source"] and not tx["snapshot_ready"]:
                materialize(plan["source"], self.store_root,
                            expected_source_hash=plan["version"]["snapshot"]["source_tree_sha256"],
                            expected_snapshot_hash=plan["version"]["snapshot"]["snapshot_tree_sha256"])
            if plan["after"]["state"] == "active" and plan["operation"] != "revoke":
                verify_snapshot(plan["version"]["snapshot"])
            tx["snapshot_ready"] = True
            tx["state"] = "applying"
            self._persist_tx(tx)
            _checkpoint(checkpoint, "transaction:staged", tx)
            for index, step in enumerate(tx["steps"]):
                observed = _entry(step["path"])
                if step["state"] == "completed":
                    if not _matches(observed, step["after"]):
                        raise LifecycleError("recovery_conflict", "A completed step no longer has its expected target.")
                    continue
                if step["state"] == "intent" and _matches(observed, step["after"]):
                    step["state"] = "completed"
                    self._persist_tx(tx)
                    continue
                if not _matches(observed, step["before"]):
                    raise LifecycleError("recovery_conflict", "Target is neither the reviewed before-state nor a recorded after-state.")
                step["state"] = "intent"
                self._persist_tx(tx)
                _checkpoint(checkpoint, "step:{}:intent".format(index), tx)
                self._preconditions(plan, targets=False, source=check_source)
                self._effect(step["path"], step["before"], step["after"], tx, index, checkpoint)
                _checkpoint(checkpoint, "step:{}:effect".format(index), tx)
                if not _matches(_entry(step["path"]), step["after"]):
                    raise LifecycleError("effect_verification_failed", "Target changed during the filesystem operation.")
                step["state"] = "completed"
                self._persist_tx(tx)
                _checkpoint(checkpoint, "step:{}:completed".format(index), tx)
            self._preconditions(plan, targets=False, source=check_source)
            for step in tx["steps"]:
                if not _matches(_entry(step["path"]), step["after"]):
                    raise LifecycleError("effect_verification_failed", "Target changed before final publication.")
            if plan["after"]["state"] == "active" and plan["operation"] != "revoke":
                verify_snapshot(plan["version"]["snapshot"])
            _checkpoint(checkpoint, "transaction:before_commit", tx)
            receipt = self._commit(tx, check_source=check_source)
            _checkpoint(checkpoint, "transaction:committed", tx)
            return receipt
        except Exception as error:
            # A post-commit notification failure must not overwrite durable success.
            try:
                persisted = self.repository.get("transaction", tx["transaction_id"])
            except Exception as journal_error:
                raise LifecycleError("journal_write_failed", "Execution failed and durable completion state cannot be read; inspect this transaction before retrying or compensating.", details={"transaction_id": tx["transaction_id"], "primary": _error(error), "journal": _error(journal_error)}) from error
            if persisted and persisted["data"]["state"] == "completed":
                raise LifecycleError("committed_response_failed", "Transaction committed; inspect its recorded receipt before retrying.", details={"transaction_id": tx["transaction_id"], "error": _error(error)}) from error
            tx["state"] = "recovery_needed"
            tx["receipt_id"] = None
            if tx["error"] is None:
                tx["error"] = _error(error)
            else:
                tx["recovery_errors"].append(_error(error))
            try:
                self._persist_tx(tx)
            except Exception as journal_error:
                raise LifecycleError("journal_write_failed", "Execution failed and failure recording also failed; inspect durable intent before recovery.", details={"transaction_id": tx["transaction_id"], "primary": _error(error), "journal": _error(journal_error)}) from error
            raise LifecycleError("transaction_failed", "Transaction is incomplete and requires explicit recovery.", details={"transaction_id": tx["transaction_id"], "primary": _error(error)}) from error

    def _effect(self, path, before, after, tx, index, checkpoint):
        target = Path(path)
        if _parent_identity(target) != tx["steps"][index]["parent_identity"] or str(canonical_entry(target)) != str(target):
            raise LifecycleError("recovery_conflict", "Target parent changed immediately before its filesystem operation.")
        if not _matches(_entry(target), before):
            raise LifecycleError("recovery_conflict", "Target changed immediately before its filesystem operation.")
        if _matches(before, after):
            return
        if after["kind"] == "missing":
            target.unlink()
        else:
            stage = target.parent / (".skills-auditor-tx-{}-{}".format(tx["transaction_id"], index))
            # The deterministic staging name is part of the durable intent. Never
            # remove an unexpected occupant, including one left by another writer.
            staged = _entry(stage)
            if staged["kind"] == "missing":
                stage.symlink_to(after["link"], target_is_directory=True)
            elif not _matches(staged, after):
                raise LifecycleError("staging_conflict", "Transaction staging path has a foreign occupant.")
            _checkpoint(checkpoint, "step:{}:staged".format(index), tx)
            if _parent_identity(target) != tx["steps"][index]["parent_identity"] or str(canonical_entry(target)) != str(target):
                raise LifecycleError("recovery_conflict", "Target parent changed before atomic pointer replacement.")
            if not _matches(_entry(target), before):
                raise LifecycleError("recovery_conflict", "Target changed before atomic pointer replacement.")
            os.replace(stage, target)
        descriptor = os.open(str(target.parent), os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _commit(self, tx, *, check_source=True):
        plan = tx["plan"]
        installation = copy.deepcopy(plan["after"])
        installation.update({"updated_at": utc_now(), "last_transaction_id": tx["transaction_id"]})
        installation["generation"] = (plan["before"].get("generation", 0) if plan["before"] else 0) + 1
        grant = None
        if tx["grant_id"]:
            grant = {"schema_version": _SCHEMA + "grant/v1", "grant_id": tx["grant_id"],
                     "installation_id": plan["installation_id"], "skill_id": plan["skill"]["skill_id"],
                     "version_id": plan["version"]["version_id"], "plan_id": plan["plan_id"],
                     "target": installation["target"], "installation_generation": installation["generation"],
                     "transaction_id": tx["transaction_id"], "actor": tx["actor"], "approved_at": tx["created_at"]}
            installation["authorization"] = {"state": "valid", "grant_id": tx["grant_id"], "reason_codes": []}
        elif plan["operation"] == "revoke":
            installation["authorization"] = {**installation["authorization"], "state": "revoked", "reason_codes": ["explicit_revocation"]}
        receipt = {"schema_version": _SCHEMA + "receipt/v1", "receipt_id": uuid.uuid4().hex,
                   "transaction_id": tx["transaction_id"], "plan_id": plan["plan_id"], "operation": plan["operation"],
                   "installation_id": plan["installation_id"], "skill_id": plan["skill"]["skill_id"],
                   "version_id": plan["version"]["version_id"], "grant_id": installation["authorization"]["grant_id"],
                   "status": "completed", "completed_at": utc_now(), "steps": copy.deepcopy(tx["steps"])}
        installation["receipt_id"] = receipt["receipt_id"]
        with self.repository.atomic():
            self._preconditions(plan, targets=False, source=check_source)
            for step in tx["steps"]:
                if not _matches(_entry(step["path"]), step["after"]):
                    raise LifecycleError("effect_verification_failed", "Target changed at final publication boundary.")
            if installation["state"] == "active" and plan["operation"] != "revoke":
                verify_snapshot(plan["version"]["snapshot"])
            if self.repository.get("skill", plan["skill"]["skill_id"]) is None:
                self.repository.put("skill", plan["skill"]["skill_id"], plan["skill"])
            if self.repository.get("version", plan["version"]["version_id"]) is None:
                self.repository.put("version", plan["version"]["version_id"], plan["version"])
            if grant:
                self.repository.put("grant", grant["grant_id"], grant)
            grant_id = installation["authorization"]["grant_id"]
            if grant_id:
                old = self.repository.get("authorization", grant_id)
                self.repository.put("authorization", grant_id, installation["authorization"], expected_revision=old["revision"] if old else 0)
            self.repository.put("installation", plan["installation_id"], installation, expected_revision=plan["expected_revision"])
            self.repository.put("receipt", receipt["receipt_id"], receipt)
            tx["state"], tx["receipt_id"] = "completed", receipt["receipt_id"]
            self._persist_tx(tx)
            self.repository.append_event(plan["installation_id"], "transaction_completed",
                                         {"transaction_id": tx["transaction_id"], "receipt_id": receipt["receipt_id"], "operation": plan["operation"]}, actor=tx["actor"], tool="lifecycle")
        return receipt

    def recover(self, transaction_id, *, mode="inspect", approve_plan_id=None,
                actor="local-operator", checkpoint=None):
        tx = self.inspect_transaction(transaction_id)
        if mode == "inspect":
            return tx
        if mode not in {"resume", "compensate"}:
            raise LifecycleError("invalid_recovery", "Recovery mode must be inspect, resume or compensate.")
        if approve_plan_id != tx["plan"]["plan_id"]:
            raise LifecycleError("approval_required", "Recovery requires explicit approval of the recorded exact plan.")
        self._actor(actor)
        self._validate_plan(tx["plan"], physical_paths=False, resolve_source=False)
        with self._locks(tx["plan"]):
            return self._recover_locked(transaction_id, mode=mode, approve_plan_id=approve_plan_id, actor=actor, checkpoint=checkpoint)

    def _recover_locked(self, transaction_id, *, mode, approve_plan_id, actor, checkpoint=None):
        """Internal batch seam sharing core authorization and recovery checks."""
        tx = self.inspect_transaction(transaction_id)
        if mode not in {"resume", "compensate"} or approve_plan_id != tx["plan"]["plan_id"]:
            raise LifecycleError("approval_required", "Explicit recovery must name this exact saved plan ID.")
        self._actor(actor)
        self._validate_plan(tx["plan"], physical_paths=False, resolve_source=False)
        if tx["state"] == "completed":
            if mode != "resume":
                raise LifecycleError("invalid_recovery", "Completed work needs a new rollback plan, not compensation.")
            self._retry_health(tx)
            return self._get("receipt", tx["receipt_id"])["data"]
        if tx["state"] == "compensated":
            if mode == "compensate":
                return tx
            raise LifecycleError("invalid_recovery", "Compensated transaction cannot resume; generate a new plan.")
        self._preconditions(tx["plan"], targets=False, source=False, compensating=mode == "compensate")
        self.repository.append_event(transaction_id, "recovery_authorized", {"mode": mode, "plan_id": approve_plan_id}, actor=actor, tool="lifecycle")
        if mode == "resume":
            return self._execute(tx, checkpoint, recovering=True)
        return self._compensate(tx, checkpoint)

    def _compensate(self, tx, checkpoint):
        try:
            before_installation = tx["plan"]["before"]
            if before_installation and before_installation["state"] == "active":
                already_before = all(_matches(_entry(step["path"]), {key: value for key, value in step["before"].items() if key != "identity"}) for step in tx["steps"])
                if already_before:
                    # Cancelling unchanged pointers is not activation of old
                    # bytes. Persist any observed denial, but do not require a
                    # damaged installation to become healthy before cancelling.
                    self._verify_locked(before_installation["installation_id"])
                else:
                    verify_snapshot(self._version(before_installation["version_id"])["snapshot"])
            for index in reversed(range(len(tx["steps"]))):
                step = tx["steps"][index]
                actual = _entry(step["path"])
                # An original inode cannot be recreated after replacing a link;
                # compensation restores its reviewed link bytes, not inode identity.
                before = {key: value for key, value in step["before"].items() if key != "identity"}
                if _matches(actual, before):
                    self._clean_stage(step, tx, index)
                    step["state"] = "compensated"
                    self._persist_tx(tx)
                    continue
                if step["state"] not in {"intent", "completed", "compensating"} or not _matches(actual, step["after"]):
                    raise LifecycleError("recovery_conflict", "Compensation refuses to replace foreign target state.")
                step["state"] = "compensating"
                self._persist_tx(tx)
                _checkpoint(checkpoint, "compensate:{}:intent".format(index), tx)
                self._effect(step["path"], actual, before, tx, index, checkpoint)
                _checkpoint(checkpoint, "compensate:{}:effect".format(index), tx)
                if not _matches(_entry(step["path"]), before):
                    raise LifecycleError("recovery_conflict", "Compensation postcondition changed.")
                step["state"] = "compensated"
                self._persist_tx(tx)
            tx["state"] = "compensated"
            self._persist_tx(tx)
            return tx
        except Exception as error:
            tx["state"] = "recovery_needed"
            tx["recovery_errors"].append(_error(error))
            try:
                self._persist_tx(tx)
            except Exception as journal_error:
                raise LifecycleError("journal_write_failed", "Compensation and failure recording both failed.", details={"primary": _error(error), "journal": _error(journal_error), "transaction_id": tx["transaction_id"]}) from error
            if isinstance(error, LifecycleError):
                raise
            raise LifecycleError("compensation_failed", "Compensation failed; original and recovery errors are retained.", details={"transaction_id": tx["transaction_id"], "error": _error(error)}) from error

    def _clean_stage(self, step, tx, index):
        stage = Path(step["path"]).parent / (".skills-auditor-tx-{}-{}".format(tx["transaction_id"], index))
        observed = _entry(stage)
        if observed["kind"] == "missing":
            return
        if step["state"] not in {"intent", "completed", "compensating", "compensated"} or not _matches(observed, step["after"]):
            raise LifecycleError("recovery_conflict", "Refusing to remove a foreign transaction staging entry.")
        stage.unlink()
        descriptor = os.open(str(stage.parent), os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _evidence_checks(self, installation):
        """Check durable linkage without revisiting mutable historical inputs."""
        checks = []
        receipt = None
        try:
            record = self.repository.get("receipt", installation["receipt_id"])
            receipt = record["data"] if record else None
            expected = {"receipt_id": installation["receipt_id"], "status": "completed",
                        "installation_id": installation["installation_id"], "skill_id": installation["skill_id"],
                        "version_id": installation["version_id"], "transaction_id": installation["last_transaction_id"],
                        "grant_id": installation["authorization"]["grant_id"], "schema_version": _SCHEMA + "receipt/v1"}
            valid = bool(receipt) and _matches(receipt, expected)
            checks.append({"code": "receipt_record", "valid": bool(valid)})
        except (LifecycleError, OSError, KeyError, TypeError, ValueError) as error:
            checks.append({"code": "receipt_record", "valid": False, "error": _error(error)})
        try:
            record = self.repository.get("transaction", installation["last_transaction_id"])
            tx = record["data"] if record else None
            valid = bool(tx and receipt) and tx["state"] == "completed" and tx["receipt_id"] == receipt["receipt_id"]
            if valid:
                plan = tx["plan"]
                valid = (tx["transaction_id"] == receipt["transaction_id"]
                         and tx["approved_plan_id"] == plan["plan_id"] == receipt["plan_id"]
                         and plan["schema_version"] == _SCHEMA + "plan/v1"
                         and plan["plan_id"] == digest({key: value for key, value in plan.items() if key != "plan_id"})
                         and plan["installation_id"] == installation["installation_id"]
                         and plan["after"]["version_id"] == installation["version_id"]
                         and plan["after"]["target"] == installation["target"]
                         and plan["after"]["state"] == installation["state"]
                         and plan["operation"] == receipt["operation"])
                if valid and plan["operation"] in _GRANTING:
                    grant_record = self.repository.get("grant", installation["authorization"]["grant_id"])
                    grant = grant_record["data"] if grant_record else {}
                    valid = grant.get("plan_id") == plan["plan_id"] and grant.get("transaction_id") == tx["transaction_id"]
            checks.append({"code": "transaction_record", "valid": bool(valid)})
        except (LifecycleError, OSError, KeyError, TypeError, ValueError) as error:
            checks.append({"code": "transaction_record", "valid": False, "error": _error(error)})
        return checks

    @contextmanager
    def _inspection_context(self, installation_id):
        """Hold cooperating writer locks, without starting an observation."""
        self._state_safety()
        with locked_paths([self.state_root / "registry"]):
            record = self._get("installation", installation_id)
            with ExitStack() as stack:
                lock_error = None
                try:
                    stack.enter_context(locked_paths([Path(record["data"]["target"])]))
                except LifecycleError as error:
                    if error.code == "lock_contended":
                        raise
                    if error.code != "lock_parent_missing":
                        lock_error = error
                yield record, lock_error

    @contextmanager
    def _verification_run(self, record, checkpoint=None, *, require_completed=False, lock_error=None):
        """Shared durable fence; caller already holds the inspection locks."""
        installation_id = record["data"]["installation_id"]
        previous = self.repository.get("verification-run", installation_id)
        binding = {"grant_id": record["data"]["authorization"]["grant_id"], "version_id": record["data"]["version_id"], "generation": record["data"]["generation"]}
        completed = bool(previous and previous["data"].get("state") == "completed" and _matches(previous["data"], binding))
        if require_completed and not completed:
            raise LifecycleError("verification_required", "Cached integrity inspection requires a completed verification bound to this authorization; refresh verification first.")
        interrupted = bool(previous and previous["data"].get("state") == "in_progress" and _matches(previous["data"], binding))
        marker = {"state": "in_progress", "verification_id": uuid.uuid4().hex, "started_at": utc_now(), **binding}
        saved = self.repository.put("verification-run", installation_id, marker, expected_revision=previous["revision"] if previous else 0)
        if lock_error:
            raise lock_error
        _checkpoint(checkpoint, "verification:started", marker)
        # Missing parent is itself a failed observation. Contentious
        # cooperating locks never start an observation or revoke a grant.
        yield record, marker, saved["revision"], interrupted, previous if completed else None

    @contextmanager
    def _verification_context(self, installation_id, checkpoint=None, *, require_completed=False):
        with self._inspection_context(installation_id) as (record, lock_error):
            with self._verification_run(record, checkpoint, require_completed=require_completed, lock_error=lock_error) as context:
                yield context

    def verify(self, installation_id, *, checkpoint=None):
        return self._verify(installation_id, checkpoint=checkpoint, refresh=True)

    def check_cached_integrity(self, installation_id, *, checkpoint=None):
        """Inspect current health without refreshing clean cached evidence age.

        Failed observations use the normal durable denial path. An interrupted
        probe leaves the same bound fence as verification; exceptions are not a
        negative result that callers may bypass. This does not acquire a lease.
        """
        return self._verify(installation_id, checkpoint=checkpoint, refresh=False)["valid"]

    def _verify(self, installation_id, *, checkpoint=None, refresh=True):
        context = self._verification_context(installation_id, checkpoint, require_completed=not refresh)
        return self._observe_verification(installation_id, context, checkpoint=checkpoint, refresh=refresh)

    def _verify_locked(self, installation_id, *, observation=None, lock_error=None):
        """Internal retry/planning seam; never reacquire an already-held flock."""
        context = self._verification_run(self._get("installation", installation_id), lock_error=lock_error)
        return self._observe_verification(installation_id, context, refresh=False, observation=observation)

    def _observe_verification(self, installation_id, context, *, checkpoint=None, refresh=True, observation=None):
        from .incidents import record_verification
        from .status import publish_status
        with context as (record, marker, marker_revision, interrupted, previous_run):
            installation = record["data"]
            reasons, checks = [], []
            snapshot = {"snapshot_tree_sha256": None, "path": None}
            try:
                version = self._version(installation["version_id"])
                snapshot = version["snapshot"]
                verify_snapshot(snapshot)
                checks.append({"code": "snapshot_tree", "valid": True, "expected": snapshot["snapshot_tree_sha256"], "actual": snapshot["snapshot_tree_sha256"], "path": snapshot["path"]})
            except (LifecycleError, OSError, ValueError) as error:
                if observation is not None:
                    observation["snapshot_error"] = error
                reasons.append("snapshot_tree")
                actual = getattr(error, "details", {}).get("actual")
                actual = actual if isinstance(actual, str) and re.fullmatch(r"[a-f0-9]{64}", actual) else None
                checks.append({"code": "snapshot_tree", "valid": False, "expected": snapshot["snapshot_tree_sha256"], "actual": actual, "path": snapshot["path"], "error": _error(error)})
            try:
                actual_target = _entry(installation["target"])
                if observation is not None:
                    observation["target"] = actual_target
                self._owned_observation(installation, actual_target)
                checks.append({"code": "target_link", "valid": True})
            except (LifecycleError, OSError, ValueError) as error:
                if observation is not None and "target" not in observation:
                    observation["target_read_error"] = error
                reasons.append("target_link")
                checks.append({"code": "target_link", "valid": False, "error": _error(error)})
            evidence = self._evidence_checks(installation)
            checks.extend(evidence)
            reasons.extend(check["code"] for check in evidence if not check["valid"])
            authorization = copy.deepcopy(installation["authorization"])
            grant_record = self.repository.get("grant", authorization["grant_id"]) if authorization["grant_id"] else None
            projection = self.repository.get("authorization", authorization["grant_id"]) if authorization["grant_id"] else None
            grant = grant_record["data"] if grant_record else {}
            binding = {"installation_id": installation_id, "skill_id": installation["skill_id"], "version_id": installation["version_id"]}
            if installation["state"] == "active" and authorization["state"] == "valid":
                binding.update(target=installation["target"], installation_generation=installation["generation"])
            grant_valid = bool(projection) and projection["data"] == authorization and all(grant.get(key) == value for key, value in binding.items())
            checks.append({"code": "grant_binding", "valid": grant_valid, "expected": binding, "actual": {key: grant.get(key) for key in binding}})
            if not grant_valid:
                reasons.append("grant_binding")
            if projection and projection["data"]["state"] in {"invalidated", "revoked"}:
                authorization = copy.deepcopy(projection["data"])
            if interrupted:
                reasons.append("verification_interrupted")
            if reasons and authorization["state"] != "revoked":
                authorization["state"] = "invalidated"
                authorization["reason_codes"] = list(dict.fromkeys(authorization.get("reason_codes", []) + reasons))
            if authorization["state"] != "valid":
                reasons = list(dict.fromkeys(reasons + authorization.get("reason_codes", []) + ["approval_" + authorization["state"]]))
            verification = {"schema_version": _SCHEMA + "verification/v1", "verification_id": marker["verification_id"],
                            "installation_id": installation_id, "skill_id": installation["skill_id"],
                            "version_id": installation["version_id"], "receipt_id": installation["receipt_id"],
                            "grant_id": authorization["grant_id"], "observed_at": utc_now(),
                            "integrity": {"valid": all(check["valid"] for check in checks), "checks": checks},
                            "approval": {"state": authorization["state"], "requires_reapproval": authorization["state"] != "valid", "reason_codes": reasons},
                            "valid": not reasons}
            _checkpoint(checkpoint, "verification:observed", verification)
            if not refresh and verification["valid"] and previous_run is not None:
                # The probe made no new freshness claim. Restore only the exact
                # previous completed marker while holding the original locks.
                self.repository.put("verification-run", installation_id, previous_run["data"], expected_revision=marker_revision)
                return verification
            with self.repository.atomic():
                self.repository.put("verification", verification["verification_id"], verification)
                latest = self.repository.get("latest-verification", installation_id)
                self.repository.put("latest-verification", installation_id, {"verification_id": verification["verification_id"]}, expected_revision=latest["revision"] if latest else 0)
                if installation["authorization"] != authorization:
                    installation["authorization"] = authorization
                    self.repository.put("installation", installation_id, installation, expected_revision=record["revision"])
                if authorization["grant_id"] and (not self.repository.get("authorization", authorization["grant_id"]) or self.repository.get("authorization", authorization["grant_id"])["data"] != authorization):
                    previous = self.repository.get("authorization", authorization["grant_id"])
                    self.repository.put("authorization", authorization["grant_id"], authorization, expected_revision=previous["revision"] if previous else 0)
                self.repository.put("verification-run", installation_id, {**marker, "state": "completed", "completed_at": verification["observed_at"]}, expected_revision=marker_revision)
                self.repository.append_event(installation_id, "verification_completed",
                                             {"verification_id": verification["verification_id"], "valid": verification["valid"], "reason_codes": reasons}, actor="local-observer", tool="lifecycle")
            # A failed derived projection must never undo an observed denial.
            # The started/completed marker also fences stale cached evidence.
            record_verification(self.repository, installation, verification)
            with self.repository.atomic():
                publish_status(self.repository, installation, verification)
            return verification
