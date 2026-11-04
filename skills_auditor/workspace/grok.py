"""Read Grok's explicit Skill paths without changing configuration or trust."""
import hashlib
import shutil
import subprocess
from pathlib import Path

from ..cli import skill_alias_for_install_root
from ..lifecycle.common import LifecycleError
from .common import MAX_BYTES, decode_json, fail


def configuration(grok_home, project_directories):
    """Conservatively include declared roots from all owner-local config layers.

    Never load hooks, plugins or MCP servers. A configured relative path has an
    ambiguous base across Grok versions, so require explicit absolute paths.
    """
    home = Path(grok_home)
    configs = [home / name for name in ("config.toml", "managed_config.toml", "requirements.toml")]
    configs += [Path("/etc/grok") / name for name in ("managed_config.toml", "requirements.toml")]
    configs += [Path(directory) / ".grok/config.toml" for directory in project_directories]
    roots, observations = [], []
    for config in dict.fromkeys(configs):
        if config.is_symlink() and not config.exists():
            fail("workspace_grok_config", "Grok configuration link is unavailable.", path=str(config))
        if not config.exists():
            continue
        if not config.is_file() or config.stat().st_size > 1024 * 1024:
            fail("workspace_grok_config", "Grok configuration must be a bounded file.", path=str(config))
        try:
            try:
                import tomllib
            except ImportError:  # Python 3.9/3.10 dependency declared in package metadata.
                try:
                    import tomli as tomllib
                except ImportError:
                    fail("workspace_grok_dependency", "Install the target package's TOML dependency before Grok configuration discovery.")
            raw = config.read_bytes()
            value = tomllib.loads(raw.decode("utf-8"))
        except (ValueError, OSError, RecursionError):
            fail("workspace_grok_config", "Grok configuration cannot be parsed; inspect it natively.", path=str(config))
        observations.append({"path": str(config), "sha256": hashlib.sha256(raw).hexdigest()})
        settings = value.get("skills", {})
        if not isinstance(settings, dict):
            fail("workspace_grok_config", "Grok skills configuration must be a table.", path=str(config))
        paths = settings.get("paths", [])
        if not isinstance(paths, list) or any(not isinstance(p, str) or not p for p in paths):
            fail("workspace_grok_config", "Grok Skill paths must be a list of absolute paths.", path=str(config))
        for selected in paths:
            path = Path(selected).expanduser()
            if not path.is_absolute():
                fail("workspace_grok_config", "Use absolute owner-local Grok Skill paths before managed deployment.", path=str(config))
            roots.append(path)
    return {"roots": list(dict.fromkeys(roots)), "configurations": observations}


def inspect_native(workspace, expected):
    """Native catalog plus actual reads, without trusting a folder or invoking a model."""
    if not expected:
        return {"status": "not_applicable", "valid": True, "entries": []}
    command = shutil.which("grok")
    if not command:
        return {"status": "unavailable", "valid": False, "code": "grok_cli_unavailable"}
    try:
        result = subprocess.run([command, "--no-auto-update", "--cwd", str(workspace), "inspect", "--json"],
                                cwd=workspace, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        if result.returncode or len(result.stdout) > MAX_BYTES:
            raise ValueError("Native inspection did not return a bounded success response")
        value = decode_json(result.stdout)
        if not isinstance(value, dict) or not isinstance(value.get("grokVersion"), str) or not isinstance(value.get("skills"), list):
            raise ValueError("Not a Grok Build inspection contract")
        matches, findings = [], []
        for wanted in expected:
            candidates = [row for row in value["skills"] if isinstance(row, dict)
                          and isinstance(row.get("name"), str)
                          and skill_alias_for_install_root(row["name"]) == wanted["name"]]
            if not candidates:
                findings.append({"code": "grok_skill_not_discovered", "name": wanted["name"]})
                continue
            for row in candidates:
                if row.get("userInvocable") is False:
                    findings.append({"code": "grok_skill_not_invocable", "name": wanted["name"]})
                    continue
                source = row.get("source", {})
                path = Path(source.get("path", "")) if isinstance(source, dict) else Path()
                target = Path(wanted["target"]) / "SKILL.md"
                if not path.is_absolute() or not path.is_file() or path.resolve() != target.resolve():
                    findings.append({"code": "grok_native_shadow", "name": wanted["name"]})
                    continue
                data = path.read_bytes()
                matches.append({"name": wanted["name"], "path": str(path), "canonical": str(path.resolve()),
                                "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        return {"status": "verified" if not findings else "unverified", "valid": not findings,
                "level": "catalog_and_reads", "version": value["grokVersion"],
                "project_trusted": value.get("projectTrusted"), "entries": matches, "findings": findings}
    except (OSError, ValueError, TypeError, RecursionError, LifecycleError, subprocess.SubprocessError):
        # Never echo native config, stderr or authentication material.
        return {"status": "unavailable", "valid": False, "code": "grok_inspection_failed"}
