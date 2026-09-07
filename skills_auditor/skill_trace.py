"""Opt-in controls and read-only runtime checks for the Skill Trace plugin.

This module never edits Codex hook trust or the host-wide hook feature flag.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


ENABLE_ENV = "SKILLS_AUDITOR_SKILL_TRACE"
SETTINGS_ENV = "SKILLS_AUDITOR_SKILL_TRACE_CONFIG"
MAX_TAIL_BYTES = 256 * 1024
MAX_SETTINGS_BYTES = 64 * 1024
MAX_AGE_SECONDS = 300
REPAIR_HINT = (
    "In Codex CLI /hooks, review and enable only skill-trace@skills-auditor-local hooks; "
    "restart the task after plugin updates. Check the plugin script/interpreter and log "
    "directory if no events arrive. See docs/install.md#optional-skill-trace-plugin."
)


def settings_path() -> Path:
    override = os.environ.get(SETTINGS_ENV)
    if override:
        return Path(override).expanduser()
    codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
    return codex_home / "skill-trace.json"


def read_settings() -> dict:
    path = settings_path()
    result = {"enabled": False, "source": "default", "settings_path": str(path), "updated_at": ""}
    override = os.environ.get(ENABLE_ENV)
    if override is not None:
        if override not in {"0", "1"}:
            raise ValueError(f"{ENABLE_ENV} must be 0 or 1")
        result.update(enabled=override == "1", source="environment")
        return result
    data = _read_regular_file(path, MAX_SETTINGS_BYTES)
    if data is None:
        return result
    value = json.loads(data.decode("utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != 1 or type(value.get("enabled")) is not bool:
        raise ValueError(f"Invalid Skill Trace settings: {path}")
    updated_at = value.get("updated_at", "")
    if not isinstance(updated_at, str) or not parse_time(updated_at):
        raise ValueError(f"Invalid Skill Trace settings timestamp: {path}")
    result.update(enabled=value["enabled"], source="file", updated_at=updated_at)
    return result


def set_enabled(enabled: bool) -> Path:
    """Persist only this plugin's preference, atomically, outside Codex config.toml."""
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    value = {"schema_version": 1, "enabled": enabled, "updated_at": datetime.now(timezone.utc).isoformat()}
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, indent=2)
            stream.write("\n")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path


def parse_time(value: str) -> Optional[datetime]:
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp if stamp.tzinfo else None
    except (ValueError, AttributeError):
        return None


def log_root(cwd: Path, value: Optional[str] = None) -> Path:
    root = Path(value or os.environ.get("SKILLS_AUDITOR_LOG_DIR") or ".skills-auditor-local").expanduser()
    return root if root.is_absolute() else cwd / root


def _read_regular_file(path: Path, limit: int, *, tail: bool = False) -> Optional[bytes]:
    """Bound diagnostic input without waiting for a FIFO writer.

    Validate the opened descriptor, allowing symlinks to regular files. The
    byte bound is not a deadline for a slow or unresponsive filesystem.
    """
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
    except FileNotFoundError:
        return None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"Skill Trace input must be a regular file: {path}")
        if not tail and metadata.st_size > limit:
            raise ValueError(f"Skill Trace input exceeds {limit} bytes: {path}")
        start = max(0, metadata.st_size - limit) if tail else 0
        if start:
            os.lseek(descriptor, start, os.SEEK_SET)
        # An extra settings byte detects growth after fstat. Sensor reads and
        # partial-line discard share one bound, even during concurrent appends.
        remaining = limit if tail else limit + 1
        chunks = []
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if not tail and len(data) > limit:
            raise ValueError(f"Skill Trace input exceeds {limit} bytes: {path}")
        return data.partition(b"\n")[2] if start else data
    finally:
        os.close(descriptor)


def _tail_events(path: Path):
    """Bound preflight cost; incomplete appends are ignored, never repaired here."""
    data = _read_regular_file(path, MAX_TAIL_BYTES, tail=True)
    if data is None:
        return
    for line in data.splitlines():
        try:
            value = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if isinstance(value, dict):
            yield value


