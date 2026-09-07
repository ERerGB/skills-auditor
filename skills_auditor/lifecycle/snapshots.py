"""Reviewed, content-addressed trees with bounded, durable publication.

The source digest matches integration/v1, including child permission bits but
excluding the root directory's mode. The snapshot digest intentionally removes
write bits from files/directories; symlink text and modes are unchanged. These
are local integrity checks, not privileged-tamper protection. Publication uses
an advisory store-directory lock: cooperating local writers must respect it.
"""

from __future__ import annotations

from contextlib import contextmanager
import errno
import hashlib
import json
import math
import os
import re
import stat
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .common import LifecycleError, atomic_json, fsync_directory

try:
    import fcntl
except ImportError:
    fcntl = None


NORMALIZATION = "remove-write-bits/v1"
_HASH = re.compile(r"^[0-9a-f]{64}$")
Checkpoint = Optional[Callable[[str, Dict[str, Any]], None]]


def _unsafe(message: str) -> LifecycleError:
    return LifecycleError("snapshot_unsafe_source", message)


def _identity(info: os.stat_result) -> Tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _open_directory(path: Any, *, dir_fd: Optional[int] = None) -> int:
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise LifecycleError("snapshot_unsupported", "Snapshot storage requires POSIX no-follow directory descriptors")
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)


