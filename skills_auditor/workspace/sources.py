"""Explicit file selection, pinned Git/local sources and portable packet staging.

Packets are transport envelopes, not another package lock format. They contain
reviewed files only; native vendir locks may be selected as ordinary dependencies.
No source hooks, package scripts or credential helpers are executed.
"""
import base64
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unicodedata
from urllib.parse import urlsplit
from pathlib import Path

from ..cli import parse_skill_name, skill_alias_for_install_root, validate_skill_metadata
from ..lifecycle.snapshots import inspect_source
from .common import MAX_BYTES, fields, fail, identifier, platforms, relative

IGNORED = {".git", ".venv", "node_modules", "__pycache__", ".skills-auditor-local", ".DS_Store"}
PLUGIN_MARKERS = {".codex-plugin", ".claude-plugin", ".cursor-plugin", ".grok-plugin"}


def source_binding(value):
    if not isinstance(value, dict) or not isinstance(value.get("root"), str) or not Path(value["root"]).is_absolute():
        fail("workspace_source", "Source requires an absolute owner-local root.")
    if "environment" in value:
        identifier(value["environment"])
    source_spec({k: v for k, v in value.items() if k not in {"root", "environment"}})
    return value


def source_spec(value):
    fields(value, {"kind", "include", "entries", "platforms"}, {"commit", "required_files", "repository"})
    if value["kind"] not in {"local", "git"}:
        fail("workspace_source", "Source must identify a local snapshot or pinned Git commit.")
    if not isinstance(value["include"], list) or not value["include"]:
        fail("workspace_source", "Select explicit package files/directories with include.")
    for item in value["include"]:
        relative(item)
    if not isinstance(value.get("required_files", []), list):
        fail("workspace_source", "Required dependencies must be a list of package-relative files.")
    for item in value.get("required_files", []):
        relative(item)
    if not isinstance(value["entries"], dict) or not value["entries"]:
        fail("workspace_source", "Source must declare its Skill entrypoints.")
    for name, entry in value["entries"].items():
        identifier(name)
        relative(entry)
    platforms(value["platforms"])
    if value["kind"] == "git":
        import re
        if not isinstance(value.get("commit"), str) or not re.fullmatch(r"[a-f0-9]{40}", value["commit"]):
            fail("workspace_source", "Git releases require a full pinned commit.")
    elif "commit" in value:
        fail("workspace_source", "Local snapshots cannot claim a Git commit.")
    if "repository" in value:
        if not isinstance(value["repository"], str):
            fail("workspace_source", "Repository provenance must be a URL string.")
        url = urlsplit(value["repository"])
        if (value["kind"] != "git" or url.scheme not in {"https", "ssh"} or not url.hostname
                or url.password or url.query or url.fragment or (url.scheme == "https" and url.username)):
            fail("workspace_source", "Repository must be a credential-free HTTPS/SSH URL.")
    return value


def _selected(path, includes):
    return not (set(Path(path).parts) & IGNORED) and any(
        item == "." or path == item or path.startswith(item + "/") for item in includes)


def _portable_path_key(path):
    return unicodedata.normalize("NFD", path).casefold()


def _git(root, *args):
    try:
        return subprocess.check_output(["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=", "-C", str(root), *args],
                                       stderr=subprocess.PIPE, timeout=60)
    except (subprocess.SubprocessError, OSError) as error:
        fail("workspace_git", "Pinned Git source cannot be read.", error=str(error))


