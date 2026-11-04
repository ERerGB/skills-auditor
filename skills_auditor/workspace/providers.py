"""Local, existing SSH and optional Potology owner-local execution adapters."""
import json
import shlex
import subprocess
from pathlib import Path

from ..lifecycle.common import LifecycleError
from .common import MAX_BYTES, decode_json, fail, identifier


class LocalProvider:
    def __init__(self, name, config):
        self.name, self.config = name, config

    def call(self, method, **payload):
        from .node import Node
        try:
            return Node(self.config["state_dir"]).dispatch({"method": method, "environment": self.name,
                "generation": self.config["generation"], **payload})
        except OSError as error:
            fail("workspace_io", "Local operation failed; inspect its journal before recovery.", error=str(error))


class SSHProvider:
    def __init__(self, name, config):
        self.name, self.config = name, config

    def _argv(self):
        alias = self.config["connection"]
        # Native aliases may contain a user@host; no option/command injection.
        if (not isinstance(alias, str) or not alias or alias.startswith("-")
                or any(c.isspace() or ord(c) < 32 for c in alias)):
            fail("workspace_transport", "Expected an existing native SSH alias.")
        return ["ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                "-o", "ForwardAgent=no", "-o", "ClearAllForwardings=yes", "-o", "ControlMaster=no",
                "-o", "ControlPath=none", "-o", "PermitLocalCommand=no", "-o", "ConnectTimeout=10", alias]

    def call(self, method, **payload):
        request = {"method": method, "environment": self.name, "generation": self.config["generation"],
                   "state_dir": self.config["state_dir"], **payload}
        if len(json.dumps(request).encode()) > MAX_BYTES:
            fail("workspace_transport", "Request exceeds the portable packet transport bound.")
        python = self.config.get("python", "python3")
        command = [python, "-B", "-m", "skills_auditor.workspace.worker"]
        if self.config.get("python_path"):
            command = ["env", "PYTHONDONTWRITEBYTECODE=1", "PYTHONPATH=" + self.config["python_path"], *command]
        try:
            result = subprocess.run([*self._argv(), shlex.join(command)], input=json.dumps(request), text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
        except (OSError, subprocess.TimeoutExpired) as error:
            fail("workspace_unreachable" if method in {"inspect", "plan", "verify"} else "workspace_uncertain",
                 "Environment operation did not return a confirmed response; inspect before retrying.", error=str(error))
        if len(result.stdout.encode()) > MAX_BYTES:
            fail("workspace_unreachable" if method in {"inspect", "plan", "verify"} else "workspace_uncertain",
                 "Oversized environment response; inspect state before recovery.")
        try:
            response = decode_json(result.stdout)
            if not isinstance(response, dict):
                raise ValueError("Expected object")
        except (ValueError, TypeError, LifecycleError):
            fail("workspace_unreachable" if method in {"inspect", "plan", "verify"} else "workspace_uncertain",
                 "Native SSH did not return a workspace protocol response.", exit_code=result.returncode,
                 diagnostic=result.stderr[-1000:])
        if response.get("status") == "error":
            fail(response.get("code", "workspace_remote"), response.get("message", "Remote workspace error."),
                 remote_details=response.get("details", {}))
        if result.returncode:
            fail("workspace_uncertain", "Remote response and process exit disagree; inspect state.")
        return response


class PotologyProvider(SSHProvider):
    """Consume supported Potology queries, never read/copy its private credentials."""
    def _query(self, *args):
        try:
            result = subprocess.run([self.config.get("python", "python3"), "-B",
                str(Path(self.config["potology_tools"]) / "potology-env.py"),
                "--state", self.config["potology_state"], *args], text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=True)
            return json.loads(result.stdout)
        except (OSError, ValueError, subprocess.SubprocessError):
            fail("workspace_potology", "Potology identity/route query failed; no fallback credentials are used.")

    def _argv(self):
        status = self._query("status")
        target = next((v for v in status.get("environments", []) if v["id"] == self.config["potology_target"]), None)
        if not target or target["generation"] != self.config["generation"]:
            fail("workspace_generation", "Potology target generation changed; review enrollment.")
        routes = self._query("routes", "--target", target["id"], "--capability", "ssh.shell")
        direct = [r for r in routes.get("routes", []) if not r.get("requires_owner_execution")
                  and len(r.get("connections", [])) == 1 and r["connections"][0]["mode"] == "ssh"
                  and r["connections"][0].get("profile_ref") == self.config["pairing"]]
        if not direct:
            fail("workspace_potology", "No owner-executable direct SSH route; delegated routes require their owner.")
        result = self._query("connect-command", "--pairing", self.config["pairing"])
        argv = result.get("argv")
        if not isinstance(argv, list) or not argv or argv[0] != "ssh" or not all(isinstance(x, str) for x in argv):
            fail("workspace_potology", "Potology did not return a validated native SSH vector.")
        # Preserve its pinned identity, fingerprint and no-forwarding options.
        return argv


def provider(name, config):
    identifier(name)
    return {"local": LocalProvider, "ssh": SSHProvider, "potology": PotologyProvider}[config["provider"]](name, config)