def _hash(records: List[List[str]]) -> str:
    payload = json.dumps(records, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validate_links(records: List[List[str]]) -> None:
    """Resolve link topology from already-read entries, never external paths."""
    entries = {tuple(row[1].split("/")): row for row in records}
    directories = {path for path, row in entries.items() if row[0] == "directory"}
    directories.add(())
    edges = {path: [] for path in directories}
    for path in directories:
        if path:
            edges[path[:-1]].append(path)

    def resolve(path: Tuple[str, ...], target: str) -> Tuple[str, ...]:
        current = path[:-1]
        pending = target.split("/")
        expansions = 0
        while pending:
            component = pending.pop(0)
            if component in ("", "."):
                continue
            if component == "..":
                if not current:
                    raise _unsafe("Snapshot symlink escapes its source tree: " + "/".join(path))
                current = current[:-1]
                continue
            candidate = current + (component,)
            row = entries.get(candidate)
            if row is None:
                raise _unsafe("Dangling snapshot symlink: " + "/".join(path))
            if row[0] == "symlink":
                expansions += 1
                if expansions > 40:
                    raise _unsafe("Looping snapshot symlink: " + "/".join(path))
                if row[3].startswith("/"):
                    raise _unsafe("Absolute snapshot symlinks are unsupported")
                pending = row[3].split("/") + pending
            else:
                if pending and row[0] != "directory":
                    raise _unsafe("Snapshot symlink traverses a non-directory: " + "/".join(path))
                current = candidate
        return current

    for path, row in entries.items():
        if row[0] != "symlink":
            continue
        if row[3].startswith("/"):
            raise _unsafe("Absolute snapshot symlinks are unsupported: " + "/".join(path))
        destination = resolve(path, row[3])
        if destination in directories:
            edges[path[:-1]].append(destination)
    visiting: set[Tuple[str, ...]] = set()
    visited: set[Tuple[str, ...]] = set()

    def visit(node: Tuple[str, ...]) -> None:
        if node in visiting:
            raise _unsafe("Snapshot directory symlinks form a traversal cycle")
        if node in visited:
            return
        visiting.add(node)
        for child in edges[node]:
            visit(child)
        visiting.remove(node)
        visited.add(node)

    visit(())


def _scan(root: Path, copy_to: Optional[Path] = None) -> Dict[str, str]:
    """Read children by no-follow descriptors, detecting concurrent replacement."""
    records: List[List[str]] = []
    normalized: List[List[str]] = []
    root_fd = _open_directory(root)
    root_info = os.fstat(root_fd)
    if copy_to is not None:
        try:
            copy_to.mkdir(mode=0o700)
        except Exception:
            os.close(root_fd)
            raise

    def walk(fd: int, relative: Tuple[str, ...]) -> None:
        before = os.fstat(fd)
        for name in sorted(os.listdir(fd)):
            parts = relative + (name,)
            relative_path = "/".join(parts)
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            mode = stat.S_IMODE(info.st_mode)
            target = copy_to.joinpath(*parts) if copy_to is not None else None
            if stat.S_ISLNK(info.st_mode):
                raw = os.readlink(name, dir_fd=fd)
                row = ["symlink", relative_path, format(mode, "04o"), raw]
                if target is not None:
                    target.symlink_to(raw)
                    # macOS link modes depend on umask and can be changed;
                    # Linux links normally remain 0777. Preserve the reviewed
                    # mode without ever chmod-ing the link's destination.
                    if stat.S_IMODE(target.lstat().st_mode) != mode:
                        try:
                            os.chmod(target, mode, follow_symlinks=False)
                        except (OSError, NotImplementedError) as exc:
                            raise LifecycleError(
                                "snapshot_unsupported",
                                "Filesystem cannot preserve reviewed symlink permissions",
                                details={"entry": relative_path, "mode": format(mode, "04o")},
                            ) from exc
                        if stat.S_IMODE(target.lstat().st_mode) != mode:
                            raise LifecycleError(
                                "snapshot_unsupported",
                                "Filesystem did not preserve reviewed symlink permissions",
                                details={"entry": relative_path, "mode": format(mode, "04o")},
                            )
            elif stat.S_ISDIR(info.st_mode):
                row = ["directory", relative_path, format(mode, "04o")]
                records.append(row)
                normalized.append([row[0], row[1], format(mode & ~0o222, "04o")])
                child_fd = _open_directory(name, dir_fd=fd)
                try:
                    if _identity(os.fstat(child_fd)) != _identity(info):
                        raise _unsafe("Source directory changed while being opened")
                    if target is not None:
                        target.mkdir(mode=0o700)
                    walk(child_fd, parts)
                    if target is not None:
                        target.chmod(mode)
                finally:
                    os.close(child_fd)
                continue
            elif stat.S_ISREG(info.st_mode):
                source_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                output_fd = None
                content_hash = hashlib.sha256()
                try:
                    if _identity(os.fstat(source_fd)) != _identity(info):
                        raise _unsafe("Source file changed while being opened")
                    if target is not None:
                        output_fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    while True:
                        chunk = os.read(source_fd, 1024 * 1024)
                        if not chunk:
                            break
                        content_hash.update(chunk)
                        if output_fd is not None:
                            remaining = memoryview(chunk)
                            while remaining:
                                count = os.write(output_fd, remaining)
                                if count <= 0:
                                    raise OSError("Snapshot copy made no write progress")
                                remaining = remaining[count:]
                    if _identity(os.fstat(source_fd)) != _identity(info):
                        raise _unsafe("Source file changed while being read")
                    if output_fd is not None:
                        os.fchmod(output_fd, mode)
                        os.fsync(output_fd)
                finally:
                    os.close(source_fd)
                    if output_fd is not None:
                        os.close(output_fd)
                row = ["file", relative_path, format(mode, "04o"), content_hash.hexdigest()]
            else:
                raise _unsafe("Special files are unsupported in snapshots: " + relative_path)
            if _identity(os.stat(name, dir_fd=fd, follow_symlinks=False)) != _identity(info):
                raise _unsafe("Source entry changed while being read: " + relative_path)
            records.append(row)
            normalized.append(row if row[0] == "symlink" else
                              [row[0], row[1], format(mode & ~0o222, "04o")] + row[3:])
        if _identity(os.fstat(fd)) != _identity(before):
            raise _unsafe("Source directory changed while being scanned")

    try:
        walk(root_fd, ())
        _validate_links(records)
        if copy_to is not None:
            copy_to.chmod(stat.S_IMODE(root_info.st_mode))
        return {"source_tree_sha256": _hash(records),
                "snapshot_tree_sha256": _hash(normalized), "normalization": NORMALIZATION}
    finally:
        os.close(root_fd)


def inspect_source(source: Path) -> Dict[str, str]:
    """Return reviewable source and normalized hashes without changing its tree."""
    try:
        return _scan(Path(source))
    except LifecycleError:
        raise
    except (OSError, ValueError, RuntimeError) as exc:
        raise LifecycleError("snapshot_unsafe_source", f"Cannot inspect snapshot source: {exc}") from exc


def _copy_tree(source: Path, tree: Path) -> Dict[str, str]:
    return _scan(source, tree)


def _normalize(tree: Path) -> None:
    fd = _open_directory(tree)
    try:
        for name in os.listdir(fd):
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                _normalize(tree / name)
            elif stat.S_ISREG(info.st_mode):
                os.chmod(name, stat.S_IMODE(info.st_mode) & ~0o222, dir_fd=fd,
                         follow_symlinks=False)
            elif not stat.S_ISLNK(info.st_mode):
                raise _unsafe("Staged snapshot contains a special file")
        os.fchmod(fd, stat.S_IMODE(os.fstat(fd).st_mode) & ~0o222)
    finally:
        os.close(fd)


def _sync_tree(tree: Path) -> None:
    fd = _open_directory(tree)
    try:
        for name in os.listdir(fd):
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                _sync_tree(tree / name)
            elif stat.S_ISREG(info.st_mode):
                file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    os.fsync(file_fd)
                finally:
                    os.close(file_fd)
        os.fsync(fd)
    finally:
        os.close(fd)


def _remove_stage(stage: Path) -> None:
    """Remove only an owned staging tree, never traversing a symlink."""
    fd = _open_directory(stage)

    def remove_children(directory_fd: int) -> None:
        os.fchmod(directory_fd, stat.S_IMODE(os.fstat(directory_fd).st_mode) | 0o700)
        for name in os.listdir(directory_fd):
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child_fd = _open_directory(name, dir_fd=directory_fd)
                try:
                    remove_children(child_fd)
                finally:
                    os.close(child_fd)
                os.rmdir(name, dir_fd=directory_fd)
            else:
                os.unlink(name, dir_fd=directory_fd)

    try:
        remove_children(fd)
    finally:
        os.close(fd)
    stage.rmdir()


def _validate_descriptor(descriptor: Dict[str, Any]) -> Path:
    if not isinstance(descriptor, dict) or set(descriptor) != {
        "source_tree_sha256", "snapshot_tree_sha256", "normalization", "path"
    }:
        raise LifecycleError("snapshot_invalid_descriptor", "Snapshot descriptor has an invalid shape")
    if (any(not isinstance(descriptor[key], str) or not _HASH.fullmatch(descriptor[key])
            for key in ("source_tree_sha256", "snapshot_tree_sha256"))
            or descriptor["normalization"] != NORMALIZATION
            or not isinstance(descriptor["path"], str)):
        raise LifecycleError("snapshot_invalid_descriptor", "Snapshot descriptor has invalid hashes or normalization")
    tree = Path(descriptor["path"])
    if (not tree.is_absolute() or tree.name != "tree"
            or tree.parent.name != descriptor["snapshot_tree_sha256"]
            or str(tree) != str(tree.resolve(strict=False))):
        raise LifecycleError("snapshot_invalid_descriptor", "Snapshot path is not canonical content-addressed storage")
    return tree


def _read_metadata(path: Path) -> Dict[str, Any]:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
            raise ValueError("Snapshot metadata must be a bounded regular file")
        payload = handle.read(4097)
        if len(payload) > 4096:
            raise ValueError("Snapshot metadata exceeded its size bound")
        return json.loads(payload)


def verify_snapshot(descriptor: Dict[str, Any]) -> Dict[str, Any]:
    """Fail closed on corrupt payload, writable content or malformed metadata."""
    evidence = {}
    try:
        tree = _validate_descriptor(descriptor)
        evidence = {"expected": descriptor["snapshot_tree_sha256"], "actual": None, "path": str(tree)}
        parent = tree.parent
        if parent.is_symlink() or tree.is_symlink():
            raise ValueError("snapshot object or tree is a symlink")
        if set(os.listdir(parent)) != {"tree", "manifest.json", "stage.json"}:
            raise ValueError("snapshot object has unexpected entries")
        expected = {"schema_version": "skills-auditor-snapshot/v1",
                    "snapshot_tree_sha256": descriptor["snapshot_tree_sha256"],
                    "normalization": NORMALIZATION}
        if _read_metadata(parent / "manifest.json") != expected:
            raise ValueError("snapshot manifest does not match descriptor")
        intent = _read_metadata(parent / "stage.json")
        if (not isinstance(intent, dict) or set(intent) != {
                "schema_version", "source_tree_sha256", "snapshot_tree_sha256", "normalization"}
                or intent["schema_version"] != "skills-auditor-snapshot-stage/v1"
                or intent["snapshot_tree_sha256"] != descriptor["snapshot_tree_sha256"]
                or intent["normalization"] != NORMALIZATION
                or not isinstance(intent["source_tree_sha256"], str)
                or not _HASH.fullmatch(intent["source_tree_sha256"])):
            raise ValueError("snapshot stage provenance is malformed")
        actual = _scan(tree)
        evidence["actual"] = actual["source_tree_sha256"]
        if actual["source_tree_sha256"] != descriptor["snapshot_tree_sha256"]:
            raise ValueError("snapshot bytes or modes do not match the approved digest")
        if (actual["snapshot_tree_sha256"] != descriptor["snapshot_tree_sha256"]
                or stat.S_IMODE(tree.stat().st_mode) & 0o222):
            raise ValueError("snapshot is not normalized read-only content")
        return descriptor
    except LifecycleError as exc:
        if exc.code == "snapshot_invalid_descriptor":
            raise
        raise LifecycleError("snapshot_corrupt", f"Snapshot verification failed: {exc}", details=evidence) from exc
    except (OSError, ValueError, RuntimeError) as exc:
        raise LifecycleError("snapshot_corrupt", f"Snapshot verification failed: {exc}", details=evidence) from exc


def _mkdir_durable(path: Path) -> None:
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    if not current.is_dir() or current.is_symlink():
        raise ValueError("Snapshot store parent is not a real directory")
    for directory in reversed(missing):
        directory.mkdir(exist_ok=True)
        fsync_directory(directory.parent)


@contextmanager
def locked_store(store_root: Path, *, timeout: float = 5.0):
    """Coordinate publication/GC using the SAME existing store directory inode.

    Acquire coordinator/target locks before this lock, never the reverse. The
    context yields its directory descriptor for fsync, not ownership of it.
    Store directories must not be renamed/replaced by cooperating writers.
    Contention is bounded; process death closes the descriptor and releases it.
    """
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError("Snapshot store lock timeout must be finite and nonnegative")
    if fcntl is None:
        raise LifecycleError("snapshot_unsupported", "Snapshot publication requires local POSIX flock")
    fd = _open_directory(store_root)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EACCES):
                    raise LifecycleError("snapshot_lock_failed", f"Cannot lock snapshot store: {exc}") from exc
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise LifecycleError("snapshot_store_busy", "Another writer holds the snapshot store",
                                         details={"path": str(store_root)}) from exc
                time.sleep(min(remaining, 0.01))
        yield fd
    finally:
        os.close(fd)


