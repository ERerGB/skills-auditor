"""Local trigger and observability logs for Skills Auditor.

These logs sit above the route state-machine trace. They record prompt-level
skill trigger decisions, observability trigger decisions, and references to
route traces without storing raw prompts by default.
"""

from __future__ import annotations

import hashlib
import json
import shlex
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


DEFAULT_LOCAL_LOG_DIR = Path(".skills-auditor-local")
ALLOWED_LOG_KINDS = frozenset({"skill_trigger", "observability_trigger", "trace"})
ALLOWED_VERDICTS = frozenset(
    {"", "unknown", "correct", "incorrect", "false_positive", "false_negative", "ambiguous"}
)
ALLOWED_SENSOR_EVENT_TYPES = frozenset(
    {
        "unknown",
        "user_prompt",
        "session_start",
        "session_end",
        "pre_tool_use",
        "post_tool_use",
        "stop",
        "tool_call",
        "tool_result",
        "file_access",
        "skill_file_access",
    }
)
PATH_KEYS = ("file_path", "path", "notebook_path")
COMMAND_KEYS = ("command", "cmd")
SHELL_READ_COMMANDS = frozenset({"cat", "head", "tail", "sed", "nl", "wc"})
SHELL_CONTROL_TOKENS = frozenset({"|", "||", "&&", ";", ">", ">>", "<", "2>", "2>>"})


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_kind(kind: str) -> str:
    return kind.strip().lower().replace("-", "_")


@dataclass
class TriggerLogEvent:
    """One append-only prompt-level observability event."""

    kind: str
    source: str = "manual"
    prompt_id: str = ""
    prompt_hash: str = ""
    prompt_summary: str = ""
    context_summary: str = ""
    skill: str = ""
    expected_skill: str = ""
    actual_skill: str = ""
    expected_mode: str = ""
    actual_mode: str = ""
    decision: str = ""
    confidence: Optional[float] = None
    verdict: str = "unknown"
    trace_path: str = ""
    notes: str = ""
    schema_version: int = 1
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = field(default_factory=utc_timestamp)

    def __post_init__(self) -> None:
        self.kind = normalize_kind(self.kind)
        self.verdict = self.verdict.strip().lower().replace("-", "_") if self.verdict else ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SensorEvent:
    """One raw-ish agent runtime event normalized by a sensor adapter.

    This is the bottom "sensor" layer: it records what a host exposed through
    hooks/transcripts without claiming whether a skill was semantically used.
    """

    provider: str
    event_type: str
    source: str = "hook"
    session_id: str = ""
    cwd: str = ""
    tool_name: str = ""
    operation: str = ""
    path: str = ""
    realpath: str = ""
    content_hash: str = ""
    skill_name: str = ""
    skill_path: str = ""
    transcript_path: str = ""
    call_id: str = ""
    status: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    schema_version: int = 1
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = field(default_factory=utc_timestamp)

    def __post_init__(self) -> None:
        self.provider = self.provider.strip().lower().replace("_", "-") or "unknown"
        self.event_type = normalize_kind(self.event_type or "unknown")
        self.source = normalize_kind(self.source or "hook")

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class LogAuditFinding:
    check: str
    severity: str  # error | warning | info
    detail: str
    event_id: str = ""
    path: str = ""


@dataclass
class LogSummary:
    total_events: int
    by_kind: Dict[str, int]
    by_verdict: Dict[str, int]
    labeled_events: int
    correct_events: int
    false_positive_events: int
    false_negative_events: int

    @property
    def accuracy(self) -> Optional[float]:
        if self.labeled_events == 0:
            return None
        return self.correct_events / self.labeled_events


@dataclass
class StorageScopeStats:
    label: str
    path: str
    exists: bool
    file_count: int
    total_bytes: int
    record_count: int
    average_file_bytes: float
    average_record_bytes: float


@dataclass
class SensorClaim:
    """Aggregated evidence-backed claim derived from sensor events."""

    claim_id: str
    claim_type: str
    provider: str
    session_id: str = ""
    call_id: str = ""
    operation: str = ""
    path: str = ""
    realpath: str = ""
    content_hash: str = ""
    skill_name: str = ""
    skill_path: str = ""
    evidence_event_ids: List[str] = field(default_factory=list)
    evidence_sources: List[str] = field(default_factory=list)
    confidence: str = "weak"
    score: float = 0.40
    status: str = "supported"
    notes: List[str] = field(default_factory=list)
    schema_version: int = 1

    def to_dict(self) -> dict:
        return asdict(self)