def capture(binding):
    source_binding(binding)
    if "environment" in binding:
        fail("workspace_source_environment", "An environment-owned source must be captured through its registered provider.")
    try:
        root = Path(binding["root"]).resolve()
    except (OSError, RuntimeError) as error:
        fail("workspace_source", "Source root cannot be resolved.", error=str(error))
    if binding["kind"] == "local" and any((ancestor / marker).is_dir()
            for ancestor in [root, *root.parents] for marker in PLUGIN_MARKERS):
        fail("workspace_plugin_excluded", "Do not flatten plugin-owned Skills into standalone releases.")
    records = []
    total = 0
    matched = set()
    observations = []

    def record(path, mode, kind, data):
        nonlocal total
        relative(path)
        total += len(data)
        if total > MAX_BYTES:
            fail("workspace_capacity", "Selected source exceeds the 64 MiB packet bound.")
        records.append({"path": path, "mode": mode, "kind": kind,
                        "data": base64.b64encode(data).decode("ascii")})
        matched.update(item for item in binding["include"] if item == "." or path == item or path.startswith(item + "/"))

    if binding["kind"] == "git":
        actual = _git(root, "rev-parse", binding["commit"] + "^{commit}").decode().strip()
        if actual != binding["commit"]:
            fail("workspace_git", "Git commit did not resolve exactly.")
        rows = [row.split(b"\t", 1) for row in
                _git(root, "ls-tree", "-rz", "--full-tree", actual).split(b"\0") if row]
        plugin_roots = set()
        for _, raw_path in rows:
            parts = Path(raw_path.decode("utf-8")).parts
            for index, part in enumerate(parts):
                if part in PLUGIN_MARKERS:
                    plugin_roots.add("/".join(parts[:index]))
        for meta, raw_path in rows:
            mode, kind, oid = meta.decode().split()
            path = raw_path.decode("utf-8")
            if not _selected(path, binding["include"]):
                continue
            if any(not parent or path == parent or path.startswith(parent + "/") for parent in plugin_roots):
                fail("workspace_plugin_excluded", "Selected pinned files belong to a plugin package.", path=path)
            if kind != "blob":
                fail("workspace_source", "Submodules require an explicitly materialized source.")
            data = _git(root, "cat-file", "blob", oid)
            if data.startswith(b"version https://git-lfs.github.com/spec/v1\n"):
                fail("workspace_dependency_missing", "Git LFS content must be explicitly materialized before release.", path=path)
            record(path, 0o777 if mode == "120000" else 0o755 if mode == "100755" else 0o644,
                   "symlink" if mode == "120000" else "file", data)
        origin = {"kind": "git", "commit": actual}
        if "repository" in binding:
            origin["repository"] = binding["repository"]
    else:
        if not root.is_dir() or root == root.parent or root == Path.home().resolve():
            fail("workspace_source", "Select a concrete local package, not a home/filesystem root.")
        seen = set()
        for include in binding["include"]:
            start = root / include
            if not start.exists() and not start.is_symlink():
                fail("workspace_dependency_missing", "Selected package dependency is missing.", path=include)
            candidates = [start]
            if start.is_dir() and not start.is_symlink():
                for parent, dirs, files in os.walk(start, followlinks=False):
                    dirs[:] = sorted(d for d in dirs if d not in IGNORED)
                    candidates += [Path(parent) / name for name in dirs + sorted(files)]
            for path in candidates:
                rel = path.relative_to(root).as_posix()
                if rel == "." or rel in seen or not _selected(rel, binding["include"]):
                    continue
                seen.add(rel)
                try:
                    physical = path.resolve()
                except (OSError, RuntimeError) as error:
                    fail("workspace_source", "Selected source path cannot be resolved.", error=str(error))
                if any((parent / marker).is_dir() for parent in [physical, *physical.parents] for marker in PLUGIN_MARKERS):
                    fail("workspace_plugin_excluded", "Selected files belong to a plugin package.", path=rel)
                info = path.lstat()
                observations.append((path, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns))
                if stat.S_ISLNK(info.st_mode):
                    record(rel, stat.S_IMODE(info.st_mode), "symlink", os.readlink(path).encode())
                elif stat.S_ISREG(info.st_mode):
                    record(rel, stat.S_IMODE(info.st_mode), "file", path.read_bytes())
                elif stat.S_ISDIR(info.st_mode):
                    record(rel, stat.S_IMODE(info.st_mode), "directory", b"")
                else:
                    fail("workspace_source", "Special files are not Skill payloads.", path=rel)
        for path, *before in observations:
            info = path.lstat()
            if before != [info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]:
                fail("workspace_source_changed", "Source changed during capture.")
        origin = {"kind": "local", "uncommitted_snapshot": True}
    if set(binding["include"]) - matched:
        fail("workspace_dependency_missing", "Selected dependency has no materialized files.", paths=sorted(set(binding["include"]) - matched))
    packet = {"origin": origin, "files": sorted(records, key=lambda r: r["path"]),
              "entries": binding["entries"], "platforms": binding["platforms"],
              "required_files": binding.get("required_files", [])}
    with tempfile.TemporaryDirectory(prefix="skills-auditor-packet-") as temp:
        staged = Path(temp) / "tree"
        unpack(packet, staged)
        packet["inspection"] = inspect_source(staged)
    if len(json.dumps(packet).encode()) > MAX_BYTES:
        fail("workspace_capacity", "Encoded source packet exceeds the portable transport bound.")
    return packet


