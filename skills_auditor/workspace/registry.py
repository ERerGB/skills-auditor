"""Owner-local addresses and source bindings, separate from shared declarations."""
import copy
import os
from pathlib import Path

from ..lifecycle.locking import locked_paths
from .common import PREFIX, atomic_json, digest, fail, fields, identifier, read_json
from .sources import source_binding


def default_root():
    return Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "skills-auditor/workspaces"


class Registry:
    def __init__(self, root=None):
        self.root = Path(root or default_root()).expanduser().resolve()
        self.path = self.root / "registry.json"

    def read(self):
        if not self.path.exists():
            return {"schema_version": PREFIX + "registry/v1", "revision": 0,
                    "environments": {}, "sources": {}, "workspaces": {}}
        value = read_json(self.path)
        fields(value, {"schema_version", "revision", "environments", "sources", "workspaces"})
        if value["schema_version"] != PREFIX + "registry/v1" or type(value["revision"]) is not int:
            fail("workspace_registry", "Unsupported or corrupt workspace registry.")
        for key in ("environments", "sources", "workspaces"):
            if not isinstance(value[key], dict):
                fail("workspace_registry", "Registry collections must be objects.")
        return value

    def put(self, kind, name, value):
        identifier(name)
        if kind == "sources":
            source_binding(value)
        elif kind == "environments":
            fields(value, {"generation", "provider", "state_dir"},
                   {"connection", "python", "python_path", "potology_state", "potology_tools", "potology_target", "pairing"})
            identifier(value["generation"])
            if value["provider"] not in {"local", "ssh", "potology"} or not Path(value["state_dir"]).is_absolute():
                fail("workspace_registry", "Environment needs local/ssh/potology provider and absolute target state directory.")
            if value["provider"] == "ssh" and not value.get("connection"):
                fail("workspace_registry", "SSH provider requires an existing native connection alias.")
            if value["provider"] == "potology" and not all(value.get(k) for k in ("potology_state", "potology_tools", "potology_target", "pairing")):
                fail("workspace_registry", "Potology provider requires its existing owner-local tools, state, target and pairing.")
        elif kind == "workspaces":
            fields(value, {"environment", "path", "declaration"})
            identifier(value["environment"])
            if not all(Path(value[k]).is_absolute() for k in ("path", "declaration")):
                fail("workspace_registry", "Workspace and local declaration bindings must be absolute.")
        else:
            fail("workspace_registry", "Unknown registry collection.")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with locked_paths([self.root / "registry"]):
            current = self.read()
            if kind == "workspaces" and value["environment"] not in current["environments"]:
                fail("workspace_registry", "Register the execution environment first.")
            if kind == "sources" and "environment" in value and value["environment"] not in current["environments"]:
                fail("workspace_registry", "Register the source's owning environment first.")
            previous = current[kind].get(name)
            if previous == value:
                return current
            current[kind][name] = copy.deepcopy(value)
            current["revision"] += 1
            atomic_json(self.path, current)
            self.path.chmod(0o600)
            return current

    def identity(self):
        return digest(self.read())
