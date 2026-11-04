"""Target-local registration, managed plans and durable partial-result journals.

The local account is the trust boundary. The protocol does not evaluate source
instructions or accept arbitrary shell commands. Each installation effect uses
the existing lifecycle engine; there is no cross-target atomicity claim.
"""
import copy
import json
import os
import shutil
from pathlib import Path

from ..cli import _IGNORE_SKILL_SCAN_SEGMENTS, parse_skill_name, skill_alias_for_install_root
from ..environments import BUILTIN_ENVIRONMENTS
from ..lifecycle.common import LifecycleError, canonical_entry, path_components, paths_overlap
from ..lifecycle.engine import Manager
from ..lifecycle.locking import locked_paths
from ..lifecycle.snapshots import inspect_source
from .common import (DEFAULT_PLATFORMS, PREFIX, atomic_json, checked, declaration, digest, fail, fields,
                     fingerprint, identifier, platforms, read_json, seal, utc_now)
from .sources import IGNORED, stage


class Node:
    def __init__(self, state_dir):
        self.root = Path(state_dir).expanduser().resolve()
        if self.root == self.root.parent or self.root == Path.home().resolve():
            fail("workspace_state", "Choose a dedicated persistent state directory.")
        self.binding_path = self.root / "environment.json"

    def _binding(self, request):
        expected = {"environment": request["environment"], "generation": request["generation"],
                    "runtime_fingerprint": fingerprint()}
        if not self.binding_path.exists() or read_json(self.binding_path) != expected:
            fail("workspace_generation", "Environment generation/runtime binding is missing or changed; register explicitly.")
        return expected

    def _workspaces(self):
        path = self.root / "workspaces.json"
        return read_json(path) if path.exists() else {}

    def _workspace(self, name):
        identifier(name)
        value = self._workspaces().get(name)
        if not value:
            fail("workspace_missing", "Workspace is not registered on this environment.")
        path = Path(value["path"])
        if not path.is_dir() or [path.stat().st_dev, path.stat().st_ino] != value["path_identity"]:
            fail("workspace_replaced", "Registered workspace directory was replaced or moved.")
        for platform, enrolled in value["roots"].items():
            native = BUILTIN_ENVIRONMENTS.get(platform).primary_project_root(path)
            try:
                current = native.resolve()
            except (OSError, RuntimeError):
                fail("workspace_replaced", "Registered native Skill root cannot be resolved.")
            if not native.is_dir() or str(current) != enrolled or not current.is_relative_to(path):
                fail("workspace_replaced", "Registered native Skill root was replaced or redirected.", platform=platform)
        return value

    def register(self, request):
        expected = {"environment": request["environment"], "generation": request["generation"],
                    "runtime_fingerprint": fingerprint()}
        if self.binding_path.exists() and read_json(self.binding_path) != expected:
            fail("workspace_generation", "Existing environment binding differs; no implicit re-enrollment.")
        name = identifier(request["workspace"])
        path = Path(request["path"]).resolve()
        if not path.is_dir() or path == path.parent or path == Path.home().resolve() or paths_overlap(path, self.root):
            fail("workspace_path", "Workspace must exist and remain disjoint from runtime state.")
        selected = platforms(request.get("platforms", list(DEFAULT_PLATFORMS)))
        roots = {}
        for platform in selected:
            root = BUILTIN_ENVIRONMENTS.get(platform).primary_project_root(path)
            if not root.resolve().is_relative_to(path):
                fail("workspace_path", "Native project directory resolves outside its workspace.")
            roots[platform] = str(root.resolve())
        workspaces = self._workspaces()
        value = {"path": str(path), "path_identity": [path.stat().st_dev, path.stat().st_ino],
                 "platforms": selected, "roots": roots}
        if name in workspaces and workspaces[name] != value:
            previous = workspaces[name]
            if (previous["path"] != value["path"] or previous["path_identity"] != value["path_identity"]
                    or not set(previous["platforms"]) <= set(selected)
                    or any(roots.get(key) != root for key, root in previous["roots"].items())):
                fail("workspace_registration", "Workspace identity is already bound differently; registration only adds platforms.")
        # Registration is an explicit setup operation, not a plan/apply shortcut.
        # Empty native parent directories contain no activated Skill bytes.
        for root in roots.values():
            Path(root).mkdir(parents=True, exist_ok=True)
        atomic_json(self.binding_path, expected)
        workspaces[name] = value
        atomic_json(self.root / "workspaces.json", workspaces)
        manager = Manager(self.root / "managed")
        manager.repository.close()
        return {"status": "registered", "binding": expected, "workspace": name, **value}

    def _record_key(self, workspace, platform, name):
        return digest([workspace, platform, name])

    def capture(self, request):
        self._binding(request)
        from .sources import capture
        return capture(request["source"])

    def _records(self, manager, workspace):
        return [r["data"] for r in manager.repository.list("workspace_installation")
                if r["data"]["workspace"] == workspace]

    def _roots(self, workspace):
        return [Path(root) for root in self._discovery(workspace)["roots"]]

    def _discovery(self, workspace):
        path = Path(workspace["path"])
        native_roots = (".agents/skills", ".codex/skills", ".cursor/skills", ".claude/skills")
        if "grok" in workspace["platforms"]:
            native_roots += (".grok/skills",)
        roots = [path / p for p in native_roots]
        roots += [Path.home() / p for p in native_roots if p != ".grok/skills"]
        project_directories = [path]
        # Parent repository roots participate in native discovery too.
        parent = path
        while not (parent / ".git").exists() and parent.parent != parent:
            parent = parent.parent
        if (parent / ".git").exists():
            current = path.parent
            while current != parent.parent and current != current.parent:
                roots += [current / p for p in native_roots]
                project_directories.append(current)
                if current == parent:
                    break
                current = current.parent
        observations = []
        if "grok" in workspace["platforms"]:
            from .grok import configuration
            try:
                global_root = BUILTIN_ENVIRONMENTS.get("grok").primary_global_root(Path.home())
            except ValueError as error:
                fail("workspace_grok_config", str(error))
            roots.append(global_root)
            selected = configuration(global_root.parent, project_directories)
            roots.extend(selected["roots"])
            observations = selected["configurations"]
        return {"roots": list(dict.fromkeys(map(str, roots))), "configurations": observations}

    def _conflicts(self, workspace, desired, *, inventory=False):
        findings = []
        selected_targets = {str(canonical_entry(d["target"])) for d in desired}
        wanted = {d["name"] for d in desired}
        for root in self._roots(workspace):
            if root.is_symlink() and not root.exists():
                findings.append({"code": "workspace_broken_root", "path": str(root)})
                continue
            if not root.is_dir():
                continue
            # Follow visible pack links with an ancestor set, retaining logical
            # entry paths so two mutable copies never collapse by alias alone.
            def visible(folder, ancestors):
                try:
                    resolved = folder.resolve(strict=True)
                except (OSError, RuntimeError):
                    if folder.name in wanted:
                        findings.append({"code": "workspace_broken_shadow", "path": str(folder), "name": folder.name})
                    return
                if resolved in ancestors or not folder.is_dir():
                    return
                if (folder / "SKILL.md").is_file():
                    yield folder
                for item in folder.iterdir():
                    if item.name not in IGNORED | _IGNORE_SKILL_SCAN_SEGMENTS and not item.name.startswith(".") and (item.is_dir() or item.is_symlink()):
                        yield from visible(item, ancestors | {resolved})

            for entry in visible(root, set()):
                if str(canonical_entry(entry)) in selected_targets:
                    continue
                md = entry / "SKILL.md"
                name = skill_alias_for_install_root(parse_skill_name(md))
                if inventory:
                    findings.append({"name": name, "path": str(entry), "kind": "symlink" if entry.is_symlink() else "directory",
                                     "verification": "unverified", "migration": "explicit_review_required"})
                elif name in wanted:
                    # Equal bytes today do not make two independently addressed
                    # pointers one installation. Parent-directory aliases already
                    # collapse through canonical_entry above.
                    findings.append({"code": "workspace_shadow_conflict", "path": str(entry), "name": name})
        return findings

    def plan(self, request):
        binding = self._binding(request)
        workspace_id = request["workspace"]
        workspace = self._workspace(workspace_id)
        declared = declaration(request["declaration"])
        selected_platforms = declared.get("platforms", list(workspace["platforms"]))
        if not set(selected_platforms) <= set(workspace["platforms"]):
            fail("workspace_registration", "Register all requested agent roots before planning.")
        packets = request["packets"]
        needed = sum(len(record["data"]) * 3 // 4 + 4096 for p in packets.values() for record in p["files"])
        if shutil.disk_usage(self.root).free < needed * 2 + 1024 * 1024:
            fail("workspace_capacity", "Insufficient target capacity before candidate staging.", required=needed * 2 + 1024 * 1024)
        selected = {}
        candidates = {}
        for source in declared["baseline"]:
            identifier(source)
            packet = packets[source]
            candidates[source] = stage(packet, self.root / "candidates")
            for name, entry in packet["entries"].items():
                if name in selected and selected[name] != {"source": source, "entry": entry}:
                    if name not in declared.get("overrides", {}):
                        fail("workspace_source_conflict", "Baseline has different sources for one Skill; declare an override.", name=name)
                selected[name] = {"source": source, "entry": entry}
        for name, choice in declared.get("overrides", {}).items():
            if choice is None:
                selected.pop(name, None)
                continue
            source = choice["source"]
            if source not in packets or packets[source]["entries"].get(name) != choice["entry"]:
                fail("workspace_override", "Override must select a declared source entrypoint.", name=name)
            if source not in candidates:
                candidates[source] = stage(packets[source], self.root / "candidates")
            selected[name] = choice
        desired = []
        target_keys = set()
        for platform in selected_platforms:
            for name, choice in sorted(selected.items()):
                packet = packets[choice["source"]]
                if platform not in packet["platforms"]:
                    fail("workspace_platform", "Selected Skill is incompatible with a requested agent platform.", name=name, platform=platform)
                target = str(Path(workspace["roots"][platform]) / name)
                key = path_components(target)
                if key in target_keys:
                    fail("workspace_source_conflict", "Effective Skill targets collide on a supported filesystem.", target=target)
                target_keys.add(key)
                desired.append({"name": name, "platform": platform, **choice, "target": target})
        findings = self._conflicts(workspace, desired)
        manager = Manager(self.root / "managed")
        children, actions, expectations = [], [], []
        try:
            records = self._records(manager, workspace_id)
            indexed = {(r["platform"], r["name"]): r for r in records}
            for item in desired:
                name, platform = item["name"], item["platform"]
                record = indexed.get((platform, name))
                source = candidates[item["source"]]
                packet = packets[item["source"]]
                skill_id = digest({"workspace_skill": name})
                identity = manager.repository.get("skill", skill_id)
                if not identity:
                    manager.repository.put("skill", skill_id, {"skill_id": skill_id, "name": name, "created_at": utc_now()})
                before = manager.get_installation(record["installation_id"]) if record else None
                if before:
                    version = manager.repository.get("version", before["version_id"])["data"]
                    same = (version["snapshot"]["source_tree_sha256"] == packet["inspection"]["source_tree_sha256"]
                            and version.get("entrypoint", ".") == item["entry"])
                    if not manager.verify(before["installation_id"])["valid"]:
                        findings.append({"code": "workspace_active_invalid", "name": name, "platform": platform})
                        continue
                    expectations.append(copy.deepcopy(before))
                    if before["state"] != "active":
                        if not same:
                            findings.append({"code": "workspace_disabled_update", "name": name, "platform": platform})
                            continue
                        child = manager.plan("enable", installation_id=before["installation_id"])
                    elif same:
                        actions.append({**item, "action": "noop", "installation_id": before["installation_id"]})
                        continue
                    else:
                        child = manager.plan("update", installation_id=before["installation_id"], source=source, entrypoint=item["entry"])
                else:
                    target = Path(item["target"])
                    if target.exists() or target.is_symlink():
                        legacy = self._legacy_source(request.get("legacy_receipts", []), target, item["entry"], packet)
                        if legacy is None:
                            findings.append({"code": "workspace_unmanaged_target", "path": str(target), "name": name})
                            continue
                        original, receipt = legacy
                        child = manager.plan("migrate", source=original, entrypoint=item["entry"], target=target,
                                             skill_id=skill_id, name=name, legacy_receipt=receipt)
                    else:
                        child = manager.plan("install", source=source, entrypoint=item["entry"], target=target, skill_id=skill_id, name=name)
                children.append({"plan": child, "workspace": workspace_id, "name": name, "platform": platform})
                actions.append({**item, "action": child["operation"], "installation_id": child["installation_id"]})
            wanted = {(v["platform"], v["name"]) for v in desired}
            for record in records:
                if (record["platform"], record["name"]) in wanted:
                    continue
                before = manager.get_installation(record["installation_id"])
                if before["state"] == "active":
                    child = manager.plan("disable", installation_id=record["installation_id"])
                    children.append({"plan": child, **{k: record[k] for k in ("workspace", "name", "platform")}})
                    actions.append({"name": record["name"], "platform": record["platform"], "action": "disable",
                                    "target": before["target"], "installation_id": before["installation_id"]})
            result = seal({"schema_version": PREFIX + "node-plan/v1", "binding": binding,
                "workspace": workspace_id, "workspace_binding": workspace, "declaration": declared,
                "created_at": utc_now(), "actions": actions, "children": children,
                "expectations": expectations, "desired": desired, "findings": findings,
                **({"discovery": self._discovery(workspace)} if "grok" in workspace["platforms"] else {}),
                "status": "blocked" if findings else "planned",
                "summary": {"changes": len(children), "unchanged": sum(a["action"] == "noop" for a in actions)}}, "plan_id")
            atomic_json(self.root / "plans" / (result["plan_id"] + ".json"), result)
            return result
        finally:
            manager.repository.close()

    def _legacy_source(self, receipts, target, entry, packet):
        from ..integration import verify_receipt
        for receipt in receipts:
            if verify_receipt(receipt)["status"] != "passed":
                fail("workspace_legacy", "A supplied legacy receipt is not currently clean.")
            for result in receipt["results"]:
                if Path(result["root"]) / result["name"] != target:
                    continue
                old_entry = Path(result["expected_target"])
                original = old_entry
                if entry != ".":
                    for _ in Path(entry).parts:
                        original = original.parent
                if original / entry != old_entry or inspect_source(original) != packet["inspection"]:
                    fail("workspace_legacy_layout", "Legacy package layout differs from reviewed package; retain it until an explicit migration is prepared.")
                return original, receipt
        return None

    def _check_plan(self, request, *, require_approval=True):
        plan = checked(request["plan"], "plan_id", "node-plan")
        self._binding(request)
        if plan["binding"] != read_json(self.binding_path) or plan["workspace_binding"] != self._workspace(plan["workspace"]):
            fail("workspace_generation", "Plan is bound to a different environment/workspace generation.")
        if "discovery" in plan and plan["discovery"] != self._discovery(plan["workspace_binding"]):
            fail("workspace_stale", "Native Skill roots or Grok configuration changed after review.")
        if plan["status"] != "planned" or plan["findings"]:
            fail("workspace_blocked", "Resolve all plan findings before activation.", findings=plan["findings"])
        saved = self.root / "plans" / (plan["plan_id"] + ".json")
        if not saved.is_file() or read_json(saved) != plan:
            fail("workspace_contract", "Target-local saved plan differs or is missing.")
        if require_approval and request.get("approve_plan_id") != plan["plan_id"]:
            fail("workspace_approval", "Apply requires the exact reviewed node plan ID.")
        return plan

    def apply(self, request):
        plan = self._check_plan(request)
        manager = Manager(self.root / "managed")
        journal_path = self.root / "transactions" / (plan["plan_id"] + ".json")
        try:
            if journal_path.exists():
                journal = read_json(journal_path)
                if journal["status"] == "completed":
                    for child, result in zip(plan["children"], journal["results"]):
                        manager.apply(child["plan"], approve_plan_id=child["plan"]["plan_id"], transaction_id=result["transaction_id"])
                    for expected in plan["expectations"]:
                        if not any(c["plan"]["installation_id"] == expected["installation_id"] for c in plan["children"]):
                            if manager.get_installation(expected["installation_id"]) != expected:
                                fail("workspace_stale", "Completed no-change plan has been superseded.")
                    if not self.verify({**request, "workspace": plan["workspace"]})["valid"]:
                        fail("workspace_active_invalid", "Completed apply does not override changed active state.")
                    return journal
                fail("workspace_recovery_required", "Existing transaction needs explicit recovery.", plan_id=plan["plan_id"])
            if self._conflicts(plan["workspace_binding"], plan["desired"]):
                fail("workspace_stale", "Native discovery roots changed after review.")
            for expected in plan["expectations"]:
                if (manager.get_installation(expected["installation_id"]) != expected
                        or not manager.verify(expected["installation_id"])["valid"]):
                    fail("workspace_stale", "An unchanged installation changed after review.")
            # Validate all child preconditions before the first pointer effect.
            for child in plan["children"]:
                manager._validate_plan(child["plan"])
                with manager._locks(child["plan"]):
                    manager._preconditions(child["plan"])
            payloads, metadata = {}, 0
            for child in plan["children"]:
                core = child["plan"]
                # Core validation and source preconditions above establish this
                # identity. Several aliases/platforms share one snapshot tree,
                # while each still writes its own plan, journal and receipt.
                metadata += len(json.dumps(core).encode()) * 3 + 16 * 1024
                if core["source"]:
                    key = core["version"]["snapshot"]["snapshot_tree_sha256"]
                    if key not in payloads:
                        payloads[key] = 4096 + sum(4096 + (p.stat().st_size if p.is_file() else 0)
                                                  for p in Path(core["source"]).rglob("*"))
            required = sum(payloads.values()) * 2 + metadata + 1024 * 1024
            if shutil.disk_usage(self.root).free < required:
                fail("workspace_capacity", "Target needs space for shared snapshots and per-installation transaction evidence.", required=required)
            journal = {"schema_version": PREFIX + "transaction/v1", "plan_id": plan["plan_id"],
                       "workspace": plan["workspace"], "status": "running", "results": [], "created_at": utc_now()}
            atomic_json(journal_path, journal)
            return self._execute(plan, journal, journal_path, manager)
        finally:
            manager.repository.close()

    def _execute(self, plan, journal, path, manager):
        try:
            # Both first application and explicit resume pass this boundary.
            # Completed siblings remain selected owned targets; a new shadow
            # must stop before any pending installation advances.
            if self._conflicts(plan["workspace_binding"], plan["desired"]):
                fail("workspace_stale", "Native discovery roots changed before pending activation.")
            for expected in plan["expectations"]:
                if not any(c["plan"]["installation_id"] == expected["installation_id"] for c in plan["children"]):
                    if (manager.get_installation(expected["installation_id"]) != expected
                            or not manager.verify(expected["installation_id"])["valid"]):
                        fail("workspace_stale", "Unchanged installation drifted before recovery.")
            for index, child in enumerate(plan["children"]):
                if index < len(journal["results"]):
                    manager.apply(child["plan"], approve_plan_id=child["plan"]["plan_id"],
                                  transaction_id=journal["results"][index]["transaction_id"])
                    continue
                # Intent is durable before Manager can publish a pointer.
                transaction_id = "workspace-" + digest([plan["plan_id"], index])[:48]
                journal["in_flight"] = {"index": index, "transaction_id": transaction_id}
                atomic_json(path, journal)
                existing = manager.repository.get("transaction", transaction_id)
                if existing and existing["data"]["state"] != "completed":
                    receipt = manager.recover(transaction_id, mode="resume", approve_plan_id=child["plan"]["plan_id"])
                else:
                    receipt = manager.apply(child["plan"], approve_plan_id=child["plan"]["plan_id"], transaction_id=transaction_id)
                installation = manager.get_installation(child["plan"]["installation_id"])
                mapping = {k: child[k] for k in ("workspace", "name", "platform")}
                mapping["installation_id"] = installation["installation_id"]
                mapping_key = self._record_key(mapping["workspace"], mapping["platform"], mapping["name"])
                previous_mapping = manager.repository.get("workspace_installation", mapping_key)
                if previous_mapping is None or previous_mapping["data"] != mapping:
                    manager.repository.put("workspace_installation", mapping_key, mapping,
                                           expected_revision=previous_mapping["revision"] if previous_mapping else 0)
                if not manager.verify(installation["installation_id"])["valid"]:
                    fail("workspace_verification", "Managed installation did not verify.")
                journal["results"].append({"installation_id": installation["installation_id"], "receipt_id": receipt.get("receipt_id"), "transaction_id": transaction_id})
                journal.pop("in_flight", None)
                atomic_json(path, journal)
            journal["status"] = "completed"
            journal["completed_at"] = utc_now()
            if self._conflicts(plan["workspace_binding"], plan["desired"]):
                fail("workspace_shadow_conflict", "Discovery roots changed during activation; inspect before continuing.")
            view = {"workspace": plan["workspace"], "installations": {}}
            for record in self._records(manager, plan["workspace"]):
                installation = manager.get_installation(record["installation_id"])
                if installation["state"] == "active":
                    view["installations"][installation["installation_id"]] = {
                        key: installation[key] for key in ("version_id", "generation", "target")}
            previous_view = manager.repository.get("workspace_view", plan["workspace"])
            manager.repository.put("workspace_view", plan["workspace"], view,
                                   expected_revision=previous_view["revision"] if previous_view else 0)
            atomic_json(path, journal)
            return journal
        except (LifecycleError, OSError) as error:
            journal["status"] = "partial"
            journal["error"] = {"code": getattr(error, "code", "workspace_io"), "message": str(error)[:1000]}
            atomic_json(path, journal)
            raise

    def recover(self, request):
        plan = self._check_plan(request, require_approval=request.get("mode", "inspect") != "inspect")
        path = self.root / "transactions" / (plan["plan_id"] + ".json")
        if not path.exists():
            manager = Manager(self.root / "managed", create=False)
            try:
                existing = [manager.repository.get("transaction", "workspace-" + digest([plan["plan_id"], index])[:48]) for index in range(len(plan["children"]))]
            finally:
                manager.repository.close()
            if any(existing):
                fail("workspace_recovery", "Target journal is missing but child effects exist; preserve state for investigation.")
            if request.get("mode", "inspect") == "inspect":
                return {"status": "not_started", "plan_id": plan["plan_id"]}
            return self.apply(request)
        journal = read_json(path)
        if request.get("mode", "inspect") == "inspect":
            return journal
        if request["mode"] != "resume":
            fail("workspace_recovery", "Use explicit per-installation compensation for uncertain effects.")
        manager = Manager(self.root / "managed")
        try:
            if journal["status"] == "completed":
                return self.apply(request)
            return self._execute(plan, journal, path, manager)
        finally:
            manager.repository.close()

    def verify(self, request):
        binding = self._binding(request)
        workspace = self._workspace(request["workspace"])
        manager = Manager(self.root / "managed", create=False)
        try:
            results = []
            records = self._records(manager, request["workspace"])
            desired = []
            for record in records:
                installation = manager.get_installation(record["installation_id"])
                verification = manager.verify(installation["installation_id"])
                readable = None
                if installation["state"] == "active" and verification["valid"]:
                    md = Path(installation["target"]) / "SKILL.md"
                    readable = {"bytes": len(md.read_bytes()), "canonical": str(md.resolve())}
                    desired.append({"name": record["name"], "target": installation["target"]})
                results.append({**record, "valid": verification["valid"], "state": installation["state"],
                                "version_id": installation["version_id"], "readable": readable})
            conflicts = self._conflicts(workspace, desired)
            view = manager.repository.get("workspace_view", request["workspace"])
            active = {i["installation_id"]: {key: i[key] for key in ("version_id", "generation", "target")}
                      for r in records for i in [manager.get_installation(r["installation_id"]) ] if i["state"] == "active"}
            view_matches = bool(view) and view["data"]["installations"] == active
            native = {}
            if request.get("native_platforms"):
                if request["native_platforms"] != ["grok"] or "grok" not in workspace["platforms"]:
                    fail("workspace_platform", "Register Grok Build before requesting its native verification.")
                from .grok import inspect_native
                expected = [{"name": r["name"], "target": manager.get_installation(r["installation_id"])["target"]}
                            for r in records if r["platform"] == "grok"
                            and manager.get_installation(r["installation_id"])["state"] == "active"]
                native["grok"] = inspect_native(workspace["path"], expected)
            return {"binding": binding, "workspace": request["workspace"], "results": results,
                    "valid": view_matches and all(r["valid"] for r in results if r["state"] == "active") and not conflicts
                             and all(r["valid"] for r in native.values()),
                    "findings": conflicts, "native_discovery": native or "unverified", "observed_at": utc_now()}
        finally:
            manager.repository.close()

    def rollback(self, request):
        self._binding(request)
        old = checked(request["plan"], "plan_id", "node-plan")
        workspace = self._workspace(old["workspace"])
        manager = Manager(self.root / "managed")
        try:
            children, actions = [], []
            by_id = {r["installation_id"]: r for r in self._records(manager, old["workspace"])}
            for child in reversed(old["children"]):
                previous = child["plan"]["before"]
                current = manager.get_installation(child["plan"]["installation_id"])
                if previous is None:
                    inverse = manager.plan("disable", installation_id=current["installation_id"])
                elif previous["state"] != "active":
                    if current["state"] != "active" or previous["version_id"] != current["version_id"]:
                        fail("workspace_rollback", "This inverse needs an explicit retained-version selection.")
                    inverse = manager.plan("disable", installation_id=current["installation_id"])
                elif current["state"] == "active":
                    inverse = manager.plan("rollback", installation_id=current["installation_id"], version_id=previous["version_id"])
                else:
                    inverse = manager.plan("enable", installation_id=current["installation_id"])
                mapping = by_id[current["installation_id"]]
                children.append({"plan": inverse, **{k: mapping[k] for k in ("workspace", "name", "platform")}})
                actions.append({"name": mapping["name"], "platform": mapping["platform"], "target": current["target"], "action": inverse["operation"]})
            result = seal({"schema_version": PREFIX + "node-plan/v1", "binding": read_json(self.binding_path),
                "workspace": old["workspace"], "workspace_binding": workspace, "declaration": old["declaration"],
                "created_at": utc_now(), "children": children, "actions": actions, "desired": [],
                **({"discovery": self._discovery(workspace)} if "grok" in workspace["platforms"] else {}),
                "expectations": [], "findings": [], "status": "planned",
                "summary": {"changes": len(children), "unchanged": 0}}, "plan_id")
            atomic_json(self.root / "plans" / (result["plan_id"] + ".json"), result)
            return result
        finally:
            manager.repository.close()

    def inspect(self, request):
        binding = self._binding(request)
        manager = Manager(self.root / "managed", create=False)
        try:
            installations = [{**record["data"], **{key: manager.get_installation(record["data"]["installation_id"])[key]
                              for key in ("state", "version_id", "target", "authorization")}}
                             for record in manager.repository.list("workspace_installation")]
        finally:
            manager.repository.close()
        workspaces = self._workspaces()
        legacy = {name: self._conflicts(value, [{"name": i["name"], "target": i["target"]}
                  for i in installations if i["workspace"] == name], inventory=True) for name, value in workspaces.items()}
        return {"binding": binding, "workspaces": workspaces, "free_bytes": shutil.disk_usage(self.root).free,
                "installations": installations,
                "legacy_installations": legacy,
                "clients": {"codex": bool(shutil.which("codex")), "cursor": bool(shutil.which("cursor-agent")),
                            "claude-code": bool(shutil.which("claude")), "grok": bool(shutil.which("grok"))}}

    def dispatch(self, request):
        fields(request, {"method", "environment", "generation"},
               {"state_dir", "workspace", "path", "platforms", "declaration", "packets", "plan", "approve_plan_id", "mode", "legacy_receipts", "native_platforms", "source"})
        identifier(request["environment"])
        identifier(request["generation"])
        methods = {"register": self.register, "inspect": self.inspect, "plan": self.plan, "apply": self.apply,
                   "verify": self.verify, "recover": self.recover, "rollback": self.rollback, "capture": self.capture}
        if request["method"] not in methods:
            fail("workspace_method", "Unknown target operation; shell execution is not part of this protocol.")
        if request["method"] in {"inspect", "verify", "capture"}:
            return methods[request["method"]](request)
        if request["method"] == "register":
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with locked_paths([self.root / "coordinator"]):
            return methods[request["method"]](request)
