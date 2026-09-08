"""Independent, read-only sequence oracle for the frozen lifecycle model.

This module imports no lifecycle implementation. Public adapters perform actions;
the oracle reads committed SQLite rows and explicit filesystem expectations. It
never calls verification, production validators, recovery or reference walkers.
"""

import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat


# Literal reference transitions, not aliases of production operation sets.
TRANSITIONS = {
    "install": ({None}, "active", "new"),
    "install-retained": ({None}, "active", "new"),
    "migrate": ({None}, "active", "new"),
    "update": ({"active"}, "active", "new"),
    "edit": ({"active"}, "active", "new"),
    "rollback": ({"active"}, "active", "new"),
    "renew": ({"active"}, "active", "new"),
    "move": ({"active"}, "active", "new"),
    "rename": ({"active", "disabled", "archived"}, None, "new"),
    "disable": ({"active"}, "disabled", "preserve"),
    "archive": ({"active", "disabled", "archived"}, "archived", "preserve"),
    "enable": ({"disabled", "archived"}, "active", "new"),
    "uninstall": ({"active", "disabled", "archived"}, "uninstalled", "preserve"),
    "revoke": ({"active", "disabled", "archived"}, None, "revoke"),
}


def digest(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def expected_transition(before, operation):
    """Return the literal expected lifecycle, authorization policy and generation."""
    allowed, state, grant = TRANSITIONS[operation]
    previous = before["state"] if before else None
    if previous not in allowed:
        raise ValueError("transition is outside the reference model")
    return {
        "state": state or previous,
        "grant_policy": grant,
        "authorization": ("valid" if grant == "new" else "revoked" if grant == "revoke"
                          else before["authorization"]["state"]),
        "generation": before["generation"] + 1 if before else 1,
    }


def tree_state(path):
    """Read exact relative payload/modes without following any tree symlink."""
    root = Path(path)
    result = {}
    pending = [(root, ".")]
    while pending:
        item, relative = pending.pop()
        info = item.lstat()
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISLNK(info.st_mode):
            result[relative] = ("symlink", mode, os.readlink(item))
        elif stat.S_ISDIR(info.st_mode):
            result[relative] = ("directory", mode)
            pending.extend((child, str(child.relative_to(root))) for child in sorted(item.iterdir()))
        else:
            result[relative] = ("file", mode, item.read_bytes())
    return result


def normalized_tree(source_tree):
    """Literal snapshot model: only regular-file/directory write bits change."""
    return {path: (entry[0], entry[1] & ~0o222, *entry[2:]) if entry[0] in {"file", "directory"} else entry
            for path, entry in copy.deepcopy(source_tree).items()}


class ModelOracle:
    """Capture history across actions; state/payload checks are always explicit."""

    IMMUTABLE = {
        "skill", "version", "grant", "receipt", "verification", "batch-receipt",
        "retention-receipt", "invocation-override-grant", "invocation-override-receipt",
        "invocation-override-revocation", "invocation",
    }

    def __init__(self, testcase, manager):
        self.test = testcase
        self.project_root = Path(manager.project_root)
        self.database = self.project_root / ".skills-auditor-local" / "lifecycle" / "state.sqlite3"
        self.immutable = {}
        self.intents = {}
        self.events = []
        self.paths = {}

    def watch_tree(self, path, expected):
        self.paths[Path(path)] = ("tree", copy.deepcopy(expected))

    def watch_pointer(self, path, *allowed_links):
        if not allowed_links:
            raise ValueError("pointer expectation must name at least one reviewed state")
        self.paths[Path(path)] = ("pointer", tuple(None if link is None else str(link) for link in allowed_links))

    def _read(self):
        records, envelopes = {}, {}
        connection = sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            for row in connection.execute("SELECT * FROM records ORDER BY kind,id"):
                data = json.loads(row["data"])
                envelope = {"kind": row["kind"], "id": row["id"], "revision": row["revision"], "data": data}
                self.test.assertEqual(digest(envelope), row["checksum"], "record checksum")
                records.setdefault(row["kind"], {})[row["id"]] = data
                envelopes[(row["kind"], row["id"])] = envelope
            events = []
            for row in connection.execute("SELECT * FROM events ORDER BY sequence"):
                event = {key: row[key] for key in ("sequence", "stream", "event_type", "actor", "tool", "created_at")}
                event["payload"] = json.loads(row["payload"])
                self.test.assertEqual(digest(event), row["checksum"], "event checksum")
                events.append(event)
        finally:
            connection.close()
        return {"records": records, "events": events, "envelopes": envelopes}

    def check(self):
        snapshot = self._read()
        records, envelopes = snapshot["records"], snapshot["envelopes"]
        for key, old in self.immutable.items():
            self.test.assertEqual(envelopes.get(key), old, "immutable historical fact changed: " + repr(key))
        for kind, identifier in self.intents:
            self.test.assertIn(identifier, records.get(kind, {}), "previous durable intent disappeared")
        self.test.assertEqual(snapshot["events"][:len(self.events)], self.events, "append-only event prefix changed")
        for kind in self.IMMUTABLE:
            for identifier in records.get(kind, {}):
                self.immutable[(kind, identifier)] = copy.deepcopy(envelopes[(kind, identifier)])
        self.events = copy.deepcopy(snapshot["events"])
        for kind in ("transaction", "batch", "retention-transaction"):
            for identifier, tx in records.get(kind, {}).items():
                plan = tx["plan"]
                self.test.assertEqual(plan["project_root"], str(self.project_root))
                self.test.assertEqual(plan["plan_id"], digest({key: value for key, value in plan.items() if key != "plan_id"}))
                self.test.assertEqual(tx["approved_plan_id"], plan["plan_id"])
                fields = ("plan", "approved_plan_id", "actor") if kind == "retention-transaction" else ("plan", "approved_plan_id", "created_at", "actor")
                intent = {key: copy.deepcopy(tx[key]) for key in fields}
                key = (kind, identifier)
                if key in self.intents:
                    self.test.assertEqual(intent, self.intents[key], "approved intent changed")
                self.intents[key] = intent
                if kind == "transaction":
                    self.test.assertEqual(len(tx["steps"]), len(plan["steps"]))
                    for actual, reviewed in zip(tx["steps"], plan["steps"]):
                        self.test.assertEqual({key: value for key, value in actual.items() if key != "state"}, reviewed)
                    if tx["state"] == "completed":
                        receipt = records["receipt"][tx["receipt_id"]]
                        self.test.assertEqual(receipt["transaction_id"], identifier)
                        self.test.assertEqual(receipt["plan_id"], plan["plan_id"])
                        self.test.assertEqual(receipt["installation_id"], plan["installation_id"])
                        self.test.assertEqual(receipt["version_id"], plan["after"]["version_id"])
                        self.test.assertTrue(all(step["state"] == "completed" for step in tx["steps"]))
                    else:
                        self.test.assertIsNone(tx["receipt_id"], "unfinished core work has a success receipt")
                elif kind == "batch":
                    self.test.assertEqual(len(tx["children"]), len(plan["children"]))
                    for index, (reference, child) in enumerate(zip(tx["children"], plan["children"])):
                        expected_id = (child["transaction_id"] if child["kind"] == "compensate" else
                                       "batch-" + digest({"batch_id": identifier, "index": index, "plan_id": child["plan"]["plan_id"]}))
                        self.test.assertEqual(reference["index"], index)
                        self.test.assertEqual(reference["transaction_id"], expected_id)
                    # A receipt remains historical when this batch is itself compensated.
                    if tx["state"] == "completed":
                        self.test.assertIsNotNone(tx["receipt_id"], "completed batch has no success fact")
                    if tx["receipt_id"] is not None:
                        if tx["state"] == "recovery_needed":
                            # A completed inverse may explicitly leave unsupported
                            # work; the original completion remains historical.
                            inverse_id = tx["compensation_batch_id"]
                            self.test.assertIn(inverse_id, records.get("batch", {}), "unfinished batch has a success receipt")
                            inverse = records["batch"][inverse_id]
                            self.test.assertIsNotNone(inverse["receipt_id"])
                            self.test.assertEqual(inverse["plan"]["compensates_batch_id"], identifier)
                            self.test.assertTrue(inverse["plan"]["uncompensated"])
                        else:
                            self.test.assertIn(tx["state"], {"completed", "compensating", "compensated"},
                                               "unfinished batch has a success receipt")
                        for reference, child in zip(tx["children"], plan["children"]):
                            completed_state = "compensated" if child["kind"] == "compensate" else "completed"
                            self.test.assertEqual(reference["state"], completed_state, "batch child has not completed")
                            self.test.assertIn(reference["transaction_id"], records.get("transaction", {}),
                                               "batch completion has no real child intent")
                            core = records["transaction"][reference["transaction_id"]]
                            self.test.assertEqual(core["state"], completed_state)
                            self.test.assertEqual(core["plan"], child["plan"], "batch child completed a different reviewed plan")
                            self.test.assertEqual(core["receipt_id"], reference["receipt_id"])
                        receipt = records["batch-receipt"][tx["receipt_id"]]
                        self.test.assertEqual(receipt["batch_id"], identifier)
                        self.test.assertEqual(receipt["plan_id"], plan["plan_id"])
                        self.test.assertEqual(receipt["children"], tx["children"])
                        self.test.assertEqual(receipt["compensates_batch_id"], plan["compensates_batch_id"])
                        self.test.assertEqual(receipt["uncompensated"], plan["uncompensated"])
                        self.test.assertTrue(any(event["stream"] == identifier and event["event_type"] == "batch_completed"
                                                 and event["payload"] == {"receipt_id": receipt["receipt_id"]}
                                                 for event in snapshot["events"]))
                else:
                    if tx["state"] == "completed":
                        receipt = records["retention-receipt"][tx["receipt_id"]]
                        for field, expected in (("transaction_id", identifier), ("plan_id", plan["plan_id"]),
                                                ("operation", plan["operation"]), ("status", "completed"),
                                                ("objects", plan["objects"]), ("expires", plan["expires"]),
                                                ("permanently_deleted", plan["operation"] == "purge")):
                            self.test.assertEqual(receipt[field], expected)
                        self.test.assertTrue(all(step["state"] == "completed" for step in tx["objects"]))
                        self.test.assertTrue(any(event["sequence"] == tx["completion_event_sequence"]
                                                 and event["stream"] == "retention:" + identifier
                                                 and event["event_type"] == "retention_completed"
                                                 and event["payload"] == {"receipt_id": receipt["receipt_id"], "operation": plan["operation"]}
                                                 for event in snapshot["events"]))
                    else:
                        self.test.assertIsNone(tx["receipt_id"])
        # Reverse proof checks catch orphan success facts which no mutable
        # installation or transaction projection happens to reference.
        for kind, transaction_kind in (("receipt", "transaction"), ("batch-receipt", "batch"),
                                       ("retention-receipt", "retention-transaction")):
            for identifier, receipt in records.get(kind, {}).items():
                reference = receipt["batch_id"] if kind == "batch-receipt" else receipt["transaction_id"]
                self.test.assertIn(reference, records.get(transaction_kind, {}), "receipt has no durable intent")
                tx = records[transaction_kind][reference]
                self.test.assertEqual(receipt["receipt_id"], identifier)
                self.test.assertEqual(tx["receipt_id"], identifier, "receipt is not its intent's completed fact")
                self.test.assertEqual(receipt["status"], "completed")
                self.test.assertEqual(receipt["plan_id"], tx["approved_plan_id"])
                if kind != "batch-receipt":
                    self.test.assertEqual(tx["state"], "completed")
                    self.test.assertEqual(receipt["operation"], tx["plan"]["operation"])
                if kind == "receipt":
                    self.test.assertEqual(receipt["steps"], tx["steps"])
                    self.test.assertTrue(any(event["stream"] == receipt["installation_id"]
                                             and event["event_type"] == "transaction_completed"
                                             and event["payload"] == {"transaction_id": reference, "receipt_id": identifier,
                                                                      "operation": receipt["operation"]}
                                             for event in snapshot["events"]), "core completion event missing")
        for identifier, grant in records.get("grant", {}).items():
            reference = grant["transaction_id"]
            self.test.assertIn(reference, records.get("transaction", {}), "grant has no completed intent")
            tx = records["transaction"][reference]
            plan = tx["plan"]
            self.test.assertEqual(tx["state"], "completed")
            self.test.assertEqual(tx["grant_id"], identifier, "orphan or substituted grant")
            self.test.assertEqual(grant["grant_id"], identifier)
            self.test.assertEqual(grant["plan_id"], plan["plan_id"])
            self.test.assertEqual(TRANSITIONS[plan["operation"]][2], "new")
            for field in ("installation_id", "skill_id", "version_id", "target"):
                self.test.assertEqual(grant[field], plan["after"][field])
            self.test.assertEqual(grant["installation_generation"], plan["before"]["generation"] + 1 if plan["before"] else 1)
        for identifier, installation in records.get("installation", {}).items():
            self.test.assertEqual(installation["installation_id"], identifier)
            version = records["version"][installation["version_id"]]
            self.test.assertIn(installation["skill_id"], records["skill"])
            self.test.assertEqual(version["skill_id"], installation["skill_id"])
            grant_id = installation["authorization"]["grant_id"]
            grant = records["grant"][grant_id]
            self.test.assertEqual(records["authorization"][grant_id], installation["authorization"])
            for field in ("installation_id", "skill_id", "version_id"):
                self.test.assertEqual(grant[field], installation[field], "current grant identity binding")
            if installation["state"] == "active" and installation["authorization"]["state"] == "valid":
                self.test.assertEqual(grant["target"], installation["target"])
                self.test.assertEqual(grant["installation_generation"], installation["generation"])
            receipt = records["receipt"][installation["receipt_id"]]
            tx = records["transaction"][installation["last_transaction_id"]]
            self.test.assertEqual(tx["state"], "completed")
            self.test.assertEqual(tx["receipt_id"], receipt["receipt_id"])
            self.test.assertEqual(receipt["transaction_id"], installation["last_transaction_id"])
            self.test.assertEqual(receipt["plan_id"], tx["plan"]["plan_id"])
            self.test.assertEqual(receipt["grant_id"], grant_id)
            for field in ("installation_id", "skill_id", "version_id"):
                self.test.assertEqual(receipt[field], installation[field])
            for field in ("installation_id", "skill_id", "version_id", "target", "state", "name"):
                self.test.assertEqual(tx["plan"]["after"][field], installation[field])
            previous = tx["plan"]["before"]
            self.test.assertEqual(installation["generation"], previous["generation"] + 1 if previous else 1)
            policy = TRANSITIONS[tx["plan"]["operation"]][2]
            if policy == "new":
                self.test.assertEqual(grant["transaction_id"], tx["transaction_id"])
                self.test.assertEqual(grant["plan_id"], tx["plan"]["plan_id"])
        for path, (kind, expected) in self.paths.items():
            if kind == "tree":
                if expected is None:
                    self.test.assertFalse(os.path.lexists(path), "expected absent tree: " + str(path))
                else:
                    self.test.assertEqual(tree_state(path), expected, "complete tree differs: " + str(path))
            elif not os.path.lexists(path):
                self.test.assertIn(None, expected, "expected owned pointer: " + str(path))
            else:
                self.test.assertTrue(path.is_symlink(), "foreign pointer entry: " + str(path))
                self.test.assertIn(os.readlink(path), expected, "pointer differs from reviewed alternatives")
        return snapshot

    def expect_installation(self, identifier, *, state, authorization=None, version_id=None,
                            target=None, generation=None, grant_id=None, payload=None):
        records = self.check()["records"]
        installation = records["installation"][identifier]
        self.test.assertEqual(installation["installation_id"], identifier)
        for key, expected in (("state", state), ("version_id", version_id), ("generation", generation)):
            if expected is not None:
                self.test.assertEqual(installation[key], expected)
        if authorization is not None:
            self.test.assertEqual(installation["authorization"]["state"], authorization)
        if grant_id is not None:
            self.test.assertEqual(installation["authorization"]["grant_id"], grant_id)
        if target is not None:
            self.test.assertEqual(installation["target"], str(target))
        pointer = Path(installation["target"])
        version = records["version"][installation["version_id"]]
        self.test.assertEqual(version["skill_id"], installation["skill_id"])
        if state == "active":
            self.test.assertTrue(pointer.is_symlink(), "expected active owned pointer")
            self.test.assertEqual(os.readlink(pointer), version["snapshot"]["path"])
            if payload is not None:
                self.test.assertEqual((pointer / "payload").read_bytes(), payload.encode() if isinstance(payload, str) else payload)
        else:
            self.test.assertFalse(os.path.lexists(pointer), "inactive installation unexpectedly exposes a pointer")
        return installation

    def expect_snapshot(self, version_id, present=True, payload=None):
        version = self.check()["records"]["version"][version_id]
        path = Path(version["snapshot"]["path"])
        self.test.assertEqual(path.is_dir(), present)
        if present and payload is not None:
            self.test.assertEqual((path / "payload").read_bytes(), payload.encode() if isinstance(payload, str) else payload)
        return path

    def pending(self):
        records = self.check()["records"]
        result = []
        for kind in ("transaction", "batch", "retention-transaction"):
            for identifier, tx in records.get(kind, {}).items():
                if tx["state"] not in {"completed", "compensated"}:
                    result.append({"kind": kind, "id": identifier, "state": tx["state"],
                                   "plan_id": tx["plan"]["plan_id"], "project_root": str(self.project_root)})
        return result

    def expect_pending(self, kind, identifier, present=True):
        self.test.assertEqual(any(item["kind"] == kind and item["id"] == identifier for item in self.pending()), present)
