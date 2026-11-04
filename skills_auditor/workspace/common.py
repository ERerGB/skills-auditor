"""Small bounded workspace contracts layered on the managed lifecycle codecs."""
import json
import os
import platform
import re
from pathlib import Path, PurePosixPath

from ..lifecycle.common import LifecycleError, atomic_json, digest, utc_now

PREFIX = "skills-auditor-workspace-"
DEFAULT_PLATFORMS = ("codex", "cursor", "claude-code")
PLATFORMS = (*DEFAULT_PLATFORMS, "grok")
MAX_BYTES = 64 * 1024 * 1024


def fail(code, message, **details):
    raise LifecycleError(code, message, details=details)


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", value):
        fail("workspace_input", "Expected a bounded identifier.")
    return value


def relative(value):
    if (not isinstance(value, str) or not value or "\\" in value or any(ord(c) < 32 for c in value)
            or PurePosixPath(value).is_absolute() or ".." in PurePosixPath(value).parts
            or str(PurePosixPath(value)) != value):
        fail("workspace_path", "Expected a normalized relative path.")
    return value


def platforms(value):
    if (not isinstance(value, list) or not value or any(not isinstance(v, str) for v in value) or len(value) != len(set(value))
            or any(item not in PLATFORMS for item in value)):
        fail("workspace_input", "Unsupported or duplicate agent platforms.")
    return value


def fields(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= set(value) <= set(required) | set(optional):
        fail("workspace_input", "Missing or unexpected contract fields.")
    return value


def read_json(path, limit=MAX_BYTES):
    path = Path(path)
    if not path.is_file() or path.stat().st_size > limit:
        fail("workspace_json", "Expected a bounded regular JSON file.", path=str(path))
    return decode_json(path.read_bytes())


def decode_json(raw):
    def pairs(items):
        out = {}
        for key, item in items:
            if key in out:
                fail("workspace_json", "Duplicate JSON keys are unsupported.")
            out[key] = item
        return out
    try:
        return json.loads(raw, object_pairs_hook=pairs,
                          parse_constant=lambda _: fail("workspace_json", "Non-finite JSON is unsupported."))
    except (ValueError, UnicodeError) as error:
        fail("workspace_json", "Could not decode JSON.", error=str(error))


def seal(value, key):
    value[key] = digest(value)
    return value


def checked(value, key, schema):
    if (not isinstance(value, dict) or value.get("schema_version") != PREFIX + schema + "/v1"
            or value.get(key) != digest({k: v for k, v in value.items() if k != key})):
        fail("workspace_contract", "Document schema or content identity does not match.")
    return value


def fingerprint():
    # This is a local runtime binding, not remote cryptographic attestation.
    return digest({"hostname": platform.node(), "system": platform.system(),
                   "user": os.getuid(), "home": str(Path.home().resolve())})


def declaration(value):
    fields(value, {"schema_version", "baseline"}, {"overrides", "platforms", "sources"})
    if value["schema_version"] != PREFIX + "declaration/v1":
        fail("workspace_input", "Unsupported workspace declaration schema.")
    if (not isinstance(value["baseline"], list) or not value["baseline"]
            or any(not isinstance(v, str) for v in value["baseline"])
            or len(value["baseline"]) != len(set(value["baseline"]))):
        fail("workspace_input", "Baseline must name distinct source bindings.")
    for item in value["baseline"]:
        identifier(item)
    if not isinstance(value.get("sources", {}), dict):
        fail("workspace_source", "Shared source declarations must be an object.")
    from .sources import source_spec
    for name, spec in value.get("sources", {}).items():
        identifier(name)
        source_spec(spec)
    platforms(value.get("platforms", list(DEFAULT_PLATFORMS)))
    overrides = value.get("overrides", {})
    if not isinstance(overrides, dict):
        fail("workspace_input", "Project overrides must be an object.")
    for name, item in overrides.items():
        identifier(name)
        if item is not None:
            fields(item, {"source", "entry"})
            identifier(item["source"])
            relative(item["entry"])
    return value