def _event_date(timestamp: str) -> str:
    if len(timestamp) >= 10:
        return timestamp[:10]
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def event_log_path(log_dir: Path, event: TriggerLogEvent) -> Path:
    return log_dir / "logs" / _event_date(event.timestamp) / f"{event.kind}.jsonl"


def sensor_log_path(log_dir: Path, event: SensorEvent) -> Path:
    return log_dir / "sensors" / _event_date(event.timestamp) / f"{event.provider}.jsonl"


def write_trigger_log(event: TriggerLogEvent, log_dir: Optional[Path] = None) -> Path:
    """Append one event to the repo-local JSONL log tree."""
    out_root = log_dir or DEFAULT_LOCAL_LOG_DIR
    out_path = event_log_path(out_root, event)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True))
        f.write("\n")
    return out_path


def write_sensor_event(event: SensorEvent, log_dir: Optional[Path] = None) -> Path:
    """Append one normalized sensor event to the repo-local JSONL log tree."""
    out_root = log_dir or DEFAULT_LOCAL_LOG_DIR
    out_path = sensor_log_path(out_root, event)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True))
        f.write("\n")
    return out_path


def _first_string(payload: dict, keys: Sequence[str]) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _nested_dict(payload: dict, key: str) -> dict:
    value = payload.get(key)
    return value if isinstance(value, dict) else {}


def _infer_event_type(raw_type: str, tool_name: str, path: str) -> str:
    event_type = normalize_kind(raw_type or "unknown")
    aliases = {
        "userpromptsubmit": "user_prompt",
        "user_prompt_submit": "user_prompt",
        "sessionstart": "session_start",
        "sessionend": "session_end",
        "pretooluse": "pre_tool_use",
        "posttooluse": "post_tool_use",
        "custom_tool_call": "tool_call",
        "function_call": "tool_call",
        "tool_call": "tool_call",
        "tool_result": "tool_result",
        "function_call_output": "tool_result",
        "custom_tool_call_output": "tool_result",
    }
    event_type = aliases.get(event_type, event_type)
    if tool_name and path:
        return "skill_file_access" if _looks_like_skill_path(path) else "file_access"
    return event_type if event_type in ALLOWED_SENSOR_EVENT_TYPES else "unknown"


def _infer_operation(tool_name: str, event_type: str) -> str:
    tool = tool_name.strip().lower()
    if tool in {"read", "view"}:
        return "read"
    if tool in {"write", "edit", "multiedit", "notebookedit"}:
        return "write"
    if tool in {"ls", "list"}:
        return "list"
    if tool in {"grep", "glob", "search"}:
        return "search"
    if tool in {"bash", "shell", "exec_command"}:
        return "command"
    if event_type.endswith("tool_use"):
        return "tool"
    return ""


def _extract_path(value: Any) -> str:
    if isinstance(value, dict):
        for key in PATH_KEYS:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
        for nested in value.values():
            candidate = _extract_path(nested)
            if candidate:
                return candidate
    elif isinstance(value, list):
        for item in value:
            candidate = _extract_path(item)
            if candidate:
                return candidate
    return ""


def _extract_command(value: Any) -> str:
    if isinstance(value, dict):
        for key in COMMAND_KEYS:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
        for nested in value.values():
            candidate = _extract_command(nested)
            if candidate:
                return candidate
    elif isinstance(value, list):
        for item in value:
            candidate = _extract_command(item)
            if candidate:
                return candidate
    return ""


def _command_path_candidate(token: str) -> bool:
    if not token or token.startswith("-"):
        return False
    if token.isdigit():
        return False
    if token in {".", ".."}:
        return False
    return "/" in token or Path(token).name == "SKILL.md"


