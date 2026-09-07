"""Conservative legacy-write guards for *recognized* managed paths.

This is not a filesystem sandbox or global ownership index. Inspection is
limited to explicit paths, their ancestors and the current/explicit project.
A drifted ordinary file registered only in an unrelated project cannot be
recognized without that context. Managed verification remains the authority.
"""

from pathlib import Path

from .common import LifecycleError, canonical_entry, path_components, paths_overlap as _overlap
from .repository import Repository


class ManagedBoundaryError(LifecycleError):
    def __init__(self, message, *, path=None, cause=None):
        details = {}
        if path is not None:
            details["path"] = str(path)
        if cause is not None:
            details["cause"] = getattr(cause, "code", "io_error")
        super().__init__("managed_boundary", message + " Use a managed lifecycle plan and explicit approval; migrate only from clean legacy evidence.", details=details)


def _variants(path):
    lexical = canonical_entry(path)
    try:
        resolved = lexical.resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise ManagedBoundaryError("Cannot safely classify this legacy write path.", path=lexical, cause=error) from error
    return {lexical, resolved}


def _inside_reserved_state(path):
    parts = path_components(path)
    return any(parts[index:index + 2] == (".skills-auditor-local", "lifecycle") for index in range(len(parts) - 1))


def assert_legacy_mutation_allowed(paths, *, project_root=None):
    """Check the whole explicit mutation set before any legacy side effect."""
    inspected = set()
    roots = {Path(project_root).resolve() if project_root is not None else Path.cwd().resolve()}
    for raw in paths:
        for path in _variants(raw):
            if _inside_reserved_state(path):
                raise ManagedBoundaryError("Legacy mutation of managed state or snapshot content is unsupported.", path=path)
            inspected.add(path)
            roots.add(path)
            roots.update(path.parents)
    if not inspected:
        return
    for root in sorted(roots, key=str):
        state = root / ".skills-auditor-local" / "lifecycle"
        if not state.exists() and not state.is_symlink():
            continue
        for path in inspected:
            if _overlap(path, state):
                raise ManagedBoundaryError("Legacy mutation contains a managed state directory.", path=path)
        try:
            with Repository(state, create=False) as repository:
                installations = repository.list("installation")
                for record in installations:
                    installation = record["data"]
                    if installation.get("state") == "uninstalled":
                        continue
                    target = installation.get("target")
                    if not isinstance(target, str) or not Path(target).is_absolute():
                        raise ValueError("managed installation target is malformed")
                    for boundary in _variants(target):
                        for path in inspected:
                            if _overlap(path, boundary):
                                raise ManagedBoundaryError("Legacy mutation overlaps a managed installation entry.", path=path)
        except ManagedBoundaryError:
            raise
        except (LifecycleError, OSError, ValueError, TypeError) as error:
            raise ManagedBoundaryError("An existing managed registry cannot be validated; refusing legacy mutation.", path=state, cause=error) from error
