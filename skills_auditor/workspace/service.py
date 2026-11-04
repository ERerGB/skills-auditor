"""On-demand orchestration with exact approval and per-environment results."""
import json
from pathlib import Path

from ..lifecycle.common import LifecycleError
from ..lifecycle.locking import locked_paths
from .common import DEFAULT_PLATFORMS, MAX_BYTES, PREFIX, atomic_json, checked, declaration, digest, fail, identifier, read_json, seal, utc_now
from .providers import provider
from .registry import Registry
from .sources import capture


class WorkspaceService:
    def __init__(self, registry_root=None):
        self.registry = Registry(registry_root)

    def _capture(self, binding, state):
        owner = binding.get("environment")
        if owner is None:
            return capture(binding)
        if owner not in state["environments"]:
            fail("workspace_registry", "Source owner is not a registered environment.", environment=owner)
        source = {k: v for k, v in binding.items() if k != "environment"}
        return provider(owner, state["environments"][owner]).call("capture", source=source)

    def register(self, name, environment, path, declaration_path):
        identifier(name)
        state = self.registry.read()
        if environment not in state["environments"]:
            fail("workspace_registry", "Register an environment before its workspaces.")
        declared = declaration(read_json(declaration_path))
        result = provider(environment, state["environments"][environment]).call("register", workspace=name,
            path=str(path), platforms=declared.get("platforms", list(DEFAULT_PLATFORMS)))
        self.registry.put("workspaces", name, {"environment": environment, "path": str(path),
                          "declaration": str(Path(declaration_path).resolve())})
        return result

    def status(self):
        state = self.registry.read()
        result = {"schema_version": PREFIX + "status/v1", "environments": {},
                  "workspaces": state["workspaces"], "observed_at": utc_now()}
        for name, config in state["environments"].items():
            try:
                result["environments"][name] = {"status": "observed", **provider(name, config).call("inspect")}
            except LifecycleError as error:
                result["environments"][name] = {"status": "unknown", "code": error.code, "message": error.message}
        return result

    def plan(self, workspaces=(), *, legacy_receipts=None, release=None):
        state = self.registry.read()
        selected = list(workspaces) or sorted(state["workspaces"])
        if not selected or len(selected) != len(set(selected)):
            fail("workspace_input", "Select distinct registered workspaces.")
        if any(name not in state["workspaces"] for name in selected):
            fail("workspace_missing", "Requested workspace is not registered.")
        declarations, source_ids, source_specs = {}, set(), {}
        for name in selected:
            value = declaration(read_json(state["workspaces"][name]["declaration"]))
            declarations[name] = value
            for source, spec in value.get("sources", {}).items():
                if source in source_specs and source_specs[source] != spec:
                    fail("workspace_source_conflict", "Workspaces declare different releases for the same source ID; use distinct IDs.", source=source)
                source_specs[source] = spec
            source_ids.update(value["baseline"])
            source_ids.update(v["source"] for v in value.get("overrides", {}).values() if v is not None)
        if release is not None:
            checked(release, "release_id", "release")
            packets = release["sources"]
            if source_ids - set(packets):
                fail("workspace_source", "Retained release does not contain all selected sources.")
            packets = {k: packets[k] for k in sorted(source_ids)}
        elif source_ids - set(state["sources"]):
            fail("workspace_source", "A declared source has no owner-local binding.", sources=sorted(source_ids - set(state["sources"])))
        else:
            packets = {name: self._capture({**state["sources"][name], **source_specs.get(name, {})}, state) for name in sorted(source_ids)}
        retained = seal({"schema_version": PREFIX + "release/v1", "sources": packets}, "release_id")
        if len(json.dumps(retained).encode()) > MAX_BYTES:
            fail("workspace_capacity", "Aggregate release exceeds the portable transport bound; split the plan.")
        atomic_json(self.registry.root / "releases" / (retained["release_id"] + ".json"), retained)
        targets = []
        for name in selected:
            config = state["workspaces"][name]
            environment = config["environment"]
            try:
                value = provider(environment, state["environments"][environment]).call("plan", workspace=name,
                    declaration=declarations[name], packets=packets,
                    legacy_receipts=(legacy_receipts or {}).get(environment, []))
                targets.append({"workspace": name, "environment": environment, "status": value["status"], "plan": value})
            except LifecycleError as error:
                targets.append({"workspace": name, "environment": environment, "status": "blocked",
                                "error": {"code": error.code, "message": error.message, "details": error.details}})
        return self._save_plan({"schema_version": PREFIX + "plan/v1", "registry_digest": digest(state),
            "created_at": utc_now(), "operation": "restore" if release else "deploy", "declarations": declarations,
            "release_id": retained["release_id"],
            "source_specs": {name: source_specs[name] for name in sorted(source_ids) if name in source_specs},
            "source_owners": {} if release else {name: {
                "environment": state["sources"][name].get("environment", "controller"),
                "generation": state["environments"][state["sources"][name]["environment"]]["generation"]
                    if "environment" in state["sources"][name] else None,
                "root": state["sources"][name]["root"]} for name in sorted(source_ids)},
            "sources": {key: {"inspection": p["inspection"], "origin": p["origin"]} for key, p in packets.items()},
            "targets": targets})

    def _save_plan(self, value):
        value["status"] = "blocked" if any(t["status"] == "blocked" for t in value["targets"]) else "planned"
        value["summary"] = {"workspaces": len(value["targets"]),
            "changes": sum(t.get("plan", {}).get("summary", {}).get("changes", 0) for t in value["targets"]),
            "unchanged": sum(t.get("plan", {}).get("summary", {}).get("unchanged", 0) for t in value["targets"]),
            "blocked": sum(t["status"] == "blocked" for t in value["targets"])}
        seal(value, "plan_id")
        atomic_json(self.registry.root / "plans" / (value["plan_id"] + ".json"), value)
        return value

    def _plan(self, value, approve_plan_id):
        checked(value, "plan_id", "plan")
        if approve_plan_id != value["plan_id"]:
            fail("workspace_approval", "Use the exact reviewed workspace plan ID.")
        if value["status"] != "planned":
            fail("workspace_blocked", "Resolve blocked workspaces before applying this plan.")
        state = self.registry.read()
        if digest(state) != value["registry_digest"]:
            fail("workspace_stale", "Environment, source or workspace bindings changed after review.")
        for name, declared in value["declarations"].items():
            if declaration(read_json(state["workspaces"][name]["declaration"])) != declared:
                fail("workspace_stale", "Workspace declaration changed after review.", workspace=name)
        return state

    def apply(self, plan, *, approve_plan_id):
        state = self._plan(plan, approve_plan_id)
        retained = checked(read_json(self.registry.root / "releases" / (plan["release_id"] + ".json")), "release_id", "release") if plan.get("release_id") else None
        for name, expected in plan["sources"].items():
            current = retained["sources"][name] if plan["operation"] == "restore" else self._capture({**state["sources"][name], **plan.get("source_specs", {}).get(name, {})}, state)
            if {"inspection": current["inspection"], "origin": current["origin"]} != expected:
                fail("workspace_stale", "Selected source changed after review; no target effects started.", source=name)
        path = self.registry.root / "transactions" / (plan["plan_id"] + ".json")
        with locked_paths([self.registry.root / "coordinator"]):
            if path.exists():
                result = read_json(path)
                if result["status"] != "completed":
                    fail("workspace_recovery_required", "Inspect/recover the recorded operation before retrying.")
                for target in plan["targets"]:
                    provider(target["environment"], state["environments"][target["environment"]]).call("apply",
                        plan=target["plan"], approve_plan_id=target["plan"]["plan_id"])
                if not self.verify([t["workspace"] for t in plan["targets"]])["valid"]:
                    fail("workspace_active_invalid", "Completed receipt is historical; current targets did not verify.")
                return result
            result = {"schema_version": PREFIX + "execution/v1", "plan_id": plan["plan_id"],
                      "created_at": utc_now(), "status": "running", "targets": []}
            atomic_json(path, result)
            for target in plan["targets"]:
                environment = target["environment"]
                result["in_flight"] = {"environment": environment, "workspace": target["workspace"]}
                atomic_json(path, result)
                try:
                    value = provider(environment, state["environments"][environment]).call("apply",
                        plan=target["plan"], approve_plan_id=target["plan"]["plan_id"])
                    result["targets"].append({"environment": environment, "workspace": target["workspace"], "status": value["status"], "result": value})
                except LifecycleError as error:
                    result["targets"].append({"environment": environment, "workspace": target["workspace"],
                        "status": "uncertain" if error.code == "workspace_uncertain" else "failed",
                        "code": error.code, "message": error.message, "details": error.details})
                result.pop("in_flight", None)
                atomic_json(path, result)
            result["status"] = "completed" if all(t["status"] == "completed" for t in result["targets"]) else "partial"
            atomic_json(path, result)
            return result

    def verify(self, workspaces=(), *, native_platforms=()):
        if any(name != "grok" for name in native_platforms) or len(native_platforms) != len(set(native_platforms)):
            fail("workspace_platform", "Explicit native verification currently supports Grok Build only.")
        state = self.registry.read()
        results = []
        for name in list(workspaces) or sorted(state["workspaces"]):
            if name not in state["workspaces"]:
                fail("workspace_missing", "Workspace is not registered.")
            environment = state["workspaces"][name]["environment"]
            try:
                options = {"native_platforms": list(native_platforms)} if native_platforms else {}
                value = provider(environment, state["environments"][environment]).call("verify", workspace=name, **options)
            except LifecycleError as error:
                value = {"workspace": name, "valid": False, "status": "unknown", "code": error.code, "message": error.message}
            results.append({"environment": environment, **value})
        return {"schema_version": PREFIX + "verification/v1", "valid": bool(results) and all(t["valid"] for t in results), "targets": results}

    def rollback(self, plan):
        checked(plan, "plan_id", "plan")
        state = self.registry.read()
        journal = self.registry.root / "transactions" / (plan["plan_id"] + ".json")
        if not journal.is_file() or read_json(journal)["status"] != "completed":
            fail("workspace_recovery_required", "Rollback requires a completed deployment; inspect partial operations first.")
        targets = []
        for target in reversed(plan["targets"]):
            environment = target["environment"]
            value = provider(environment, state["environments"][environment]).call("rollback", plan=target["plan"])
            targets.append({"workspace": target["workspace"], "environment": environment, "status": value["status"], "plan": value})
        return self._save_plan({"schema_version": PREFIX + "plan/v1", "registry_digest": digest(state), "created_at": utc_now(),
            "operation": "rollback", "declarations": {n: declaration(read_json(state["workspaces"][n]["declaration"])) for n in plan["declarations"]},
            "sources": {}, "targets": targets})

    def export_release(self, plan, destination):
        checked(plan, "plan_id", "plan")
        if not plan.get("release_id"):
            fail("workspace_release", "This plan does not name a deployable source release.")
        value = checked(read_json(self.registry.root / "releases" / (plan["release_id"] + ".json")), "release_id", "release")
        atomic_json(destination, value)
        return {"release_id": value["release_id"], "path": str(Path(destination).resolve()), "contains_approvals": False}

    def recover(self, plan, *, mode="inspect", approve_plan_id=None):
        if mode == "resume":
            with locked_paths([self.registry.root / "coordinator"]):
                return self._recover(plan, mode=mode, approve_plan_id=approve_plan_id)
        return self._recover(plan, mode=mode, approve_plan_id=approve_plan_id)

    def _recover(self, plan, *, mode, approve_plan_id):
        checked(plan, "plan_id", "plan")
        if mode not in {"inspect", "resume"}:
            fail("workspace_recovery", "Recovery supports inspect and explicitly approved resume.")
        state = self.registry.read()
        if mode == "resume":
            self._plan(plan, approve_plan_id)
        path = self.registry.root / "transactions" / (plan["plan_id"] + ".json")
        if not path.exists():
            fail("workspace_recovery", "No controller operation exists for this plan.")
        journal = read_json(path)
        observations = []
        for target in plan["targets"]:
            environment = target["environment"]
            try:
                value = provider(environment, state["environments"][environment]).call("recover", plan=target["plan"],
                    mode=mode, approve_plan_id=target["plan"]["plan_id"] if mode == "resume" else None)
                observations.append({"environment": environment, "workspace": target["workspace"], "status": value["status"], "result": value})
            except LifecycleError as error:
                observations.append({"environment": environment, "workspace": target["workspace"], "status": "unknown", "code": error.code})
        if mode == "resume":
            journal["targets"] = observations
            journal.pop("in_flight", None)
            journal["status"] = "completed" if all(t["status"] == "completed" for t in observations) else "partial"
            atomic_json(path, journal)
        return {"status": journal["status"], "journal": journal, "observations": observations}