def _extract_read_path_from_shell_command(command: str) -> Tuple[str, str]:
    """Extract the first path from a simple read-only shell command.

    This intentionally ignores compound shell syntax. The hook should capture
    a conservative signal, not attempt to audit arbitrary shell semantics.
    """
    if not command or any(token in command for token in ("`", "$(")):
        return "", ""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return "", ""
    if not tokens or any(token in SHELL_CONTROL_TOKENS for token in tokens):
        return "", ""
    verb = Path(tokens[0]).name
    if verb not in SHELL_READ_COMMANDS:
        return "", ""
    for token in tokens[1:]:
        if _command_path_candidate(token):
            return token, verb
    return "", ""


def _looks_like_skill_path(path: str) -> bool:
    parts = Path(path).parts
    return Path(path).name == "SKILL.md" or "skills" in parts or ".codex" in parts or ".claude" in parts


def _infer_skill_from_path(path: str) -> Tuple[str, str]:
    if not path:
        return "", ""
    p = Path(path)
    parts = p.parts
    if p.name == "SKILL.md":
        return p.parent.name, str(p)
    if "skills" in parts:
        idx = len(parts) - 1 - list(reversed(parts)).index("skills")
        if idx + 1 < len(parts):
            return parts[idx + 1], str(Path(*parts[: idx + 2]) / "SKILL.md")
    return "", ""


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def sensor_event_from_payload(
    payload: dict,
    provider: str,
    source: str = "hook",
    resolve_path: bool = False,
    hash_path: bool = False,
) -> SensorEvent:
    """Normalize a Claude/Codex/generic hook or transcript payload.

    The adapter intentionally stores only compact metadata. Full tool inputs or
    raw prompts should stay in the host transcript, not in this local ledger.
    """
    tool = _nested_dict(payload, "tool")
    tool_input = _nested_dict(payload, "tool_input") or _nested_dict(tool, "input")
    raw_type = _first_string(payload, ("hook_event_name", "event", "event_type", "type"))
    tool_name = _first_string(payload, ("tool_name", "name")) or _first_string(tool, ("name", "tool_name"))
    path = _extract_path(tool_input) or _extract_path(payload.get("input")) or _extract_path(payload)
    command_path = ""
    command_verb = ""
    if not path and tool_name.strip().lower() in {"bash", "shell", "exec_command"}:
        command = _extract_command(tool_input) or _extract_command(payload.get("input")) or _extract_command(payload)
        command_path, command_verb = _extract_read_path_from_shell_command(command)
        path = command_path
    event_type = _infer_event_type(raw_type, tool_name, path)
    skill_name, skill_path = _infer_skill_from_path(path)
    realpath = ""
    content_hash = ""
    if path and (resolve_path or hash_path):
        expanded = Path(path).expanduser()
        if not expanded.is_absolute():
            cwd = _first_string(payload, ("cwd", "working_dir"))
            expanded = Path(cwd).expanduser() / expanded if cwd else expanded
        if resolve_path and expanded.exists():
            realpath = str(expanded.resolve())
        if hash_path and expanded.is_file():
            content_hash = _sha256_file(expanded)
    metadata: Dict[str, Any] = {}
    for key in ("model", "turn_id", "message_id", "parent_id"):
        value = payload.get(key)
        if isinstance(value, (str, int, float, bool)):
            metadata[key] = value
    if payload.get("tool_response") is not None or payload.get("result") is not None:
        metadata["has_tool_response"] = True
    if command_path:
        metadata["command_verb"] = command_verb
        metadata["path_source"] = "shell_command"

    return SensorEvent(
        provider=provider,
        event_type=event_type,
        source=source,
        session_id=_first_string(payload, ("session_id", "sessionId", "conversation_id")),
        cwd=_first_string(payload, ("cwd", "working_dir")),
        tool_name=tool_name,
        operation="read" if command_path else _infer_operation(tool_name, event_type),
        path=path,
        realpath=realpath,
        content_hash=content_hash,
        skill_name=skill_name,
        skill_path=skill_path,
        transcript_path=_first_string(payload, ("transcript_path", "transcriptPath")),
        call_id=_first_string(payload, ("tool_call_id", "call_id", "id")),
        status=_first_string(payload, ("status", "outcome")),
        metadata=metadata,
    )


