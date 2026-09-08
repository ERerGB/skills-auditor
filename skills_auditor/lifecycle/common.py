"""Small strict codecs and durable file publication for local lifecycle state.

Durability assumes local Linux/macOS filesystems implementing fsync and atomic
same-directory rename. A failure syncing the directory *after* replacement is
an uncertain durability outcome: the new file may already be visible. Callers
must inspect/reconcile that state, never infer that an exception undid a write.
"""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unicodedata
from typing import Any, Dict, Optional, Union


class LifecycleError(Exception):
    """A stable machine-readable failure, with bounded domain-supplied details."""

    def __init__(self, code: str, message: str, *, details: Optional[Dict[str, Any]] = None, exit_code: int = 3):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = {} if details is None else details
        self.exit_code = exit_code

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": "skills-auditor-lifecycle-error/v1",
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


def canonical_json(value: Any) -> str:
    """Encode JSON deterministically; non-finite numbers are not valid state."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def canonical_entry(path: Union[str, Path]) -> Path:
    """Resolve parent aliases without following an installed symlink leaf."""
    entry = Path(path).expanduser()
    if not entry.name or entry.name in (".", ".."):
        raise LifecycleError("unsafe_path", "a concrete entry, not a filesystem root, is required")
    return entry.parent.resolve(strict=False) / entry.name


def path_components(path: Union[str, Path]):
    """Conservative alias key for already scoped paths, without leaf traversal.

    Linux also rejects case/NFD-equivalent overlaps. This deliberate restriction
    gives the same safety boundary on case-sensitive and default macOS volumes;
    it does not claim to identify every possible mount or hard-link alias.
    """
    absolute = Path(os.path.abspath(str(Path(path).expanduser())))
    return tuple(unicodedata.normalize("NFD", part).casefold() for part in absolute.parts)


def paths_overlap(left: Union[str, Path], right: Union[str, Path]) -> bool:
    """Compare every component, not only the leaves, across filesystem aliases."""
    left_parts, right_parts = path_components(left), path_components(right)
    length = min(len(left_parts), len(right_parts))
    return left_parts[:length] == right_parts[:length]


def fsync_directory(path: Union[str, Path]) -> None:
    """Synchronize directory metadata; unsupported backends fail explicitly."""
    descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def durable_mkdir(path: Union[str, Path]) -> None:
    """Create missing directories and synchronize each new ancestor entry."""
    directory = Path(path)
    missing = []
    while not directory.exists():
        missing.append(directory)
        directory = directory.parent
    if not directory.is_dir():
        raise NotADirectoryError(str(directory))
    for directory in reversed(missing):
        directory.mkdir(exist_ok=True)
        fsync_directory(directory)
        fsync_directory(directory.parent)


def atomic_json(path: Union[str, Path], data: Any) -> Path:
    """Publish JSON with file+directory fsync; preserve primary errors on cleanup."""
    destination = canonical_entry(path)
    if destination.is_symlink():
        raise LifecycleError("unsafe_path", "refusing a symlink JSON destination", details={"path": str(destination)})
    durable_mkdir(destination.parent)
    # Encode before staging so invalid application data does not leave artifacts.
    encoded = canonical_json(data) + "\n"
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=str(destination.parent), prefix="." + destination.name + ".", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(destination))
        fsync_directory(destination.parent)
    except BaseException:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass
        raise
    return destination
