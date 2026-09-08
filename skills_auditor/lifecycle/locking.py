"""Bounded advisory locks shared by all stores targeting the same entry.

Locks live beside the canonical target leaf, are acquired in sorted order and
are never unlinked. All cooperating writers must use this protocol. A hostile
actor can still replace a directory or lock file; advisory locks do not promise
protection against privileged or noncooperating filesystem mutation.
"""

from contextlib import contextmanager
import errno
import hashlib
import math
import os
from pathlib import Path
import stat
import time
import unicodedata
from typing import Iterable, Union

from .common import LifecycleError, canonical_entry

try:
    import fcntl
except ImportError:  # Supported deployments are Linux/macOS.
    fcntl = None


@contextmanager
def locked_paths(paths: Iterable[Union[str, Path]], *, timeout: float = 0.0):
    """Yield sorted canonical entries after acquiring persistent flock locks.

    Target parents must already exist. This context never creates an unmanaged
    host directory as a side effect of merely preparing a transaction.
    """
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError("lock timeout must be a finite nonnegative duration")
    if fcntl is None or not hasattr(os, "O_NOFOLLOW"):
        raise LifecycleError("locking_unsupported", "local POSIX flock and O_NOFOLLOW are required")
    entries = tuple(sorted({canonical_entry(path) for path in paths}, key=str))
    descriptors = []
    deadline = time.monotonic() + timeout
    try:
        locks = {}
        for entry in entries:
            if not entry.parent.is_dir():
                raise LifecycleError("lock_parent_missing", "target parent must already be a directory", details={"path": str(entry.parent)})
            # macOS may alias case and Unicode normalization. Conservatively
            # serialize these names on Linux too rather than split one target's
            # lock into different inodes. Directory identity also deduplicates
            # parent aliases without relying on their textual spelling.
            leaf = unicodedata.normalize("NFD", entry.name).casefold()
            leaf_hash = hashlib.sha256(os.fsencode(leaf)).hexdigest()
            lock = entry.parent / (".skills-auditor-lock-" + leaf_hash + ".lock")
            parent = entry.parent.stat()
            locks[(parent.st_dev, parent.st_ino, leaf_hash)] = (entry, lock)
        for _, (entry, lock) in sorted(locks.items()):
            try:
                descriptor = os.open(str(lock), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), 0o600)
            except OSError as exc:
                code = "unsafe_lock" if exc.errno in (errno.ELOOP, errno.EISDIR) else "lock_io_error"
                raise LifecycleError(code, "could not safely open the target lock", details={"path": str(lock), "error": str(exc)}) from exc
            descriptors.append(descriptor)
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise LifecycleError("unsafe_lock", "lock must be an unaliased regular file", details={"path": str(lock)})
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EAGAIN, errno.EACCES):
                        raise LifecycleError("lock_io_error", "could not acquire target lock", details={"path": str(lock), "error": str(exc)}) from exc
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise LifecycleError("lock_contended", "another lifecycle writer holds the target lock", details={"path": str(entry)}) from exc
                    time.sleep(min(remaining, 0.01))
            current = lock.lstat()
            if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino) or not stat.S_ISREG(current.st_mode):
                raise LifecycleError("unsafe_lock", "lock entry changed while acquiring it", details={"path": str(lock)})
        yield entries
    finally:
        # Closing releases flock even after process death; persistent filenames
        # avoid the inode-split race caused by unlinking locks at release time.
        for descriptor in reversed(descriptors):
            os.close(descriptor)