def load_trigger_logs(
    log_dir: Optional[Path] = None,
    kind: str = "",
) -> Tuple[List[dict], List[LogAuditFinding]]:
    """Load JSONL events and return parse findings separately."""
    src = log_dir or DEFAULT_LOCAL_LOG_DIR
    wanted_kind = normalize_kind(kind) if kind else ""
    events: List[dict] = []
    findings: List[LogAuditFinding] = []
    if not src.exists():
        return events, findings
    for path in sorted((src / "logs").glob("**/*.jsonl")):
        if wanted_kind and path.stem != wanted_kind:
            continue
        with path.open("r", encoding="utf-8") as f:
            for lineno, raw in enumerate(f, start=1):
                line = raw.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:
                    findings.append(
                        LogAuditFinding(
                            check="invalid_json",
                            severity="error",
                            detail=f"{path}:{lineno}: {exc}",
                            path=str(path),
                        )
                    )
                    continue
                event["_log_path"] = str(path)
                events.append(event)
    return events, findings


def load_sensor_events(
    log_dir: Optional[Path] = None,
    provider: str = "",
) -> Tuple[List[dict], List[LogAuditFinding]]:
    """Load sensor JSONL events and return parse findings separately."""
    src = log_dir or DEFAULT_LOCAL_LOG_DIR
    wanted_provider = provider.strip().lower().replace("_", "-") if provider else ""
    events: List[dict] = []
    findings: List[LogAuditFinding] = []
    if not src.exists():
        return events, findings
    for path in sorted((src / "sensors").glob("**/*.jsonl")):
        if wanted_provider and path.stem != wanted_provider:
            continue
        with path.open("r", encoding="utf-8") as f:
            for lineno, raw in enumerate(f, start=1):
                line = raw.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:
                    findings.append(LogAuditFinding(
                        check="invalid_json",
                        severity="error",
                        detail=f"{path}:{lineno}: {exc}",
                        path=str(path),
                    ))
                    continue
                event["_log_path"] = str(path)
                events.append(event)
    return events, findings


def audit_sensor_events(events: Sequence[dict]) -> List[LogAuditFinding]:
    findings: List[LogAuditFinding] = []
    seen_ids = set()
    for event in events:
        event_id = str(event.get("event_id", ""))
        path = str(event.get("_log_path", ""))
        event_type = normalize_kind(str(event.get("event_type", "")))

        if event_id:
            if event_id in seen_ids:
                findings.append(LogAuditFinding(
                    check="duplicate_event_id",
                    severity="error",
                    detail=f"Duplicate event_id: {event_id}",
                    event_id=event_id,
                    path=path,
                ))
            seen_ids.add(event_id)

        if not event.get("provider"):
            findings.append(LogAuditFinding(
                check="missing_provider",
                severity="error",
                detail="Sensor event is missing provider.",
                event_id=event_id,
                path=path,
            ))

        if event_type not in ALLOWED_SENSOR_EVENT_TYPES:
            findings.append(LogAuditFinding(
                check="unknown_sensor_event_type",
                severity="warning",
                detail=f"Unknown sensor event_type: {event_type}",
                event_id=event_id,
                path=path,
            ))

        if event_type in {"file_access", "skill_file_access"} and not event.get("path"):
            findings.append(LogAuditFinding(
                check="missing_access_path",
                severity="warning",
                detail="File access sensor events should include path.",
                event_id=event_id,
                path=path,
            ))

        if "raw_prompt" in event and event.get("raw_prompt"):
            findings.append(LogAuditFinding(
                check="raw_prompt_present",
                severity="warning",
                detail="Sensor event stores raw_prompt; keep raw prompts in host transcripts.",
                event_id=event_id,
                path=path,
            ))
    return findings


def _non_empty_values(events: Sequence[dict], key: str) -> List[str]:
    values: List[str] = []
    seen = set()
    for event in events:
        value = str(event.get(key, "") or "")
        if value and value not in seen:
            values.append(value)
            seen.add(value)
    return values


def _claim_group_key(event: dict) -> Tuple[str, str, str, str, str, str]:
    provider = str(event.get("provider", "") or "unknown").strip().lower().replace("_", "-")
    session_id = str(event.get("session_id", "") or "")
    call_id = str(event.get("call_id", "") or "")
    operation = str(event.get("operation", "") or "")
    path = str(event.get("realpath") or event.get("path") or "")
    skill_name = str(event.get("skill_name", "") or "")
    if call_id:
        return provider, session_id, call_id, "", "", ""
    return provider, session_id, "", operation, path, skill_name


