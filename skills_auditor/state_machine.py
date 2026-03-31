"""Select-One Routing state machine for skill variant lifecycle.

Each skill identity (same frontmatter ``name:``) may have multiple variants
(primary, .agents/ copy, .factory/ copy). This module defines the states,
legal transitions, classification signals, and trace data structures that
track every variant through:

    DISCOVERED → CLASSIFIED → ROUTED → RESOLVED

Every transition is recorded so that batch auditing can verify the state
machine is strict and complete.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Set


# ── States ───────────────────────────────────────────────────────────────

class VariantState(Enum):
    # Discovery phase
    DISCOVERED = "discovered"
    TRUE_DUPLICATE = "true_duplicate"
    VARIANT_DETECTED = "variant_detected"
    # Classification phase
    CLASSIFIED = "classified"
    UNROUTABLE = "unroutable"
    # Routing phase (select-one)
    SELECTED = "selected"
    SUPERSEDED = "superseded"
    # Terminal (resolve) phase
    ACTIVE = "active"
    ARCHIVED = "archived"
    DELETED = "deleted"
    KEPT_HIDDEN = "kept_hidden"
    FLAGGED = "flagged"


TRANSITIONS: Dict[VariantState, List[VariantState]] = {
    VariantState.DISCOVERED:        [VariantState.TRUE_DUPLICATE, VariantState.VARIANT_DETECTED],
    VariantState.TRUE_DUPLICATE:    [VariantState.SELECTED, VariantState.SUPERSEDED],
    VariantState.VARIANT_DETECTED:  [VariantState.CLASSIFIED, VariantState.UNROUTABLE],
    VariantState.CLASSIFIED:        [VariantState.SELECTED, VariantState.SUPERSEDED],
    VariantState.UNROUTABLE:        [VariantState.FLAGGED],
    VariantState.SELECTED:          [VariantState.ACTIVE],
    VariantState.SUPERSEDED:        [VariantState.ARCHIVED, VariantState.DELETED, VariantState.KEPT_HIDDEN],
    # Terminal — no outgoing transitions
    VariantState.ACTIVE:            [],
    VariantState.ARCHIVED:          [],
    VariantState.DELETED:           [],
    VariantState.KEPT_HIDDEN:       [],
    VariantState.FLAGGED:           [],
}

TERMINAL_STATES: Set[VariantState] = {s for s, targets in TRANSITIONS.items() if not targets}


def is_valid_transition(from_state: VariantState, to_state: VariantState) -> bool:
    return to_state in TRANSITIONS.get(from_state, [])


# ── Classification Signals (priority order) ──────────────────────────────

class ClassifySignal(Enum):
    EXPLICIT_CONFIG = "explicit_config"       # priority 1: profile declares platform
    PATH_CONVENTION = "path_convention"       # priority 2: .agents/ → codex
    CONTENT_FEATURE = "content_feature"       # priority 3: shorter = trimmed variant
    POSITION_FALLBACK = "position_fallback"   # priority 4: shortest path = primary → "*"


# ── Trace Data Structures ────────────────────────────────────────────────

@dataclass
class StateTransition:
    """One state change for one variant."""
    variant_path: str
    from_state: str
    to_state: str
    signal: str = ""
    reason: str = ""
    inferred_platform: str = ""
    content_hash: str = ""

    @staticmethod
    def create(
        variant_path: str,
        from_state: VariantState,
        to_state: VariantState,
        signal: Optional[ClassifySignal] = None,
        reason: str = "",
        inferred_platform: str = "",
        content_hash: str = "",
    ) -> "StateTransition":
        if not is_valid_transition(from_state, to_state):
            raise ValueError(
                f"Illegal transition: {from_state.value} → {to_state.value} "
                f"for {variant_path}"
            )
        return StateTransition(
            variant_path=variant_path,
            from_state=from_state.value,
            to_state=to_state.value,
            signal=signal.value if signal else "",
            reason=reason,
            inferred_platform=inferred_platform,
            content_hash=content_hash,
        )


@dataclass
class SkillIdentityTrace:
    """Full trace for one skill name across all its variants in a single run."""
    skill_name: str
    bundle: str
    active_platform: str
    variants: List[str] = field(default_factory=list)
    transitions: List[StateTransition] = field(default_factory=list)
    final_selected: Optional[str] = None
    final_superseded: List[str] = field(default_factory=list)

    def add_transition(self, t: StateTransition) -> None:
        self.transitions.append(t)

    def terminal_state_for(self, variant_path: str) -> Optional[str]:
        """Return the last recorded state for a variant."""
        for t in reversed(self.transitions):
            if t.variant_path == variant_path:
                return t.to_state
        return None


@dataclass
class RunTrace:
    """One complete dedup/route run."""
    run_id: str = field(default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ"))
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    skills_dir: str = ""
    active_platform: str = ""
    resolve_strategy: str = ""  # archive | delete | keep
    identities: List[SkillIdentityTrace] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)


# ── Trace Persistence ────────────────────────────────────────────────────

TRACE_DIR = Path.home() / ".skills-auditor" / "traces"


def write_trace(trace: RunTrace, trace_dir: Optional[Path] = None) -> Path:
    """Write a run trace to JSON. Returns the output path."""
    out_dir = trace_dir or TRACE_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{trace.run_id}.json"
    out_path.write_text(trace.to_json(), encoding="utf-8")
    return out_path


def load_traces(trace_dir: Optional[Path] = None) -> List[RunTrace]:
    """Load all trace files from the trace directory."""
    src = trace_dir or TRACE_DIR
    if not src.exists():
        return []
    traces: List[RunTrace] = []
    for p in sorted(src.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            identities = []
            for ident in data.get("identities", []):
                transitions = [StateTransition(**t) for t in ident.get("transitions", [])]
                identities.append(SkillIdentityTrace(
                    skill_name=ident["skill_name"],
                    bundle=ident["bundle"],
                    active_platform=ident["active_platform"],
                    variants=ident.get("variants", []),
                    transitions=transitions,
                    final_selected=ident.get("final_selected"),
                    final_superseded=ident.get("final_superseded", []),
                ))
            traces.append(RunTrace(
                run_id=data.get("run_id", ""),
                timestamp=data.get("timestamp", ""),
                skills_dir=data.get("skills_dir", ""),
                active_platform=data.get("active_platform", ""),
                resolve_strategy=data.get("resolve_strategy", ""),
                identities=identities,
            ))
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
    return traces


# ── Batch Audit ──────────────────────────────────────────────────────────

@dataclass
class AuditFinding:
    check: str
    severity: str  # error | warning | info
    detail: str
    run_id: str = ""
    skill_name: str = ""
    variant_path: str = ""


def audit_traces(traces: List[RunTrace]) -> List[AuditFinding]:
    """Validate accumulated traces against the state machine definition."""
    findings: List[AuditFinding] = []
    all_observed_states: Set[str] = set()
    all_observed_signals: Set[str] = set()
    unroutable_count = 0

    for trace in traces:
        for ident in trace.identities:
            for t in ident.transitions:
                all_observed_states.add(t.from_state)
                all_observed_states.add(t.to_state)
                if t.signal:
                    all_observed_signals.add(t.signal)

                # Check 1: transition legality
                try:
                    from_s = VariantState(t.from_state)
                    to_s = VariantState(t.to_state)
                except ValueError:
                    findings.append(AuditFinding(
                        check="unknown_state",
                        severity="error",
                        detail=f"Unknown state value: {t.from_state} or {t.to_state}",
                        run_id=trace.run_id,
                        skill_name=ident.skill_name,
                        variant_path=t.variant_path,
                    ))
                    continue
                if not is_valid_transition(from_s, to_s):
                    findings.append(AuditFinding(
                        check="illegal_transition",
                        severity="error",
                        detail=f"Illegal: {t.from_state} → {t.to_state}",
                        run_id=trace.run_id,
                        skill_name=ident.skill_name,
                        variant_path=t.variant_path,
                    ))

            # Check 2: terminal state coverage
            for vp in ident.variants:
                final = ident.terminal_state_for(vp)
                if final is None:
                    findings.append(AuditFinding(
                        check="no_terminal_state",
                        severity="error",
                        detail=f"Variant has no recorded transitions",
                        run_id=trace.run_id,
                        skill_name=ident.skill_name,
                        variant_path=vp,
                    ))
                elif final not in {s.value for s in TERMINAL_STATES}:
                    findings.append(AuditFinding(
                        check="non_terminal_final",
                        severity="warning",
                        detail=f"Variant ended in non-terminal state: {final}",
                        run_id=trace.run_id,
                        skill_name=ident.skill_name,
                        variant_path=vp,
                    ))

                # Track unroutable
                if final == VariantState.FLAGGED.value:
                    unroutable_count += 1

    # Check 3: dead states (defined but never observed)
    all_defined = {s.value for s in VariantState}
    unused = all_defined - all_observed_states
    if unused and traces:
        findings.append(AuditFinding(
            check="unused_states",
            severity="info",
            detail=f"States never observed in {len(traces)} trace(s): {sorted(unused)}",
        ))

    # Check 4: signal coverage
    all_signals = {s.value for s in ClassifySignal}
    unused_signals = all_signals - all_observed_signals
    if unused_signals and traces:
        findings.append(AuditFinding(
            check="unused_signals",
            severity="info",
            detail=f"Classify signals never used: {sorted(unused_signals)}",
        ))

    # Check 5: unroutable frequency
    if unroutable_count > 0:
        findings.append(AuditFinding(
            check="unroutable_frequency",
            severity="warning",
            detail=f"{unroutable_count} variant(s) ended as FLAGGED (unroutable) across all traces",
        ))

    # Check 6: cross-run consistency
    skill_paths: Dict[str, Dict[str, str]] = {}  # skill_name → {run_id: final_selected}
    for trace in traces:
        for ident in trace.identities:
            bucket = skill_paths.setdefault(ident.skill_name, {})
            bucket[trace.run_id] = ident.final_selected or ""
    for skill_name, runs in skill_paths.items():
        unique_selections = set(runs.values())
        if len(unique_selections) > 1:
            findings.append(AuditFinding(
                check="cross_run_inconsistency",
                severity="warning",
                detail=f"Different selections across runs: {dict(runs)}",
                skill_name=skill_name,
            ))

    return findings