def _publish(stage: Path, destination: Path, descriptor: Dict[str, Any]) -> bool:
    with locked_store(destination.parent) as fd:
        if os.path.lexists(destination):
            verify_snapshot(descriptor)
            return False
        os.rename(stage, destination)
        try:
            os.fsync(fd)
        except OSError as exc:
            raise LifecycleError("snapshot_publish_uncertain", "Snapshot was renamed but directory sync failed",
                                 details={"path": str(destination), "published": True, "error": str(exc)}) from exc
        return True


def materialize(
    source: Path,
    store_root: Path,
    expected_source_hash: str,
    expected_snapshot_hash: str,
    *,
    checkpoint: Checkpoint = None,
) -> Dict[str, str]:
    """Publish an immutable reviewed tree or return a previously verified object.

    Checkpoints are explicit dependency-injection callbacks for crash testing,
    never environment-controlled execution. Interrupted `.stage-*` directories
    carry durable intent and are not silently reused or deleted by a retry.
    """
    source = Path(source).absolute()
    store_root = Path(store_root).absolute()
    if (not isinstance(expected_source_hash, str) or not _HASH.fullmatch(expected_source_hash)
            or not isinstance(expected_snapshot_hash, str) or not _HASH.fullmatch(expected_snapshot_hash)):
        raise LifecycleError("snapshot_invalid_descriptor", "Reviewed snapshot hashes must be lowercase SHA-256")
    try:
        resolved_source = source.resolve(strict=True)
        resolved_store = store_root.resolve(strict=False)
        if source.is_symlink() or store_root.is_symlink():
            raise _unsafe("Snapshot source/store root must not be a symlink")
        if (resolved_source == resolved_store or resolved_source in resolved_store.parents
                or resolved_store in resolved_source.parents):
            raise LifecycleError("snapshot_path_overlap", "Snapshot source and store must not contain each other")
        source, store_root = resolved_source, resolved_store
    except (OSError, RuntimeError) as exc:
        raise LifecycleError("snapshot_unsafe_source", f"Cannot resolve snapshot source/store: {exc}") from exc
    expected = {"source_tree_sha256": expected_source_hash,
                "snapshot_tree_sha256": expected_snapshot_hash, "normalization": NORMALIZATION}
    descriptor = dict(expected, path=str(store_root / expected_snapshot_hash / "tree"))

    def require_unchanged(actual: Dict[str, str]) -> None:
        if actual != expected:
            raise LifecycleError("snapshot_source_changed", "Source or staged copy differs from the reviewed snapshot plan")

    require_unchanged(inspect_source(source))
    stage = None
    stage_lease = None
    published = False
    try:
        _mkdir_durable(store_root)
        if os.path.lexists(Path(descriptor["path"]).parent):
            verify_snapshot(descriptor)
            require_unchanged(inspect_source(source))
            return descriptor
        stage = Path(tempfile.mkdtemp(prefix=f".stage-{expected_snapshot_hash}-", dir=store_root))
        stage_lease = _open_directory(stage)
        if fcntl is None:
            raise LifecycleError("snapshot_unsupported", "Snapshot staging requires local POSIX flock")
        # Acquire before publishing recognizable intent. GC takes the store
        # lock and only *tries* this lease, so a live copy never becomes an
        # orphan and stage->store publication cannot deadlock with collection.
        fcntl.flock(stage_lease, fcntl.LOCK_EX)
        intent = dict(expected, schema_version="skills-auditor-snapshot-stage/v1")
        atomic_json(stage / "stage.json", intent)
        fsync_directory(store_root)
        details = dict(descriptor, stage=str(stage))
        if checkpoint:
            checkpoint("snapshot_staged", details)
        require_unchanged(_copy_tree(source, stage / "tree"))
        if checkpoint:
            checkpoint("snapshot_copied", details)
        require_unchanged(inspect_source(source))
        require_unchanged(inspect_source(stage / "tree"))
        _normalize(stage / "tree")
        manifest = {"schema_version": "skills-auditor-snapshot/v1",
                    "snapshot_tree_sha256": expected_snapshot_hash, "normalization": NORMALIZATION}
        atomic_json(stage / "manifest.json", manifest)
        (stage / "manifest.json").chmod(0o444)
        (stage / "stage.json").chmod(0o444)
        stage.chmod(0o555)
        _sync_tree(stage)
        if checkpoint:
            checkpoint("snapshot_durable", details)
        require_unchanged(inspect_source(source))
        if inspect_source(stage / "tree")["source_tree_sha256"] != expected_snapshot_hash:
            raise LifecycleError("snapshot_source_changed", "Normalized staged tree changed before publication")
        published = _publish(stage, Path(descriptor["path"]).parent, descriptor)
        if not published:
            _remove_stage(stage)
        if checkpoint:
            checkpoint("snapshot_published", details)
        return verify_snapshot(descriptor)
    except Exception as exc:
        cleanup = []
        if stage is not None and os.path.lexists(stage):
            try:
                _remove_stage(stage)
            except OSError as cleanup_exc:
                cleanup.append({"stage": str(stage), "cleanup_error": str(cleanup_exc)})
        if isinstance(exc, LifecycleError):
            if cleanup:
                exc.details["cleanup"] = cleanup
            raise
        raise LifecycleError("snapshot_write_failed", f"Cannot materialize snapshot: {exc}",
                             details={"path": descriptor["path"], "published": published,
                                      "cleanup": cleanup}) from exc
    finally:
        if stage_lease is not None:
            os.close(stage_lease)