def _claim_id_for(group_key: Tuple[str, str, str, str, str, str]) -> str:
    raw = json.dumps(group_key, ensure_ascii=False, sort_keys=True)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _confidence_for_sources(sources: Sequence[str], disputed: bool) -> Tuple[str, float, str]:
    source_set = set(sources)
    if disputed:
        return "disputed", 0.10, "disputed"
    if "hook" in source_set and "transcript" in source_set:
        return "strong", 0.95, "supported"
    if "hook" in source_set or "transcript" in source_set:
        return "medium", 0.70, "supported"
    if source_set == {"manual"} or ("manual" in source_set and len(source_set) == 1):
        return "manual", 0.30, "supported"
    if "fs_proxy" in source_set:
        return "weak", 0.40, "supported"
    return "weak", 0.40, "supported"


def _detect_claim_conflicts(events: Sequence[dict]) -> List[str]:
    notes: List[str] = []
    for key in ("content_hash", "realpath", "operation", "path", "skill_name"):
        values = _non_empty_values(events, key)
        if len(values) > 1:
            notes.append(f"conflicting {key}: {values}")
    return notes


def aggregate_sensor_claims(events: Sequence[dict]) -> List[SensorClaim]:
    """Aggregate raw sensor events into evidence-backed claims.

    The function is intentionally conservative: it does not assert semantic
    invocation, only that the available sensor evidence supports an access claim.
    """
    groups: Dict[Tuple[str, str, str, str, str, str], List[dict]] = {}
    for event in events:
        event_type = normalize_kind(str(event.get("event_type", "") or ""))
        if event_type not in {"skill_file_access", "file_access"}:
            continue
        groups.setdefault(_claim_group_key(event), []).append(event)

    claims: List[SensorClaim] = []
    for group_key, group_events in sorted(groups.items(), key=lambda item: item[0]):
        provider = group_key[0]
        event_ids = _non_empty_values(group_events, "event_id")
        sources = sorted(_non_empty_values(group_events, "source"))
        notes = _detect_claim_conflicts(group_events)
        confidence, score, status = _confidence_for_sources(sources, disputed=bool(notes))

        paths = _non_empty_values(group_events, "path")
        realpaths = _non_empty_values(group_events, "realpath")
        hashes = _non_empty_values(group_events, "content_hash")
        skills = _non_empty_values(group_events, "skill_name")
        skill_paths = _non_empty_values(group_events, "skill_path")
        operations = _non_empty_values(group_events, "operation")
        sessions = _non_empty_values(group_events, "session_id")
        call_ids = _non_empty_values(group_events, "call_id")

        claims.append(SensorClaim(
            claim_id=_claim_id_for(group_key),
            claim_type="skill_file_access" if skills or skill_paths else "file_access",
            provider=provider,
            session_id=sessions[0] if sessions else "",
            call_id=call_ids[0] if call_ids else "",
            operation=operations[0] if operations else "",
            path=paths[0] if paths else "",
            realpath=realpaths[0] if realpaths else "",
            content_hash=hashes[0] if hashes else "",
            skill_name=skills[0] if skills else "",
            skill_path=skill_paths[0] if skill_paths else "",
            evidence_event_ids=event_ids,
            evidence_sources=sources,
            confidence=confidence,
            score=score,
            status=status,
            notes=notes,
        ))
    return claims


def summarize_trigger_logs(events: Sequence[dict]) -> LogSummary:
    by_kind: Dict[str, int] = {}
    by_verdict: Dict[str, int] = {}
    for event in events:
        kind = normalize_kind(str(event.get("kind", "")))
        verdict = str(event.get("verdict", "") or "unknown").strip().lower().replace("-", "_")
        by_kind[kind] = by_kind.get(kind, 0) + 1
        by_verdict[verdict] = by_verdict.get(verdict, 0) + 1

    correct = by_verdict.get("correct", 0)
    incorrect = by_verdict.get("incorrect", 0)
    false_positive = by_verdict.get("false_positive", 0)
    false_negative = by_verdict.get("false_negative", 0)
    labeled = correct + incorrect + false_positive + false_negative

    return LogSummary(
        total_events=len(events),
        by_kind=by_kind,
        by_verdict=by_verdict,
        labeled_events=labeled,
        correct_events=correct,
        false_positive_events=false_positive,
        false_negative_events=false_negative,
    )


