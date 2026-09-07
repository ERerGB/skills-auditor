"""Reviewed local batches with parent WAL, child recovery and explicit inverses.

The database and multiple host roots are not globally atomic. A parent intent
precedes every child effect, and each child uses the single-object transaction
engine. Compensation is a new reviewed plan, never restoration of old grants.
"""

import copy
from pathlib import Path
import uuid

from .common import LifecycleError, digest, paths_overlap, utc_now
from .engine import _GRANTING, _entry, _entry_key, _error, _identifier, _matches, _timestamp
from .snapshots import verify_snapshot


_SCHEMA = "skills-auditor-lifecycle-batch-"
_CHILD_FIELDS = {"kind", "plan", "transaction_id", "compensates_transaction_id", "note"}


def _signal(checkpoint, name, value):
    if checkpoint is not None:
        checkpoint(name, copy.deepcopy(value))


class BatchManager:
    """Coordinate distinct installation identities in one local project."""

    def __init__(self, manager):
        self.manager = manager
        self.repository = manager.repository

    def _record(self, batch_id):
        _identifier(batch_id, "batch ID")
        record = self.repository.get("batch", batch_id)
        if record is None:
            raise LifecycleError("batch_missing", "Requested batch does not exist.")
        data = record["data"]
        try:
            fields = {"schema_version", "batch_id", "plan", "approved_plan_id", "actor", "created_at", "state", "children", "error", "recovery_errors", "receipt_id", "compensation_batch_id"}
            if (type(data) is not dict or set(data) != fields or data["schema_version"] != _SCHEMA + "transaction/v1"
                    or data["batch_id"] != batch_id or data["state"] not in {"prepared", "applying", "recovery_needed", "completed", "compensating", "compensated"}):
                raise ValueError("batch identity or state")
            self._validate(data["plan"], static=True)
            self.manager._actor(data["actor"])
            _timestamp(data["created_at"])
            if data["approved_plan_id"] != data["plan"]["plan_id"] or type(data["children"]) is not list or len(data["children"]) != len(data["plan"]["children"]):
                raise ValueError("approved plan or child cardinality")
            for index, (child, reference) in enumerate(zip(data["plan"]["children"], data["children"])):
                if (type(reference) is not dict or set(reference) != {"index", "transaction_id", "state", "receipt_id"}
                        or type(reference["index"]) is not int or reference["index"] != index
                        or reference["transaction_id"] != self._child_id(batch_id, index, child)
                        or reference["state"] not in {"pending", "running", "completed", "compensated"}):
                    raise ValueError("deterministic child identity")
                if (reference["state"] == "completed") != (reference["receipt_id"] is not None):
                    raise ValueError("child receipt state")
                if reference["receipt_id"] is not None:
                    _identifier(reference["receipt_id"])
                if data["state"] == "completed" and reference["state"] != ("completed" if child["kind"] == "apply" else "compensated"):
                    raise ValueError("incomplete child in completed parent")
            if data["state"] == "completed" and data["receipt_id"] is None:
                raise ValueError("completed parent receipt")
            for key in ("receipt_id", "compensation_batch_id"):
                if data[key] is not None:
                    _identifier(data[key])
            if type(data["recovery_errors"]) is not list:
                raise ValueError("recovery errors")
            for error in ([data["error"]] if data["error"] is not None else []) + data["recovery_errors"]:
                if type(error) is not dict or not isinstance(error.get("code"), str) or not isinstance(error.get("message"), str):
                    raise ValueError("bounded error shape")
        except (LifecycleError, KeyError, TypeError, ValueError, AttributeError, RecursionError) as error:
            raise LifecycleError("batch_corrupt", "Batch record is malformed or contradicts its exact reviewed plan.") from error
        return record

    def inspect(self, batch_id):
        """Historical inspection does not re-read mutable candidate content."""
        tx = self._record(batch_id)["data"]
        self._history_proof(tx)
        return copy.deepcopy(tx)

    def _receipt(self, tx):
        record = self.repository.get("batch-receipt", tx["receipt_id"]) if tx["receipt_id"] else None
        receipt = record["data"] if record else {}
        expected = {"schema_version": _SCHEMA + "receipt/v1", "batch_id": tx["batch_id"],
                    "plan_id": tx["plan"]["plan_id"], "receipt_id": tx["receipt_id"], "status": "completed",
                    "children": tx["children"], "compensates_batch_id": tx["plan"]["compensates_batch_id"],
                    "uncompensated": tx["plan"]["uncompensated"]}
        if type(receipt) is not dict or set(receipt) != set(expected) | {"completed_at"} or not _matches(receipt, expected):
            raise LifecycleError("batch_receipt_invalid", "Batch receipt is missing or contradicts its exact completed plan.")
        try:
            _timestamp(receipt["completed_at"])
        except (ValueError, TypeError) as error:
            raise LifecycleError("batch_receipt_invalid", "Batch receipt completion timestamp is malformed.") from error
        if not any(event["event_type"] == "batch_completed" and event["payload"] == {"receipt_id": receipt["receipt_id"]}
                   for event in self.repository.events(tx["batch_id"])):
            raise LifecycleError("batch_completion_missing", "The append-only batch completion proof is missing.")
        return receipt

    def _history_proof(self, tx, seen=None):
        """Validate metadata completion without consulting current filesystem health."""
        seen = set() if seen is None else seen
        if tx["batch_id"] in seen or len(seen) > 50:
            raise LifecycleError("batch_corrupt", "Batch compensation history contains a cycle.")
        seen.add(tx["batch_id"])
        for child, reference in zip(tx["plan"]["children"], tx["children"]):
            if reference["state"] not in {"completed", "compensated"}:
                continue
            recorded = self._child_tx(child, reference)
            if recorded is None or recorded["state"] != reference["state"] or recorded["receipt_id"] != reference["receipt_id"]:
                raise LifecycleError("batch_child_invalid", "Recorded terminal child lacks matching durable completion.")
            if reference["state"] == "completed":
                receipt = self.manager._get("receipt", reference["receipt_id"])["data"]
                expected = {"schema_version": "skills-auditor-lifecycle-receipt/v1", "status": "completed",
                            "receipt_id": reference["receipt_id"], "transaction_id": reference["transaction_id"],
                            "plan_id": child["plan"]["plan_id"], "installation_id": child["plan"]["installation_id"],
                            "version_id": child["plan"]["version"]["version_id"], "skill_id": child["plan"]["skill"]["skill_id"],
                            "operation": child["plan"]["operation"], "steps": recorded["steps"],
                            "grant_id": recorded["grant_id"] if child["plan"]["operation"] in _GRANTING else child["plan"]["before"]["authorization"]["grant_id"]}
                if not _matches(receipt, expected):
                    raise LifecycleError("batch_child_invalid", "Historical child receipt does not bind its completed core transaction.")
        if tx["receipt_id"] is not None:
            self._receipt(tx)
        if tx["state"] in {"compensated", "compensating"}:
            if not tx["compensation_batch_id"]:
                raise LifecycleError("batch_compensation_missing", "Original batch lacks its inverse batch identity.")
            inverse = self._record(tx["compensation_batch_id"])["data"]
            if inverse["plan"]["compensates_batch_id"] != tx["batch_id"]:
                raise LifecycleError("batch_compensation_missing", "Inverse batch refers to a different original batch.")
            if tx["state"] == "compensated":
                if inverse["state"] != "completed" or inverse["plan"]["uncompensated"]:
                    raise LifecycleError("batch_compensation_missing", "Original batch has no complete inverse proof.")
                self._history_proof(inverse, seen)
        if tx["state"] == "completed" and tx["plan"]["compensates_batch_id"]:
            original = self._record(tx["plan"]["compensates_batch_id"])["data"]
            expected_state = "recovery_needed" if tx["plan"]["uncompensated"] else "compensated"
            if original["compensation_batch_id"] != tx["batch_id"] or original["state"] != expected_state:
                raise LifecycleError("batch_compensation_missing", "Inverse completion and original batch state disagree.")

    def _put(self, tx):
        prior = self.repository.get("batch", tx["batch_id"])
        return self.repository.put("batch", tx["batch_id"], tx, expected_revision=prior["revision"] if prior else 0)

    def plan(self, child_plans):
        if not isinstance(child_plans, list) or not 1 <= len(child_plans) <= 50:
            raise LifecycleError("invalid_batch_plan", "An ordinary batch requires 1–50 distinct reviewed child plans.", exit_code=2)
        children = [{"kind": "apply", "plan": copy.deepcopy(plan), "transaction_id": None,
                     "compensates_transaction_id": None, "note": "Execute the exact reviewed child plan."} for plan in child_plans]
        return self._plan(children)

    def _plan(self, children, *, compensates=None, revision=0, uncompensated=None):
        plan = {"schema_version": _SCHEMA + "plan/v1", "project_root": str(self.manager.project_root),
                "created_at": utc_now(), "children": children, "compensates_batch_id": compensates,
                "compensates_revision": revision, "uncompensated": uncompensated or []}
        plan["plan_id"] = digest(plan)
        self._validate(plan)
        return plan

    def _targets(self, plan):
        return [Path(step["path"]) for child in plan["children"] for step in child["plan"]["steps"]]

    def _locks(self, plan):
        return self.manager._operation_locks([child["plan"] for child in plan["children"]])

    def _validate(self, plan, *, static=False):
        try:
            if not isinstance(plan, dict) or set(plan) != {"schema_version", "plan_id", "project_root", "created_at", "children", "compensates_batch_id", "compensates_revision", "uncompensated"}:
                raise ValueError("batch plan fields")
            if plan["schema_version"] != _SCHEMA + "plan/v1" or plan["project_root"] != str(self.manager.project_root):
                raise ValueError("batch schema or project")
            if plan["plan_id"] != digest({key: value for key, value in plan.items() if key != "plan_id"}):
                raise ValueError("batch checksum")
            _timestamp(plan["created_at"])
            compensation = plan["compensates_batch_id"] is not None
            if compensation:
                _identifier(plan["compensates_batch_id"])
            if type(plan["compensates_revision"]) is not int or (plan["compensates_revision"] > 0) != compensation:
                raise ValueError("compensation revision")
            if not compensation and plan["compensates_revision"] != 0:
                raise ValueError("ordinary batch revision")
            if type(plan["children"]) is not list or not (0 if compensation else 1) <= len(plan["children"]) <= 50:
                raise ValueError("child count")
            if type(plan["uncompensated"]) is not list or len(plan["uncompensated"]) > 50 or (not compensation and plan["uncompensated"]):
                raise ValueError("uncompensated items")
            identifiers, targets, sources = set(), [], []
            references = []
            for child in plan["children"]:
                if type(child) is not dict or set(child) != _CHILD_FIELDS or child["kind"] not in {"apply", "compensate"}:
                    raise ValueError("child shape")
                if not isinstance(child["note"], str) or not child["note"].strip() or len(child["note"]) > 1000:
                    raise ValueError("child explanation")
                if static:
                    core = child["plan"]
                    if (type(core) is not dict or core.get("schema_version") != "skills-auditor-lifecycle-plan/v1"
                            or core.get("project_root") != str(self.manager.project_root)
                            or core.get("plan_id") != digest({key: value for key, value in core.items() if key != "plan_id"})):
                        raise ValueError("historical core plan envelope")
                else:
                    self.manager._validate_plan(child["plan"], physical_paths=False)
                if child["kind"] == "apply" and child["transaction_id"] is not None:
                    raise ValueError("apply child cannot adopt a transaction")
                if child["kind"] == "compensate":
                    if not compensation or child["transaction_id"] != child["compensates_transaction_id"]:
                        raise ValueError("compensation child identity")
                    _identifier(child["transaction_id"])
                if compensation:
                    _identifier(child["compensates_transaction_id"])
                    references.append(child["compensates_transaction_id"])
                elif child["compensates_transaction_id"] is not None:
                    raise ValueError("unexpected compensation reference")
                identity = child["plan"]["installation_id"]
                if identity in identifiers:
                    raise ValueError("a batch cannot chain operations on one installation")
                identifiers.add(identity)
                current_targets = {child["plan"]["after"]["target"]}
                if child["plan"]["before"]:
                    current_targets.add(child["plan"]["before"]["target"])
                if any(paths_overlap(target, previous) for target in current_targets for previous in targets):
                    raise ValueError("batch targets overlap")
                targets.extend(current_targets)
                if child["plan"]["source"]:
                    sources.append(child["plan"]["source"])
            if any(paths_overlap(target, source) for target in targets for source in sources):
                raise ValueError("a batch target overlaps another child candidate")
            for item in plan["uncompensated"]:
                if type(item) is not dict or set(item) != {"transaction_id", "reason_code"}:
                    raise ValueError("uncompensated shape")
                _identifier(item["transaction_id"])
                _identifier(item["reason_code"])
                references.append(item["transaction_id"])
            if len(references) != len(set(references)):
                raise ValueError("duplicate compensation reference")
            if compensation and not static:
                original = self._record(plan["compensates_batch_id"])["data"]
                self._history_proof(original)
                known = {item["transaction_id"]: (child, item) for child, item in zip(original["plan"]["children"], original["children"])}
                if any(reference not in known for reference in references):
                    raise ValueError("compensation refers outside its original batch")
                for child in plan["children"]:
                    tx = self._child_tx(*known[child["compensates_transaction_id"]])
                    if tx is None:
                        raise ValueError("compensation transaction is missing")
                    if child["kind"] == "compensate":
                        if child["plan"] != tx["plan"] or tx["state"] == "completed":
                            raise ValueError("unfinished compensation must use its exact original plan")
                    else:
                        operation, arguments, _ = self._inverse_spec(tx)
                        if operation is None or child["plan"]["operation"] != operation:
                            raise ValueError("inverse operation differs from reviewed original effects")
                        if "installation_id" in arguments and child["plan"]["installation_id"] != arguments["installation_id"]:
                            raise ValueError("inverse installation differs from original child")
                        if any(child["plan"]["after"].get(key) != value for key, value in arguments.items() if key in {"target", "version_id", "name", "skill_id"}):
                            raise ValueError("inverse binding differs from original effects")
        except (KeyError, TypeError, ValueError, AttributeError, RecursionError) as error:
            raise LifecycleError("invalid_batch_plan", "Malformed batch plan: {}".format(str(error)[:500]), exit_code=2) from error

    def _child_id(self, batch_id, index, child):
        return child["transaction_id"] if child["kind"] == "compensate" else "batch-" + digest({"batch_id": batch_id, "index": index, "plan_id": child["plan"]["plan_id"]})

    def _child_tx(self, child, reference):
        record = self.repository.get("transaction", reference["transaction_id"])
        if not record:
            return None
        tx = record["data"]
        try:
            if (type(tx) is not dict or tx.get("schema_version") != "skills-auditor-lifecycle-transaction/v1"
                    or tx.get("transaction_id") != reference["transaction_id"] or tx.get("plan") != child["plan"]
                    or tx.get("approved_plan_id") != child["plan"]["plan_id"]
                    or tx["state"] not in {"prepared", "applying", "recovery_needed", "completed", "compensated"}
                    or type(tx["steps"]) is not list or len(tx["steps"]) != len(child["plan"]["steps"])):
                raise ValueError("child transaction binding")
            if child["plan"]["operation"] in _GRANTING:
                _identifier(tx["grant_id"])
            elif tx["grant_id"] is not None:
                raise ValueError("nongranting child grant")
            for step, planned in zip(tx["steps"], child["plan"]["steps"]):
                if (type(step) is not dict or set(step) != set(planned) | {"state"}
                        or {key: value for key, value in step.items() if key != "state"} != planned
                        or step["state"] not in {"pending", "intent", "completed", "compensating", "compensated"}):
                    raise ValueError("child step intent")
                if tx["state"] in {"completed", "compensated"} and step["state"] != tx["state"]:
                    raise ValueError("child step is not terminal")
        except (LifecycleError, KeyError, TypeError, ValueError) as error:
            raise LifecycleError("batch_child_conflict", "Child transaction no longer matches its reviewed batch binding or completion.") from error
        return tx

    def _compensated_health(self, tx):
        self.manager._preconditions(tx["plan"], targets=False, source=False, compensating=True)
        before = tx["plan"]["before"]
        if before and before["state"] == "active":
            # Compensation completion proves owned pointer restoration, not
            # approval or current payload health. Keep newly observed denial.
            self.manager._verify_locked(before["installation_id"])
        for step in tx["steps"]:
            if not _matches(_entry(step["path"]), {key: value for key, value in step["before"].items() if key != "identity"}):
                raise LifecycleError("recovery_conflict", "A compensated child no longer has its restored owned pointer.")

    def _preflight(self, plan, children, *, batch_id, recovering=False):
        keys = {_entry_key(path) for path in self._targets(plan)}
        for child, reference in zip(plan["children"], children):
            for index, step in enumerate(child["plan"]["steps"]):
                stage = Path(step["path"]).parent / (".skills-auditor-tx-{}-{}".format(reference["transaction_id"], index))
                if _entry_key(stage) in keys:
                    raise LifecycleError("staging_conflict", "A child staging entry aliases another batch target.")
            tx = self._child_tx(child, reference)
            if tx is None and child["compensates_transaction_id"]:
                original_tx = self.manager.inspect_transaction(child["compensates_transaction_id"])
                current = self.manager.get_installation(original_tx["plan"]["installation_id"])
                if current["last_transaction_id"] != original_tx["transaction_id"]:
                    raise LifecycleError("stale_batch", "A later lifecycle operation superseded the reviewed original child.")
            if reference["state"] in {"completed", "compensated"} and (tx is None or tx["state"] != reference["state"] or tx["receipt_id"] != reference["receipt_id"]):
                raise LifecycleError("batch_child_invalid", "Parent completion reference lacks matching durable child completion.")
            if tx is None:
                if child["kind"] != "apply" or reference["state"] not in ({"pending", "running"} if recovering else {"pending"}):
                    raise LifecycleError("batch_child_missing", "Recorded child transaction is missing.")
                self.manager._check_inflight(child["plan"], batch_id=batch_id, compensates_batch_id=plan["compensates_batch_id"])
                self.manager._admission_preconditions(child["plan"])
                if not child["plan"]["source"] and child["plan"]["after"]["state"] == "active" and child["plan"]["operation"] != "revoke":
                    verify_snapshot(child["plan"]["version"]["snapshot"])
            elif not recovering and child["kind"] == "apply":
                raise LifecycleError("batch_child_conflict", "A new parent cannot adopt an existing apply transaction.")
            elif child["kind"] == "compensate":
                if tx["state"] == "compensated":
                    self._compensated_health(tx)
                else:
                    self.manager._preconditions(tx["plan"], targets=False, source=False, compensating=True)
                    for step in tx["steps"]:
                        actual = _entry(step["path"])
                        before = {key: value for key, value in step["before"].items() if key != "identity"}
                        if not _matches(actual, before) and not _matches(actual, step["after"]):
                            raise LifecycleError("recovery_conflict", "Compensation would overwrite a foreign target.")
            elif tx["state"] == "completed":
                self.manager._retry_health(tx)
            elif recovering:
                self.manager._preconditions(tx["plan"], targets=False, source=not tx["snapshot_ready"])
            else:
                raise LifecycleError("batch_child_conflict", "A new batch cannot adopt an unfinished apply transaction.")
        if plan["compensates_batch_id"]:
            original = self._record(plan["compensates_batch_id"])
            self._history_proof(original["data"])
            owned = original["data"].get("compensation_batch_id") == batch_id
            if not owned and (original["revision"] != plan["compensates_revision"] or original["data"].get("compensation_batch_id") is not None):
                raise LifecycleError("stale_batch", "Original batch changed after its compensation was reviewed.")
            represented = {child["compensates_transaction_id"] for child in plan["children"]} | {item["transaction_id"] for item in plan["uncompensated"]}
            for original_child, reference in zip(original["data"]["plan"]["children"], original["data"]["children"]):
                record = self.repository.get("transaction", reference["transaction_id"])
                if not record:
                    self.manager._preconditions(original_child["plan"], source=False)
                if record and record["data"]["state"] != "compensated" and reference["transaction_id"] not in represented:
                    raise LifecycleError("invalid_batch_plan", "Inverse plan omits a child with possible effects.", exit_code=2)

    def apply(self, plan, *, approve_plan_id, batch_id=None, actor="local-operator", checkpoint=None):
        self._validate(plan)
        if approve_plan_id != plan["plan_id"]:
            raise LifecycleError("approval_required", "Explicit approval must name the exact batch plan ID.")
        self.manager._actor(actor)
        batch_id = _identifier(batch_id or uuid.uuid4().hex, "batch ID")
        with self._locks(plan):
            existing = self.repository.get("batch", batch_id)
            if existing:
                tx = self._record(batch_id)["data"]
                if tx["plan"] != plan or tx["approved_plan_id"] != approve_plan_id:
                    raise LifecycleError("batch_conflict", "Batch ID is already bound to another exact plan.")
                if tx["state"] == "completed":
                    return self._completed(tx)
                raise LifecycleError("recovery_required", "An existing incomplete batch requires explicit resume or a newly reviewed inverse plan.", details={"batch_id": batch_id})
            children = [{"index": index, "transaction_id": self._child_id(batch_id, index, child), "state": "pending", "receipt_id": None} for index, child in enumerate(plan["children"])]
            self._preflight(plan, children, batch_id=batch_id)
            tx = {"schema_version": _SCHEMA + "transaction/v1", "batch_id": batch_id, "plan": copy.deepcopy(plan),
                  "approved_plan_id": approve_plan_id, "actor": actor, "created_at": utc_now(), "state": "prepared",
                  "children": children, "error": None, "recovery_errors": [], "receipt_id": None, "compensation_batch_id": None}
            try:
                with self.repository.atomic():
                    self._put(tx)
                    if plan["compensates_batch_id"]:
                        original = self._record(plan["compensates_batch_id"])["data"]
                        original.update(state="compensating", compensation_batch_id=batch_id)
                        self._put(original)
                    self.repository.append_event(batch_id, "batch_approved", {"plan_id": plan["plan_id"]}, actor, "lifecycle-batch")
            except Exception as error:
                raise LifecycleError("batch_journal_failed", "Parent intent could not be confirmed; no child effect was started. Inspect this batch ID before retrying.", details={"batch_id": batch_id, "error": _error(error)}) from error
            return self._execute(tx, checkpoint, recovering=False)

    def _completed(self, tx):
        self._history_proof(tx)
        receipt = self._receipt(tx)
        self._preflight(tx["plan"], tx["children"], batch_id=tx["batch_id"], recovering=True)
        return receipt

    def recover(self, batch_id, *, mode="inspect", approve_plan_id=None, actor="local-operator", checkpoint=None):
        tx = self.inspect(batch_id)
        if mode == "inspect":
            return tx
        if mode != "resume":
            raise LifecycleError("invalid_recovery", "Batch compensation requires a new reviewed inverse plan; recovery supports inspect or resume.")
        self._validate(tx["plan"])
        if approve_plan_id != tx["plan"]["plan_id"]:
            raise LifecycleError("approval_required", "Explicit resume must name the recorded exact batch plan ID.")
        self.manager._actor(actor)
        with self._locks(tx["plan"]):
            tx = self.inspect(batch_id)
            if tx["state"] in {"compensating", "compensated"} or tx["compensation_batch_id"] is not None:
                raise LifecycleError("batch_compensation_started", "Original work cannot resume after an explicit inverse batch claimed it.")
            if tx["state"] == "completed":
                return self._completed(tx)
            self._preflight(tx["plan"], tx["children"], batch_id=batch_id, recovering=True)
            self.repository.append_event(batch_id, "batch_resume_approved", {"plan_id": approve_plan_id}, actor, "lifecycle-batch")
            return self._execute(tx, checkpoint, recovering=True)

    def _execute(self, tx, checkpoint, *, recovering):
        try:
            _signal(checkpoint, "batch:prepared", tx)
            tx["state"] = "applying"
            self._put(tx)
            for index, (child, reference) in enumerate(zip(tx["plan"]["children"], tx["children"])):
                recorded = self._child_tx(child, reference)
                if reference["state"] in {"completed", "compensated"}:
                    continue
                reference["state"] = "running"
                self._put(tx)
                _signal(checkpoint, "batch:child:{}:started".format(index), tx)
                callback = (lambda name, value, index=index: _signal(checkpoint, "batch:child:{}:".format(index) + name, value)) if checkpoint else None
                arguments = {"approve_plan_id": child["plan"]["plan_id"], "actor": tx["actor"], "checkpoint": callback}
                if child["kind"] == "compensate":
                    result = self.manager._recover_locked(reference["transaction_id"], mode="compensate", **arguments)
                    reference.update(state="compensated", receipt_id=None)
                elif recorded:
                    result = self.manager._recover_locked(reference["transaction_id"], mode="resume", **arguments)
                    reference.update(state="completed", receipt_id=result["receipt_id"])
                else:
                    result = self.manager._apply_locked(child["plan"], transaction_id=reference["transaction_id"], batch_id=tx["batch_id"], **arguments)
                    reference.update(state="completed", receipt_id=result["receipt_id"])
                self._put(tx)
                _signal(checkpoint, "batch:child:{}:recorded".format(index), tx)
            _signal(checkpoint, "batch:before_commit", tx)
            self._preflight(tx["plan"], tx["children"], batch_id=tx["batch_id"], recovering=True)
            receipt = {"schema_version": _SCHEMA + "receipt/v1", "receipt_id": uuid.uuid4().hex,
                       "batch_id": tx["batch_id"], "plan_id": tx["plan"]["plan_id"], "status": "completed",
                       "completed_at": utc_now(), "children": copy.deepcopy(tx["children"]),
                       "compensates_batch_id": tx["plan"]["compensates_batch_id"], "uncompensated": copy.deepcopy(tx["plan"]["uncompensated"])}
            with self.repository.atomic():
                self.repository.put("batch-receipt", receipt["receipt_id"], receipt)
                tx.update(state="completed", receipt_id=receipt["receipt_id"])
                self._put(tx)
                if tx["plan"]["compensates_batch_id"]:
                    original = self._record(tx["plan"]["compensates_batch_id"])["data"]
                    original["state"] = "recovery_needed" if receipt["uncompensated"] else "compensated"
                    self._put(original)
                self.repository.append_event(tx["batch_id"], "batch_completed", {"receipt_id": receipt["receipt_id"]}, tx["actor"], "lifecycle-batch")
            _signal(checkpoint, "batch:committed", tx)
            return receipt
        except Exception as error:
            try:
                persisted = self._record(tx["batch_id"])["data"]
            except Exception as journal_error:
                raise LifecycleError("batch_journal_failed", "Batch execution failed and its durable completion state cannot be read; inspect before retrying or compensating.", details={"batch_id": tx["batch_id"], "primary": _error(error), "journal": _error(journal_error)}) from error
            if persisted["state"] == "completed":
                raise LifecycleError("committed_response_failed", "Batch committed; inspect its recorded receipt.", details={"batch_id": tx["batch_id"], "error": _error(error)}) from error
            tx.update(state="recovery_needed", receipt_id=None)
            if tx["error"] is None:
                tx["error"] = _error(error)
            else:
                tx["recovery_errors"].append(_error(error))
            try:
                self._put(tx)
            except Exception as journal_error:
                raise LifecycleError("batch_journal_failed", "Batch execution and failure recording both failed; inspect durable child intents.", details={"batch_id": tx["batch_id"], "primary": _error(error), "journal": _error(journal_error)}) from error
            raise LifecycleError("batch_failed", "Batch is incomplete; inspect child outcomes and explicitly resume or plan compensation.", details={"batch_id": tx["batch_id"], "primary": _error(error)}) from error

    def _inverse_spec(self, tx):
        plan, before = tx["plan"], tx["plan"]["before"]
        operation = plan["operation"]
        arguments = {"installation_id": plan["installation_id"]}
        note = "New inverse transaction; historical grants and receipts remain unchanged."
        if operation in {"install", "migrate", "install-retained"}:
            return "uninstall", arguments, note
        if operation == "uninstall":
            if before["state"] != "active":
                return None, {}, "inactive_uninstall_requires_separate_review"
            return "install-retained", {"target": before["target"], "skill_id": before["skill_id"], "version_id": before["version_id"], "name": before["name"]}, "Create a NEW installation identity and NEW approval from retained bytes; the old identity stays uninstalled."
        if operation in {"update", "edit", "rollback"}:
            return "rollback", {**arguments, "version_id": before["version_id"]}, note
        if operation == "move":
            return "move", {**arguments, "target": before["target"]}, note
        if operation == "rename":
            return "rename", {**arguments, "name": before["name"]}, note
        if operation in {"disable", "archive", "enable"}:
            inverse = {"active": "enable", "disabled": "disable", "archived": "archive"}[before["state"]]
            if inverse == "disable" and plan["after"]["state"] != "active":
                return None, {}, "inactive_transition_requires_separate_review"
            return inverse, arguments, "Restoring an active installation explicitly creates a NEW grant; it does not restore historical approval."
        if operation in {"renew", "revoke"}:
            if before["authorization"]["state"] != "valid":
                return "revoke", arguments, "Preserve denial using a NEW explicit revocation; never revive the historical grant."
            if before["state"] != "active":
                return None, {}, "inactive_authorization_requires_separate_review"
            return "renew", arguments, "Explicitly create a NEW approval of current retained bytes; an old approval cannot be resurrected."
        return None, {}, "inverse_unsupported"

    def plan_compensation(self, batch_id):
        original = self._record(batch_id)
        tx = original["data"]
        self._validate(tx["plan"])
        with self._locks(tx["plan"]):
            original = self._record(batch_id)
            tx = original["data"]
            self._history_proof(tx)
            if tx["compensation_batch_id"] is not None or tx["state"] == "compensated":
                raise LifecycleError("batch_compensation_started", "Inspect or resume the existing inverse batch instead of creating another.")
            children, unsupported = [], []
            for child, reference in reversed(list(zip(tx["plan"]["children"], tx["children"]))):
                recorded = self._child_tx(child, reference)
                if recorded is None:
                    self.manager._preconditions(child["plan"], source=False)
                    continue
                if recorded["state"] == "compensated":
                    self._compensated_health(recorded)
                    continue
                if recorded["state"] != "completed":
                    children.append({"kind": "compensate", "plan": copy.deepcopy(recorded["plan"]), "transaction_id": recorded["transaction_id"],
                                     "compensates_transaction_id": recorded["transaction_id"], "note": "Compensate only owned unfinished effects; preserve any durable authorization denial."})
                    continue
                installation = self.manager.get_installation(recorded["plan"]["installation_id"])
                if installation["last_transaction_id"] != recorded["transaction_id"] or not all(check["valid"] for check in self.manager._evidence_checks(installation)):
                    raise LifecycleError("stale_batch", "A later lifecycle operation or invalid receipt superseded an original child.")
                operation, arguments, note = self._inverse_spec(recorded)
                if operation is None:
                    unsupported.append({"transaction_id": recorded["transaction_id"], "reason_code": note})
                    continue
                try:
                    inverse = self.manager._plan_locked(operation, **arguments)
                except LifecycleError as error:
                    unsupported.append({"transaction_id": recorded["transaction_id"], "reason_code": error.code})
                    continue
                children.append({"kind": "apply", "plan": inverse, "transaction_id": None,
                                 "compensates_transaction_id": recorded["transaction_id"], "note": note})
            return self._plan(children, compensates=batch_id, revision=original["revision"], uncompensated=unsupported)