def check_health(log_dir: Optional[str] = None, session_id: Optional[str] = None) -> dict:
    """Look for recent plugin Pre/PostToolUse writes in this task and workspace.

    Never invoke a synthetic hook or manufacture a health event. A positive result
    is recent runtime evidence, not authoritative inspection of Codex trust state.
    """
    session = session_id or os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_SESSION_ID") or ""
    result = {"status": "unverified", "session_id": session, "log_dir": "", "observed_hooks": {}}
    try:
        cwd = Path.cwd().resolve()
        root = log_root(cwd, log_dir)
        result["log_dir"] = str(root)
        settings = read_settings()
        result.update(settings)
        if not settings["enabled"]:
            result.update(status="disabled", detail="Skill Trace capture is off; ordinary auditing remains available.")
            return result
        if not session:
            result["detail"] = "No current Codex task id; run this check inside the task or pass --session-id."
            return result
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(seconds=MAX_AGE_SECONDS)
        updated = parse_time(settings["updated_at"])
        if updated:
            cutoff = max(cutoff, updated)
        latest = {}
        for day in (now - timedelta(days=1), now):
            path = root / "sensors" / day.date().isoformat() / "codex.jsonl"
            for event in _tail_events(path):
                metadata = event.get("metadata")
                if not isinstance(metadata, dict) or metadata.get("skill_trace") != 1:
                    continue
                if event.get("provider") != "codex" or event.get("source") != "hook" or event.get("session_id") != session:
                    continue
                event_cwd = event.get("cwd")
                if not isinstance(event_cwd, str) or not event_cwd or Path(event_cwd).resolve() != cwd:
                    continue
                hook = metadata.get("hook_event_name")
                stamp = parse_time(event.get("timestamp", ""))
                if not isinstance(hook, str) or hook not in {"SessionStart", "PreToolUse", "PostToolUse", "Stop"} or not stamp or stamp > now:
                    continue
                if hook not in latest or stamp > latest[hook]:
                    latest[hook] = stamp
        result["observed_hooks"] = {key: value.isoformat() for key, value in latest.items()}
        recent = {key for key, value in latest.items() if value >= cutoff}
        if {"PreToolUse", "PostToolUse"} <= recent:
            result.update(status="healthy", detail="Recent plugin PreToolUse and PostToolUse writes observed in this task.")
        else:
            result["status"] = "stale" if latest else "unverified"
            result["detail"] = "No recent complete tool-hook evidence for this task. " + REPAIR_HINT
    except (OSError, ValueError, RuntimeError) as exc:
        result.update(status="error", detail=f"Skill Trace check failed: {exc}")
    return result


def preflight_warning(log_dir: Optional[str] = None) -> None:
    """Run before ordinary CLI work in Codex; preserve stdout and command exit status."""
    if not (os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_SESSION_ID")):
        return
    result = check_health(log_dir)
    if result["status"] not in {"healthy", "disabled"}:
        print(f"Skill Trace preflight [{result['status']}]: {result['detail']}", file=sys.stderr)


def run_control(args) -> int:
    if args.action in {"enable", "disable"}:
        try:
            set_enabled(args.action == "enable")
        except OSError as exc:
            print(f"Skill Trace settings failed: {exc}", file=sys.stderr)
            return 2
    result = check_health(args.log_dir, args.session_id)
    if args.format == "json":
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"Skill Trace: {result['status']} ({result.get('source', 'unknown')} setting)")
        print(result["detail"])
        print(f"Settings: {settings_path()}")
        if args.action in {"enable", "disable"} and ENABLE_ENV in os.environ:
            print(f"{ENABLE_ENV} overrides the saved preference in this process.")
    if result["status"] == "error":
        return 2
    return int(args.action == "check" and result["status"] not in {"healthy", "disabled"})
