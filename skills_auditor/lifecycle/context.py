"""Owner-scoped navigation metadata, never approval or historical authority."""

import os
from pathlib import Path
import shlex

from .common import LifecycleError


def _path(value):
    try:
        value = os.fspath(value)
    except TypeError as error:
        raise LifecycleError("invalid_context", "Project context must be a bounded filesystem path.", exit_code=2) from error
    if (not isinstance(value, str) or not value or len(value) > 4096
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise LifecycleError("invalid_context", "Project context must be a bounded filesystem path.", exit_code=2)
    return value


def project_context(project_root, *, verified=False):
    """A requested path can be known even when its repository is unavailable."""
    if type(verified) is not bool:
        raise LifecycleError("invalid_context", "Context verification must be a boolean.", exit_code=2)
    if project_root is None:
        return {"project_root": None, "context_verified": False}
    value = _path(project_root)
    try:
        root = str(Path(value).resolve())
    except (OSError, RuntimeError, ValueError) as error:
        raise LifecycleError("invalid_context", "Project context cannot be resolved safely.", exit_code=2) from error
    return {"project_root": root, "context_verified": verified}


def manager_context(manager):
    """Check an opened owner's binding, without rebinding or claiming a lease.

    State-directory identity is the Manager's existing point-in-time proof.
    Neither it nor canonical path equality prevents later external changes.
    """
    bound = None
    try:
        bound = _path(manager.project_root)
        if str(Path(bound).resolve()) != bound or not callable(getattr(manager, "_state_safety", None)):
            raise ValueError("opened owner path changed or lacks binding proof")
        manager._state_safety()
    except (LifecycleError, OSError, RuntimeError, ValueError, TypeError, AttributeError) as error:
        raise LifecycleError("project_context_changed", "The opened project context cannot be verified. No navigation is issued; preserve both locations and reopen the intended project explicitly.",
                             details={"project_root": bound, "context_verified": False}) from error
    return {"project_root": bound, "context_verified": True}


def action(project_root, arguments, *, required_inputs=()):
    """Render concrete argv, or an explicitly non-executable input template.

    Callers supply option terminators before option-like positional IDs. No
    shell evaluation, filesystem access through references, or approval occurs.
    """
    if (not isinstance(arguments, (list, tuple)) or len(arguments) > 50
            or any(not isinstance(value, str) or not value or len(value) > 4096
                   or any(ord(char) < 32 or ord(char) == 127 for char in value) for value in arguments)
            or not isinstance(required_inputs, (tuple, list))
            or any(value not in {"project_root", "installation_id", "source", "target"} for value in required_inputs)):
        raise LifecycleError("invalid_context", "Navigation arguments must be bounded and explicit.", exit_code=2)
    missing = list(dict.fromkeys(required_inputs))
    if project_root is not None:
        project_root = _path(project_root)
    if project_root is None and "project_root" not in missing:
        missing.insert(0, "project_root")
    argv = ["skills-audit", "lifecycle", "--project-root", project_root or "<project-root>", *arguments]
    return {"command": " ".join(shlex.quote(value) for value in argv),
            "argv": None if missing else argv, "required_inputs": missing}