def unpack(packet, destination):
    """Validate the entire portable path topology before touching a destination."""
    fields(packet, {"origin", "files", "entries", "platforms"}, {"inspection", "required_files"})
    origin = packet["origin"]
    if not isinstance(origin, dict) or origin.get("kind") not in {"local", "git"}:
        fail("workspace_packet", "Packet provenance is missing.")
    spec = {k: packet[k] for k in ("entries", "platforms")}
    spec.update(kind=origin["kind"], include=["."], required_files=packet.get("required_files", []))
    spec.update({k: origin[k] for k in ("commit", "repository") if k in origin})
    source_spec(spec)
    if origin["kind"] == "local" and origin != {"kind": "local", "uncommitted_snapshot": True}:
        fail("workspace_packet", "Local snapshots must be explicitly marked uncommitted.")
    platforms(packet["platforms"])
    if not isinstance(packet["files"], list) or len(packet["files"]) > 50000:
        fail("workspace_packet", "Invalid packet file list.")
    names = set()
    decoded = []
    size = 0
    for item in packet["files"]:
        fields(item, {"path", "mode", "kind", "data"})
        path = relative(item["path"])
        if set(Path(path).parts) & PLUGIN_MARKERS:
            fail("workspace_plugin_excluded", "Plugin packages are outside standalone Skill delivery.")
        folded = _portable_path_key(path)
        if folded in names or item["kind"] not in {"file", "directory", "symlink"}:
            fail("workspace_packet", "Duplicate or unsupported package entry.")
        names.add(folded)
        if type(item["mode"]) is not int or not 0 <= item["mode"] <= 0o777:
            fail("workspace_packet", "Unsupported package permissions.")
        try:
            data = base64.b64decode(item["data"], validate=True)
        except (ValueError, TypeError):
            fail("workspace_packet", "Invalid package bytes.")
        size += len(data)
        if size > MAX_BYTES:
            fail("workspace_capacity", "Package exceeds the transport bound.")
        decoded.append((item, data))
    non_directories = {_portable_path_key(item["path"]) for item, _ in decoded if item["kind"] != "directory"}
    spellings = {}
    for item, data in decoded:
        path = Path(item["path"])
        if any(_portable_path_key(parent.as_posix()) in non_directories for parent in path.parents):
            fail("workspace_packet", "Package entry traverses a non-directory.")
        for component in [path, *path.parents]:
            if component == Path("."):
                continue
            name = component.as_posix()
            key = _portable_path_key(name)
            if spellings.setdefault(key, name) != name:
                fail("workspace_packet", "Package directories have ambiguous portable spellings.")
        if item["kind"] == "symlink":
            target = data.decode("utf-8")
            if Path(target).is_absolute():
                fail("workspace_packet", "Absolute source links cannot cross environments.")
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        fail("workspace_packet", "Packet destination must be new.")
    destination.mkdir(parents=True)
    implicit_directories = set()
    for item, data in decoded:
        path = destination / item["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        implicit_directories.update(parent for parent in path.parents if destination in parent.parents)
        if item["kind"] == "directory":
            path.mkdir(exist_ok=True)
        elif item["kind"] == "symlink":
            path.symlink_to(data.decode("utf-8"))
        else:
            path.write_bytes(data)
    explicit_directories = {destination / item["path"] for item, _ in decoded if item["kind"] == "directory"}
    for directory in implicit_directories - explicit_directories:
        directory.chmod(0o755)
    # Children may require writable staging parents; apply modes after extraction.
    for item, _ in reversed(decoded):
        if item["kind"] == "symlink":
            path = destination / item["path"]
            if stat.S_IMODE(path.lstat().st_mode) != item["mode"]:
                try:
                    os.chmod(path, item["mode"], follow_symlinks=False)
                except (OSError, NotImplementedError):
                    fail("workspace_packet", "Target filesystem cannot preserve selected link permissions.")
        else:
            (destination / item["path"]).chmod(item["mode"])
    return validate_tree(packet, destination)


def validate_tree(packet, destination):
    inspection = inspect_source(destination)
    if "inspection" in packet and packet["inspection"] != inspection:
        fail("workspace_packet", "Materialized packet does not match its reviewed hash.")
    for required in packet.get("required_files", []):
        if not (destination / required).is_file():
            fail("workspace_dependency_missing", "Declared dependency is missing from the fixed package.", path=required)
    for name, entry in packet["entries"].items():
        identifier(name)
        relative(entry)
        folder = destination / entry
        if (set(folder.relative_to(destination).parts) & PLUGIN_MARKERS
                or any((parent / marker).is_dir() for parent in [folder, *folder.parents]
                       if parent == destination or destination in parent.parents for marker in PLUGIN_MARKERS)):
            fail("workspace_plugin_excluded", "Plugin entrypoints are outside this feature.", name=name)
        md = folder / "SKILL.md"
        if not md.is_file() or validate_skill_metadata(md):
            fail("workspace_metadata", "Entrypoint must contain valid Skill metadata.", name=name)
        if skill_alias_for_install_root(parse_skill_name(md)) != name:
            fail("workspace_metadata", "Declared alias differs from Skill identity.", name=name)
    return inspection


def stage(packet, cache):
    cache = Path(cache)
    inspection = packet["inspection"]
    key = inspection["source_tree_sha256"]
    if not isinstance(key, str) or len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
        fail("workspace_packet", "Invalid source content key.")
    target = cache / key
    if target.exists():
        if validate_tree(packet, target) != inspection:
            fail("workspace_packet", "Existing candidate content changed.")
        # A healthy cache cannot prove that an exported transport envelope is
        # reproducible. Validate its bytes too, without rebuilding the cache.
        with tempfile.TemporaryDirectory(prefix=".validate-", dir=cache) as temporary:
            unpack(packet, Path(temporary) / "tree")
        return target
    cache.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".stage-", dir=cache))
    try:
        unpack(packet, temporary / "tree")
        os.rename(temporary / "tree", target)
    finally:
        shutil.rmtree(temporary)
    return target