def audit_trigger_logs(events: Sequence[dict]) -> List[LogAuditFinding]:
    findings: List[LogAuditFinding] = []
    seen_ids = set()
    for event in events:
        event_id = str(event.get("event_id", ""))
        path = str(event.get("_log_path", ""))
        kind = normalize_kind(str(event.get("kind", "")))
        verdict = str(event.get("verdict", "") or "").strip().lower().replace("-", "_")

        if event_id:
            if event_id in seen_ids:
                findings.append(LogAuditFinding(
                    check="duplicate_event_id",
                    severity="error",
                    detail=f"Duplicate event_id: {event_id}",
                    event_id=event_id,
                    path=path,
                ))
            seen_ids.add(event_id)

        if kind not in ALLOWED_LOG_KINDS:
            findings.append(LogAuditFinding(
                check="unknown_kind",
                severity="error",
                detail=f"Unknown log kind: {kind}",
                event_id=event_id,
                path=path,
            ))

        if verdict not in ALLOWED_VERDICTS:
            findings.append(LogAuditFinding(
                check="unknown_verdict",
                severity="warning",
                detail=f"Unknown verdict: {verdict}",
                event_id=event_id,
                path=path,
            ))

        if "raw_prompt" in event and event.get("raw_prompt"):
            findings.append(LogAuditFinding(
                check="raw_prompt_present",
                severity="warning",
                detail="Log event stores raw_prompt; prefer prompt_hash plus summary fields.",
                event_id=event_id,
                path=path,
            ))

        has_prompt_reference = bool(
            event.get("prompt_hash") or event.get("prompt_summary") or event.get("context_summary")
        )
        if kind in {"skill_trigger", "observability_trigger"} and not has_prompt_reference:
            findings.append(LogAuditFinding(
                check="missing_prompt_reference",
                severity="warning",
                detail="Trigger logs should include prompt_hash, prompt_summary, or context_summary.",
                event_id=event_id,
                path=path,
            ))

        has_skill_reference = bool(event.get("skill") or event.get("expected_skill") or event.get("actual_skill"))
        if kind == "skill_trigger" and not has_skill_reference:
            findings.append(LogAuditFinding(
                check="missing_skill_reference",
                severity="warning",
                detail="Skill trigger logs should include skill, expected_skill, or actual_skill.",
                event_id=event_id,
                path=path,
            ))

        if kind == "trace" and not event.get("trace_path"):
            findings.append(LogAuditFinding(
                check="missing_trace_path",
                severity="warning",
                detail="Trace log entries should reference the trace_path being observed.",
                event_id=event_id,
                path=path,
            ))

    return findings


def collect_storage_stats(scopes: Iterable[Tuple[str, Path]]) -> List[StorageScopeStats]:
    stats: List[StorageScopeStats] = []
    for label, path in scopes:
        expanded = path.expanduser()
        if not expanded.exists():
            stats.append(StorageScopeStats(
                label=label,
                path=str(expanded),
                exists=False,
                file_count=0,
                total_bytes=0,
                record_count=0,
                average_file_bytes=0.0,
                average_record_bytes=0.0,
            ))
            continue

        files = [p for p in expanded.rglob("*") if p.is_file()]
        total_bytes = sum(p.stat().st_size for p in files)
        record_count = 0
        for p in files:
            if p.suffix == ".jsonl":
                with p.open("r", encoding="utf-8", errors="replace") as f:
                    record_count += sum(1 for line in f if line.strip())
            elif p.suffix == ".json":
                record_count += 1
        stats.append(StorageScopeStats(
            label=label,
            path=str(expanded),
            exists=True,
            file_count=len(files),
            total_bytes=total_bytes,
            record_count=record_count,
            average_file_bytes=(total_bytes / len(files)) if files else 0.0,
            average_record_bytes=(total_bytes / record_count) if record_count else 0.0,
        ))
    return stats


def estimate_retention_bytes(
    average_record_bytes: float,
    events_per_day: float,
    retention_days: int,
    index_multiplier: float,
) -> float:
    return average_record_bytes * events_per_day * retention_days * (1.0 + index_multiplier)
