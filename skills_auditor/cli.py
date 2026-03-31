"""Audit and synchronize local skill directories.

Default behavior is dry-run. Use --apply to perform filesystem changes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Sentinel: source applies to all target platforms when syncing / filtering.
PLATFORM_WILDCARD = "*"


@dataclass
class SourceSpec:
    """One discovery root path and which agent platforms may consume skills from it."""

    path: Path
    platforms: List[str]
    # Glob patterns (relative to path) to exclude from this source's scan.
    exclude_patterns: List[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.exclude_patterns is None:
            self.exclude_patterns = []


@dataclass
class EntryStatus:
    name: str
    entry_type: str  # symlink | directory | file | missing
    link_target: Optional[str]
    link_status: Optional[str]  # ok | broken | None
    has_skill_md: bool
    resolved_target: Optional[str]


@dataclass
class SyncAction:
    name: str
    expected_target: str
    action: str  # noop | create_link | replace_link | backup_and_link | skip_error
    reason: str


@dataclass
class DiscoveryItem:
    skill_name: str
    folder_name: str
    source_root: str
    skill_root: str
    relative_path: str
    content_hash: str
    source_priority: int
    # Platforms tagged on the discovery source (management layer); default ["*"].
    source_platforms: List[str]


@dataclass
class DiscoveryChoice:
    skill_name: str
    canonical_skill_root: str
    canonical_source_root: str
    total_candidates: int
    effective_candidates: int
    shadowed_skill_roots: List[str]
    collapsed_identical_roots: List[str]
    hash_conflict: bool


@dataclass
class DiscoverySummary:
    total_skills: int
    raw_candidates: int
    effective_candidates: int
    duplicate_skills: int
    hash_conflict_skills: int
    collapsed_identical_candidates: int


@dataclass
class DriftStatus:
    name: str
    local_path: str
    remote_url: Optional[str]
    branch: Optional[str]
    ahead: int
    behind: int
    dirty_count: int
    synced: bool
    # When synced, display_target shows the remote URL; otherwise local path
    display_target: str
    error: Optional[str] = None


def _git(args: List[str], cwd: Path) -> Optional[str]:
    """Run a git command and return stripped stdout, or None on failure."""
    try:
        r = subprocess.run(
            ["git"] + args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=15,
        )
        return r.stdout.strip() if r.returncode == 0 else None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def check_drift_for_path(name: str, path: Path) -> DriftStatus:
    """Check git sync status for a local skill path."""
    resolved = path.resolve()
    local_str = str(resolved)

    # Walk up to find the git repo root (skill may be nested in a monorepo)
    git_root = _git(["rev-parse", "--show-toplevel"], resolved)
    if git_root is None:
        return DriftStatus(
            name=name, local_path=local_str, remote_url=None,
            branch=None, ahead=0, behind=0, dirty_count=0,
            synced=False, display_target=local_str,
            error="not a git repository",
        )

    git_root_path = Path(git_root)

    # Fetch latest remote state (quiet, non-blocking)
    _git(["fetch", "--quiet"], git_root_path)

    branch = _git(["branch", "--show-current"], git_root_path) or "HEAD"
    remote_url = _git(["remote", "get-url", "origin"], git_root_path)

    ahead_str = _git(["rev-list", "--count", f"origin/{branch}..HEAD"], git_root_path)
    behind_str = _git(["rev-list", "--count", f"HEAD..origin/{branch}"], git_root_path)
    ahead = int(ahead_str) if ahead_str and ahead_str.isdigit() else 0
    behind = int(behind_str) if behind_str and behind_str.isdigit() else 0

    dirty_out = _git(["status", "--porcelain"], git_root_path)
    dirty_count = len(dirty_out.splitlines()) if dirty_out else 0

    synced = ahead == 0 and behind == 0 and dirty_count == 0

    # Build a human-friendly remote display: github URL without .git suffix
    display = local_str
    if synced and remote_url:
        display = remote_url.removesuffix(".git")

    return DriftStatus(
        name=name, local_path=local_str, remote_url=remote_url,
        branch=branch, ahead=ahead, behind=behind,
        dirty_count=dirty_count, synced=synced,
        display_target=display,
    )


def scan_skills(skills_dir: Path) -> List[EntryStatus]:
    items: List[EntryStatus] = []
    if not skills_dir.exists():
        return items

    for entry in sorted(skills_dir.iterdir(), key=lambda p: p.name.lower()):
        name = entry.name
        if entry.is_symlink():
            raw_target = os.readlink(entry)
            resolved = (entry.parent / raw_target).resolve()
            is_ok = resolved.exists()
            has_skill = (resolved / "SKILL.md").exists() if is_ok else False
            items.append(
                EntryStatus(
                    name=name,
                    entry_type="symlink",
                    link_target=raw_target,
                    link_status="ok" if is_ok else "broken",
                    has_skill_md=has_skill,
                    resolved_target=str(resolved),
                )
            )
        elif entry.is_dir():
            items.append(
                EntryStatus(
                    name=name,
                    entry_type="directory",
                    link_target=None,
                    link_status=None,
                    has_skill_md=(entry / "SKILL.md").exists(),
                    resolved_target=str(entry.resolve()),
                )
            )
        elif entry.is_file():
            items.append(
                EntryStatus(
                    name=name,
                    entry_type="file",
                    link_target=None,
                    link_status=None,
                    has_skill_md=False,
                    resolved_target=str(entry.resolve()),
                )
            )
        else:
            items.append(
                EntryStatus(
                    name=name,
                    entry_type="missing",
                    link_target=None,
                    link_status=None,
                    has_skill_md=False,
                    resolved_target=None,
                )
            )
    return items


def load_mapping(map_file: Path) -> Dict[str, str]:
    data = json.loads(map_file.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Mapping file must be a JSON object: {skillName: targetPath}")
    mapping: Dict[str, str] = {}
    for k, v in data.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise ValueError("Mapping keys/values must be strings.")
        mapping[k] = v
    return mapping


def parse_skill_name(skill_md: Path) -> str:
    text = skill_md.read_text(encoding="utf-8", errors="replace")
    # Parse frontmatter first, then fallback to folder name.
    match = re.search(r"(?m)^name:\s*([a-z0-9-]+)\s*$", text)
    if match:
        return match.group(1).strip()
    return skill_md.parent.name


# When scanning a skill pack for duplicate frontmatter names, skip these path segments.
_IGNORE_SKILL_SCAN_SEGMENTS = frozenset(
    {
        ".git",
        "node_modules",
        "dist",
        ".cache",
        "__pycache__",
        ".venv",
        "venv",
        ".tox",
    }
)


def _skill_md_path_is_under_ignored_segment(skill_md: Path) -> bool:
    return any(part in _IGNORE_SKILL_SCAN_SEGMENTS for part in skill_md.parts)


@dataclass
class DuplicateSkillNameFinding:
    """Same frontmatter `name:` declared by more than one distinct SKILL.md file under one bundle.

    Paths listed are one representative path per resolved real file (symlinks to the same
    inode count once).
    """

    bundle: str
    skill_name: str
    skill_md_paths: List[str]


def collect_duplicate_skill_names(skills_dir: Path) -> List[DuplicateSkillNameFinding]:
    """Per immediate child of skills_dir (bundle), find duplicate `name:` values in nested SKILL.md files.

    Catches packs like gstack that ship `.agents/skills/gstack/SKILL.md` alongside `gstack/SKILL.md`,
    which can confuse hosts that index recursively (multiple `/gstack` in the Skill list).

    Multiple paths that are symlinks to the same resolved file are folded into one entry
    (dedupe by ``Path.resolve()``), avoiding false positives for DRY symlink layouts
    (see https://github.com/ERerGB/skills-auditor/issues/2).
    """
    findings: List[DuplicateSkillNameFinding] = []
    if not skills_dir.exists() or not skills_dir.is_dir():
        return findings

    for entry in sorted(skills_dir.iterdir(), key=lambda p: p.name.lower()):
        if entry.name.startswith("."):
            continue
        if entry.is_file() and not entry.is_symlink():
            continue
        try:
            root = entry.resolve()
        except OSError:
            continue
        if not root.is_dir():
            continue

        # name -> { resolved_realpath_str: representative Path }
        by_name: Dict[str, Dict[str, Path]] = {}
        try:
            for skill_md in root.rglob("SKILL.md"):
                if _skill_md_path_is_under_ignored_segment(skill_md):
                    continue
                try:
                    real_key = str(skill_md.resolve())
                except OSError:
                    continue
                try:
                    name = parse_skill_name(skill_md)
                except OSError:
                    continue
                bucket = by_name.setdefault(name, {})
                if real_key not in bucket:
                    bucket[real_key] = skill_md
        except OSError:
            continue

        for skill_name in sorted(by_name.keys()):
            reps = sorted(by_name[skill_name].values(), key=lambda p: str(p).lower())
            if len(reps) > 1:
                findings.append(
                    DuplicateSkillNameFinding(
                        bundle=entry.name,
                        skill_name=skill_name,
                        skill_md_paths=[str(p) for p in reps],
                    )
                )
    return findings


def print_duplicate_name_check(
    skills_dir: Path,
    findings: List[DuplicateSkillNameFinding],
) -> None:
    print("\nduplicate frontmatter name: check (per top-level bundle)")
    print(
        "Detects multiple distinct SKILL.md files (by resolved path) declaring the same `name:` "
        "under one install folder (e.g. gstack + .agents copy). Symlinks to the same file count once."
    )
    if not findings:
        print("status: ok (no duplicate names within any bundle)")
        print("\njson:")
        print(json.dumps({"skills_dir": str(skills_dir), "findings": []}, indent=2))
        return

    print("status: findings present")
    print("bundle\tskill_name\tcount\tpaths")
    for f in findings:
        joined = " | ".join(f.skill_md_paths)
        print(
            f"{f.bundle}\t{f.skill_name}\t{len(f.skill_md_paths)}\t{joined}"
        )
    print("\njson:")
    print(
        json.dumps(
            {
                "skills_dir": str(skills_dir),
                "findings": [asdict(f) for f in findings],
            },
            indent=2,
            ensure_ascii=False,
        )
    )


@dataclass
class DedupAction:
    """One planned action for a duplicate SKILL.md."""

    bundle: str
    skill_name: str
    canonical_path: str
    duplicate_path: str
    action: str  # relink | skip_not_file | skip_multi_version
    reason: str
    content_hash_canonical: str = ""
    content_hash_duplicate: str = ""
    inferred_platform: str = ""


# ── Convention-based platform inference (Feature B) ──────────────────────

CONVENTION_PLATFORM_MAP: Dict[str, str] = {
    ".agents": "codex",
    ".codex": "codex",
    ".factory": "factory",
}


def infer_platform_from_path(skill_md: Path, bundle_root: Path) -> str:
    """Infer target platform from a SKILL.md path by checking known sub-directory conventions.

    Returns platform label (e.g. "codex", "factory") or "" for primary/unknown.
    """
    try:
        rel = skill_md.relative_to(bundle_root)
    except ValueError:
        return ""
    for part in rel.parts:
        plat = CONVENTION_PLATFORM_MAP.get(part)
        if plat:
            return plat
    return ""


# ── Select-One Routing Pipeline ─────────────────────────────────────────
# Four phases: discover → classify → route → resolve
# Each phase produces StateTransition records for the run trace.

from skills_auditor.state_machine import (
    ClassifySignal,
    RunTrace,
    SkillIdentityTrace,
    StateTransition,
    VariantState,
    write_trace,
)


def _bundle_root_for(skill_md: Path, skills_dir: Path) -> Path:
    """Walk up from the SKILL.md parent to find the top-level bundle dir."""
    cur = skill_md.parent
    while cur.parent != skills_dir and cur.parent != cur:
        cur = cur.parent
    return cur


def route_pipeline(
    skills_dir: Path,
    active_platform: str,
    resolve_strategy: str = "archive",
    trace_dir: Optional[Path] = None,
) -> Tuple[RunTrace, List[DedupAction]]:
    """Full Select-One Routing pipeline with trace output.

    Returns (trace, actions) where actions are backward-compatible DedupAction
    objects for apply_dedup / apply_route.
    """
    trace = RunTrace(
        skills_dir=str(skills_dir),
        active_platform=active_platform,
        resolve_strategy=resolve_strategy,
    )
    actions: List[DedupAction] = []

    findings = collect_duplicate_skill_names(skills_dir)
    if not findings:
        write_trace(trace, trace_dir)
        return trace, actions

    for f in findings:
        ident = SkillIdentityTrace(
            skill_name=f.skill_name,
            bundle=f.bundle,
            active_platform=active_platform,
            variants=list(f.skill_md_paths),
        )

        paths = [Path(p) for p in f.skill_md_paths]
        paths_sorted = sorted(paths, key=lambda p: (len(str(p)), str(p).lower()))
        primary = paths_sorted[0]
        bundle_root = _bundle_root_for(primary, skills_dir)

        # ── Phase 1: Discover — compute hashes ──
        hashes: Dict[str, str] = {}
        for p in paths_sorted:
            try:
                hashes[str(p)] = file_hash(p) if p.is_file() else ""
            except OSError:
                hashes[str(p)] = ""

        primary_hash = hashes.get(str(primary), "")

        # ── Phase 2: Classify — determine each variant's state & platform ──
        variant_platforms: Dict[str, str] = {}  # path → platform
        all_same_hash = all(
            h == primary_hash and h for h in hashes.values()
        )

        for p in paths_sorted:
            p_str = str(p)
            h = hashes.get(p_str, "")

            if all_same_hash:
                # TRUE_DUPLICATE path
                ident.add_transition(StateTransition.create(
                    p_str, VariantState.DISCOVERED, VariantState.TRUE_DUPLICATE,
                    reason="all variants have identical hash",
                    content_hash=h[:12],
                ))
                # True duplicates: primary gets selected, rest superseded
                plat = infer_platform_from_path(p, bundle_root)
                variant_platforms[p_str] = plat or PLATFORM_WILDCARD
            else:
                # VARIANT_DETECTED path
                ident.add_transition(StateTransition.create(
                    p_str, VariantState.DISCOVERED, VariantState.VARIANT_DETECTED,
                    reason=f"hash {'matches' if h == primary_hash else 'differs from'} primary",
                    content_hash=h[:12],
                ))

                plat = infer_platform_from_path(p, bundle_root)
                if plat:
                    signal = ClassifySignal.PATH_CONVENTION
                elif p == primary:
                    plat = PLATFORM_WILDCARD
                    signal = ClassifySignal.POSITION_FALLBACK
                else:
                    signal = ClassifySignal.CONTENT_FEATURE
                    plat = ""

                if plat:
                    ident.add_transition(StateTransition.create(
                        p_str, VariantState.VARIANT_DETECTED, VariantState.CLASSIFIED,
                        signal=signal,
                        reason=f"platform inferred as '{plat}'",
                        inferred_platform=plat,
                    ))
                    variant_platforms[p_str] = plat
                else:
                    ident.add_transition(StateTransition.create(
                        p_str, VariantState.VARIANT_DETECTED, VariantState.UNROUTABLE,
                        reason="no convention match, no explicit config",
                    ))
                    ident.add_transition(StateTransition.create(
                        p_str, VariantState.UNROUTABLE, VariantState.FLAGGED,
                        reason="manual classification required",
                    ))

        # ── Phase 3: Route — select one per platform ──
        # Priority: exact platform match > wildcard. Scan for exact first.
        routable = [
            (str(p), variant_platforms.get(str(p)))
            for p in paths_sorted
            if variant_platforms.get(str(p)) is not None
        ]
        exact_match = next(
            (ps for ps, plat in routable if plat == active_platform),
            None,
        )

        for p_str, plat in routable:
            prev_state = (
                VariantState.TRUE_DUPLICATE if all_same_hash
                else VariantState.CLASSIFIED
            )

            if exact_match:
                is_selected = (p_str == exact_match)
            elif all_same_hash:
                is_selected = (p_str == str(primary))
            else:
                # No exact match — wildcard primary is fallback
                is_selected = (plat == PLATFORM_WILDCARD)

            if is_selected and ident.final_selected is None:
                ident.add_transition(StateTransition.create(
                    p_str, prev_state, VariantState.SELECTED,
                    reason=(
                        f"exact match: platform '{plat}' == active '{active_platform}'"
                        if exact_match and not all_same_hash
                        else "primary selected (true duplicate)" if all_same_hash
                        else f"wildcard fallback: no exact match for '{active_platform}'"
                    ),
                ))
                ident.final_selected = p_str
            else:
                ident.add_transition(StateTransition.create(
                    p_str, prev_state, VariantState.SUPERSEDED,
                    reason=(
                        f"platform '{plat}' superseded by exact match"
                        if exact_match and not all_same_hash
                        else "non-primary true duplicate" if all_same_hash
                        else f"platform '{plat}' not active"
                    ),
                    inferred_platform=plat,
                ))
                ident.final_superseded.append(p_str)

        # If no variant was selected (e.g. all are for other platforms),
        # fallback: select primary
        if ident.final_selected is None and paths_sorted:
            p_str = str(primary)
            plat = variant_platforms.get(p_str, PLATFORM_WILDCARD)
            # Undo the SUPERSEDED if primary was superseded
            ident.final_selected = p_str
            ident.add_transition(StateTransition.create(
                p_str, VariantState.SUPERSEDED, VariantState.SELECTED,
                reason="fallback: no platform-specific variant matched, using primary",
            ))
            if p_str in ident.final_superseded:
                ident.final_superseded.remove(p_str)

        # ── Phase 4: Resolve — terminal states + build actions ──
        if ident.final_selected:
            ident.add_transition(StateTransition.create(
                ident.final_selected, VariantState.SELECTED, VariantState.ACTIVE,
                reason="retained as active",
            ))

        for sup in ident.final_superseded:
            sup_hash = hashes.get(sup, "")
            sup_plat = variant_platforms.get(sup, "")
            primary_hash_short = primary_hash[:12] if primary_hash else ""
            sup_hash_short = sup_hash[:12] if sup_hash else ""

            if all_same_hash:
                # True duplicate → relink
                terminal = VariantState.ARCHIVED
                action = "relink"
                reason = "identical content, replace with symlink"
            elif resolve_strategy == "delete":
                terminal = VariantState.DELETED
                action = "delete"
                reason = f"platform '{sup_plat}' not active, strategy=delete"
            elif resolve_strategy == "archive":
                terminal = VariantState.ARCHIVED
                action = "archive"
                reason = f"platform '{sup_plat}' not active, strategy=archive"
            else:
                terminal = VariantState.KEPT_HIDDEN
                action = "keep"
                reason = f"platform '{sup_plat}' not active, strategy=keep"

            ident.add_transition(StateTransition.create(
                sup, VariantState.SUPERSEDED, terminal,
                reason=reason,
                inferred_platform=sup_plat,
            ))

            actions.append(DedupAction(
                bundle=f.bundle,
                skill_name=f.skill_name,
                canonical_path=ident.final_selected or str(primary),
                duplicate_path=sup,
                action=action,
                reason=reason,
                content_hash_canonical=primary_hash_short,
                content_hash_duplicate=sup_hash_short,
                inferred_platform=sup_plat,
            ))

        trace.identities.append(ident)

    write_trace(trace, trace_dir)
    return trace, actions


def apply_route(actions: List[DedupAction], skills_dir: Path) -> int:
    """Execute route actions. Returns count of applied changes."""
    applied = 0
    for a in actions:
        dup = Path(a.duplicate_path)
        canonical = Path(a.canonical_path)

        if a.action == "relink":
            rel = os.path.relpath(canonical, dup.parent)
            dup.unlink()
            dup.symlink_to(rel)
            applied += 1
        elif a.action == "archive":
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            archive_name = f"{dup.name}.archived-{ts}"
            dup.rename(dup.parent / archive_name)
            applied += 1
        elif a.action == "delete":
            if dup.is_file() or dup.is_symlink():
                dup.unlink()
            elif dup.is_dir():
                import shutil
                shutil.rmtree(dup)
            applied += 1
        # "keep" → no filesystem change
    return applied


def print_route_plan(
    trace: RunTrace,
    actions: List[DedupAction],
    apply: bool,
) -> None:
    mode = "APPLY" if apply else "DRY-RUN"
    print(f"route mode: {mode}")
    print(f"active platform: {trace.active_platform}")
    print(f"resolve strategy: {trace.resolve_strategy}")

    if not trace.identities:
        print("status: ok (no duplicate names found, no routing needed)")
        return

    relinks = [a for a in actions if a.action == "relink"]
    archives = [a for a in actions if a.action == "archive"]
    deletes = [a for a in actions if a.action == "delete"]
    keeps = [a for a in actions if a.action == "keep"]
    print(
        f"identities: {len(trace.identities)} | "
        f"relink: {len(relinks)} | archive: {len(archives)} | "
        f"delete: {len(deletes)} | keep: {len(keeps)}"
    )

    for ident in trace.identities:
        print(f"\n  [{ident.bundle}] {ident.skill_name}")
        print(f"    selected: {ident.final_selected or '(none)'}")
        for sup in ident.final_superseded:
            plat = ""
            for t in ident.transitions:
                if t.variant_path == sup and t.inferred_platform:
                    plat = t.inferred_platform
            act = next((a for a in actions if a.duplicate_path == sup), None)
            act_label = act.action if act else "?"
            print(f"    superseded: {sup}  (platform: {plat or '?'}, action: {act_label})")

    flagged = [
        t for ident in trace.identities for t in ident.transitions
        if t.to_state == VariantState.FLAGGED.value
    ]
    if flagged:
        print(f"\nFLAGGED (unroutable, needs manual classification): {len(flagged)}")
        for t in flagged:
            print(f"  {t.variant_path}: {t.reason}")

    print(f"\ntrace written: {trace.run_id}")


# ── Legacy plan_dedup (backward compat, delegates to route_pipeline) ─────

def plan_dedup(
    skills_dir: Path,
) -> Tuple[List[DedupAction], List[DuplicateSkillNameFinding]]:
    """Build a dedup plan. Backward-compatible wrapper around route_pipeline.

    When called without --platform, uses '*' (wildcard) which means:
    - TRUE_DUPLICATE → relink (same behavior as before)
    - VARIANT_DETECTED → skip_multi_version (same behavior as before)
    """
    findings = collect_duplicate_skill_names(skills_dir)
    actions: List[DedupAction] = []

    for f in findings:
        paths = [Path(p) for p in f.skill_md_paths]
        paths_sorted = sorted(paths, key=lambda p: (len(str(p)), str(p).lower()))
        canonical = paths_sorted[0]
        try:
            canon_hash = file_hash(canonical)
        except OSError:
            canon_hash = ""

        bundle_root = _bundle_root_for(canonical, skills_dir)

        for dup in paths_sorted[1:]:
            if not dup.is_file():
                actions.append(
                    DedupAction(
                        bundle=f.bundle, skill_name=f.skill_name,
                        canonical_path=str(canonical), duplicate_path=str(dup),
                        action="skip_not_file",
                        reason="duplicate path is not a regular file",
                    )
                )
                continue

            try:
                dup_hash = file_hash(dup)
            except OSError:
                dup_hash = ""

            plat = infer_platform_from_path(dup, bundle_root)

            if canon_hash and dup_hash and canon_hash == dup_hash:
                actions.append(DedupAction(
                    bundle=f.bundle, skill_name=f.skill_name,
                    canonical_path=str(canonical), duplicate_path=str(dup),
                    action="relink",
                    reason="identical content (same hash), safe to symlink",
                    content_hash_canonical=canon_hash[:12],
                    content_hash_duplicate=dup_hash[:12],
                    inferred_platform=plat,
                ))
            else:
                actions.append(DedupAction(
                    bundle=f.bundle, skill_name=f.skill_name,
                    canonical_path=str(canonical), duplicate_path=str(dup),
                    action="skip_multi_version",
                    reason=(
                        f"different content (hash mismatch), likely host-specific variant"
                        f"{' for ' + plat if plat else ''}"
                    ),
                    content_hash_canonical=canon_hash[:12],
                    content_hash_duplicate=dup_hash[:12],
                    inferred_platform=plat,
                ))
    return actions, findings


def apply_dedup(actions: List[DedupAction]) -> int:
    """Execute planned relink actions. Returns count of applied symlinks."""
    applied = 0
    for a in actions:
        if a.action != "relink":
            continue
        dup = Path(a.duplicate_path)
        canonical = Path(a.canonical_path)
        rel = os.path.relpath(canonical, dup.parent)
        dup.unlink()
        dup.symlink_to(rel)
        applied += 1
    return applied


def print_dedup_plan(
    actions: List[DedupAction],
    findings: List[DuplicateSkillNameFinding],
    apply: bool,
) -> None:
    mode = "APPLY" if apply else "DRY-RUN"
    print(f"dedup mode: {mode}")
    if not findings:
        print("status: ok (no duplicate names found)")
        print("\njson:")
        print(json.dumps({"actions": [], "findings": []}, indent=2))
        return

    relinks = [a for a in actions if a.action == "relink"]
    skips = [a for a in actions if a.action == "skip_multi_version"]
    print(f"findings: {len(findings)} duplicate name(s)")
    print(f"planned: {len(relinks)} relink(s), {len(skips)} multi-version skip(s)")
    print(
        "\nbundle\tskill_name\taction\tinferred_platform\t"
        "hash_canon\thash_dup\tduplicate_path\tcanonical_path"
    )
    for a in actions:
        print(
            f"{a.bundle}\t{a.skill_name}\t{a.action}\t{a.inferred_platform or '-'}\t"
            f"{a.content_hash_canonical or '-'}\t{a.content_hash_duplicate or '-'}\t"
            f"{a.duplicate_path}\t{a.canonical_path}"
        )

    if skips:
        print("\nmulti-version variants detected (not symlinked):")
        print("These files share a name but have different content — likely tailored for specific hosts.")
        print("Use 'skills-audit route --platform <name>' for Select-One routing.")
        for s in skips:
            plat_label = s.inferred_platform or "unknown"
            print(f"  {s.duplicate_path}  (platform: {plat_label})")

    print("\njson:")
    print(
        json.dumps(
            {
                "mode": mode.lower(),
                "findings": [asdict(f) for f in findings],
                "actions": [asdict(a) for a in actions],
            },
            indent=2,
            ensure_ascii=False,
        )
    )


def file_hash(path: Path) -> str:
    data = path.read_bytes()
    return hashlib.sha256(data).hexdigest()


def is_path_excluded(path: Path, excluded_roots: List[Path]) -> bool:
    abs_path = path.resolve()
    for ex in excluded_roots:
        ex_abs = ex.resolve()
        if abs_path == ex_abs or ex_abs in abs_path.parents:
            return True
    return False


def _matches_exclude_patterns(
    path: Path,
    source_root: Path,
    exclude_patterns: List[str],
) -> bool:
    """Check if *path* matches any of the exclude glob patterns relative to *source_root*."""
    if not exclude_patterns:
        return False
    try:
        rel = path.relative_to(source_root)
    except ValueError:
        return False
    rel_str = str(rel)
    import fnmatch

    return any(fnmatch.fnmatch(rel_str, pat) for pat in exclude_patterns)


def discover_from_source(
    source_root: Path,
    source_priority: int,
    excluded_roots: List[Path],
    source_platforms: Optional[List[str]] = None,
    exclude_patterns: Optional[List[str]] = None,
) -> List[DiscoveryItem]:
    plats = (
        source_platforms
        if source_platforms is not None
        else [PLATFORM_WILDCARD]
    )
    excl_pats = exclude_patterns or []
    items: List[DiscoveryItem] = []
    if not source_root.exists():
        return items
    if is_path_excluded(source_root, excluded_roots):
        return items

    seen_roots: set[str] = set()
    if source_root.is_dir():
        for child in sorted(source_root.iterdir(), key=lambda p: p.name.lower()):
            if is_path_excluded(child, excluded_roots):
                continue
            if _matches_exclude_patterns(child, source_root, excl_pats):
                continue
            skill_md = child / "SKILL.md"
            if child.is_dir() and skill_md.exists():
                skill_name = parse_skill_name(skill_md)
                root_key = str(child.resolve())
                seen_roots.add(root_key)
                items.append(
                    DiscoveryItem(
                        skill_name=skill_name,
                        folder_name=child.name,
                        source_root=str(source_root.resolve()),
                        skill_root=str(child.resolve()),
                        relative_path=str(child.relative_to(source_root)),
                        content_hash=file_hash(skill_md),
                        source_priority=source_priority,
                        source_platforms=list(plats),
                    )
                )

    for skill_md in sorted(source_root.rglob("SKILL.md"), key=lambda p: str(p).lower()):
        if is_path_excluded(skill_md, excluded_roots):
            continue
        if _matches_exclude_patterns(skill_md, source_root, excl_pats):
            continue
        skill_root = skill_md.parent
        root_key = str(skill_root.resolve())
        if root_key in seen_roots:
            continue
        skill_name = parse_skill_name(skill_md)
        rel = str(skill_root.relative_to(source_root))
        items.append(
            DiscoveryItem(
                skill_name=skill_name,
                folder_name=skill_root.name,
                source_root=str(source_root.resolve()),
                skill_root=str(skill_root.resolve()),
                relative_path=rel,
                content_hash=file_hash(skill_md),
                source_priority=source_priority,
                source_platforms=list(plats),
            )
        )
    return items


def resolve_skills_dirs(cli_dirs: Optional[List[str]]) -> List[Path]:
    """Expand and de-duplicate skill roots from CLI (repeatable --skills-dir).

    If the user passes no --skills-dir, default to ~/.cursor/skills only
    (backward compatible). Pass multiple flags to align Cursor + Claude Code, e.g.:
      --skills-dir ~/.cursor/skills --skills-dir ~/.claude/skills
    """
    raw = cli_dirs if cli_dirs else ["~/.cursor/skills"]
    out: List[Path] = []
    seen: set[str] = set()
    for item in raw:
        p = Path(item).expanduser()
        try:
            key = str(p.resolve())
        except OSError:
            key = str(p)
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def default_discovery_sources() -> List[Path]:
    home = Path("~").expanduser()
    cwd = Path.cwd()
    defaults = [
        cwd / ".cursor" / "skills",
        home / ".cursor" / "skills",
        home / ".cursor" / "skills-cursor",
        cwd / ".claude" / "skills",
        home / ".claude" / "skills",
    ]
    # Keep order and remove duplicates.
    dedup: List[Path] = []
    seen: set[str] = set()
    for p in defaults:
        key = str(p.resolve()) if p.exists() else str(p.expanduser())
        if key not in seen:
            seen.add(key)
            dedup.append(p)
    return dedup


def infer_default_platforms_for_source(root: Path) -> List[str]:
    """Heuristic platforms for built-in default discovery roots (no profile file)."""
    try:
        key = str(root.resolve()).lower()
    except OSError:
        key = str(root.expanduser()).lower()
    if ".claude" in key and "skills" in key:
        return ["claude-code"]
    if "skills-cursor" in key:
        return ["cursor"]
    if "cursor" in key and "plugins" in key:
        return ["cursor"]
    # Shared project or ~/.cursor/skills — safe for both.
    return ["cursor", "claude-code"]


def parse_profile_source_entries(sources_raw: object) -> List[SourceSpec]:
    """Parse profile ``sources``: string or object per entry.

    Object form supports:
      - ``path`` (required): root directory
      - ``platform`` (required): list of platform labels
      - ``exclude`` (optional): list of glob patterns relative to *path* to skip
    """
    if not isinstance(sources_raw, list):
        raise ValueError("'sources' must be a list.")
    specs: List[SourceSpec] = []
    for idx, item in enumerate(sources_raw):
        if isinstance(item, str):
            specs.append(SourceSpec(Path(item).expanduser(), [PLATFORM_WILDCARD]))
            continue
        if isinstance(item, dict):
            path_v = item.get("path")
            plat_v = item.get("platform")
            excl_v = item.get("exclude", [])
            if not isinstance(path_v, str):
                raise ValueError(
                    f"sources[{idx}]: object entry requires string 'path'."
                )
            if not isinstance(plat_v, list) or not plat_v:
                raise ValueError(
                    f"sources[{idx}]: object entry requires non-empty list 'platform'."
                )
            if not all(isinstance(x, str) for x in plat_v):
                raise ValueError(
                    f"sources[{idx}]: 'platform' must be a list of strings."
                )
            if not isinstance(excl_v, list) or not all(isinstance(x, str) for x in excl_v):
                raise ValueError(
                    f"sources[{idx}]: 'exclude' must be a list of strings."
                )
            specs.append(
                SourceSpec(
                    Path(path_v).expanduser(),
                    list(plat_v),
                    exclude_patterns=list(excl_v),
                )
            )
            continue
        raise ValueError(
            f"sources[{idx}]: each entry must be a string or object with path + platform."
        )
    return specs


def load_discovery_profile(profile_file: Path) -> Dict[str, object]:
    data = json.loads(profile_file.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Discovery profile must be a JSON object.")
    sources_raw = data.get("sources", [])
    exclude_sources = data.get("exclude_sources", [])
    collapse_identical = data.get("collapse_identical", True)
    source_specs = parse_profile_source_entries(sources_raw)
    if not isinstance(exclude_sources, list) or not all(
        isinstance(x, str) for x in exclude_sources
    ):
        raise ValueError("'exclude_sources' must be a string list.")
    if not isinstance(collapse_identical, bool):
        raise ValueError("'collapse_identical' must be a boolean.")
    return {
        "source_specs": source_specs,
        "exclude_sources": exclude_sources,
        "collapse_identical": collapse_identical,
    }


def platform_allows_target(source_platforms: List[str], target_platform: str) -> bool:
    if PLATFORM_WILDCARD in source_platforms:
        return True
    return target_platform in source_platforms


def longest_matching_source_platforms(
    skill_target: Path,
    source_specs: List[SourceSpec],
) -> List[str]:
    """Pick the longest profile source root that contains skill_target; else ['*']."""
    try:
        skill_resolved = skill_target.expanduser().resolve()
    except OSError:
        return [PLATFORM_WILDCARD]
    best: Optional[List[str]] = None
    best_len = -1
    for spec in source_specs:
        try:
            root_r = spec.path.expanduser().resolve()
        except OSError:
            continue
        try:
            skill_resolved.relative_to(root_r)
        except ValueError:
            continue
        ln = len(str(root_r))
        if ln > best_len:
            best_len = ln
            best = list(spec.platforms)
    return best if best is not None else [PLATFORM_WILDCARD]


def build_discovery(
    items: List[DiscoveryItem],
    collapse_identical: bool,
) -> Tuple[List[DiscoveryChoice], List[DiscoveryItem]]:
    grouped: Dict[str, List[DiscoveryItem]] = {}
    for item in items:
        grouped.setdefault(item.skill_name, []).append(item)

    choices: List[DiscoveryChoice] = []
    canonical_items: List[DiscoveryItem] = []
    for skill_name in sorted(grouped.keys()):
        raw_candidates = sorted(
            grouped[skill_name],
            key=lambda x: (x.source_priority, x.skill_root),
        )
        collapsed_identical_roots: List[str] = []
        if collapse_identical:
            hash_seen: Dict[str, DiscoveryItem] = {}
            effective_candidates: List[DiscoveryItem] = []
            for c in raw_candidates:
                if c.content_hash in hash_seen:
                    collapsed_identical_roots.append(c.skill_root)
                    continue
                hash_seen[c.content_hash] = c
                effective_candidates.append(c)
        else:
            effective_candidates = raw_candidates

        canonical = effective_candidates[0]
        canonical_items.append(canonical)
        all_hashes = {c.content_hash for c in effective_candidates}
        choices.append(
            DiscoveryChoice(
                skill_name=skill_name,
                canonical_skill_root=canonical.skill_root,
                canonical_source_root=canonical.source_root,
                total_candidates=len(raw_candidates),
                effective_candidates=len(effective_candidates),
                shadowed_skill_roots=[c.skill_root for c in effective_candidates[1:]],
                collapsed_identical_roots=collapsed_identical_roots,
                hash_conflict=len(all_hashes) > 1 and len(effective_candidates) > 1,
            )
        )
    return choices, canonical_items


def summarize_discovery(choices: List[DiscoveryChoice]) -> DiscoverySummary:
    return DiscoverySummary(
        total_skills=len(choices),
        raw_candidates=sum(c.total_candidates for c in choices),
        effective_candidates=sum(c.effective_candidates for c in choices),
        duplicate_skills=sum(1 for c in choices if c.effective_candidates > 1),
        hash_conflict_skills=sum(1 for c in choices if c.hash_conflict),
        collapsed_identical_candidates=sum(len(c.collapsed_identical_roots) for c in choices),
    )


def plan_sync(
    skills_dir: Path,
    mapping: Dict[str, str],
    *,
    target_platform: Optional[str] = None,
    source_specs: Optional[List[SourceSpec]] = None,
) -> List[SyncAction]:
    actions: List[SyncAction] = []
    for name, target_str in mapping.items():
        entry = skills_dir / name
        target = Path(target_str).expanduser()
        if target_platform and source_specs:
            plat = longest_matching_source_platforms(target, source_specs)
            if not platform_allows_target(plat, target_platform):
                actions.append(
                    SyncAction(
                        name=name,
                        expected_target=str(target),
                        action="skip_platform",
                        reason=(
                            f"source platforms {plat!r} do not allow "
                            f"sync target {target_platform!r}"
                        ),
                    )
                )
                continue
        if not target.exists():
            actions.append(
                SyncAction(
                    name=name,
                    expected_target=str(target),
                    action="skip_error",
                    reason="expected target path does not exist",
                )
            )
            continue
        if not (target / "SKILL.md").exists():
            actions.append(
                SyncAction(
                    name=name,
                    expected_target=str(target),
                    action="skip_error",
                    reason="expected target has no SKILL.md",
                )
            )
            continue

        if not entry.exists() and not entry.is_symlink():
            actions.append(
                SyncAction(
                    name=name,
                    expected_target=str(target),
                    action="create_link",
                    reason="entry missing",
                )
            )
            continue

        if entry.is_symlink():
            current_target = (entry.parent / os.readlink(entry)).resolve()
            if current_target == target.resolve():
                actions.append(
                    SyncAction(
                        name=name,
                        expected_target=str(target),
                        action="noop",
                        reason="already linked to expected target",
                    )
                )
            else:
                actions.append(
                    SyncAction(
                        name=name,
                        expected_target=str(target),
                        action="replace_link",
                        reason=f"linked to different target: {current_target}",
                    )
                )
            continue

        if entry.exists():
            actions.append(
                SyncAction(
                    name=name,
                    expected_target=str(target),
                    action="backup_and_link",
                    reason="non-symlink entry exists",
                )
            )
            continue

    return actions


def apply_actions(skills_dir: Path, actions: List[SyncAction]) -> None:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for action in actions:
        entry = skills_dir / action.name
        target = Path(action.expected_target).expanduser().resolve()

        if action.action in {"noop", "skip_error", "skip_platform"}:
            continue

        if action.action == "create_link":
            os.symlink(str(target), str(entry))
            continue

        if action.action == "replace_link":
            if entry.is_symlink() or entry.exists():
                entry.unlink()
            os.symlink(str(target), str(entry))
            continue

        if action.action == "backup_and_link":
            backup_name = f"{action.name}.backup-{timestamp}"
            backup_path = skills_dir / backup_name
            entry.rename(backup_path)
            os.symlink(str(target), str(entry))
            continue


def print_audit(
    statuses: List[EntryStatus],
    drift_map: Optional[Dict[str, DriftStatus]] = None,
) -> None:
    has_drift = drift_map is not None
    header = "name\tentry_type\tlink_status\thas_skill_md\tdisplay_target"
    if has_drift:
        header += "\tsync_status"
    print(header)

    for item in statuses:
        drift = drift_map.get(item.name) if drift_map else None
        # When drift data available and synced, show remote URL instead of local path
        target = item.resolved_target or "-"
        if drift and drift.synced and drift.remote_url:
            target = drift.display_target

        row = (
            f"{item.name}\t{item.entry_type}\t{item.link_status or '-'}\t"
            f"{str(item.has_skill_md).lower()}\t{target}"
        )
        if has_drift:
            if drift is None:
                row += "\t-"
            elif drift.error:
                row += f"\t{drift.error}"
            elif drift.synced:
                row += "\tsynced"
            else:
                parts = []
                if drift.ahead > 0:
                    parts.append(f"ahead={drift.ahead}")
                if drift.behind > 0:
                    parts.append(f"behind={drift.behind}")
                if drift.dirty_count > 0:
                    parts.append(f"dirty={drift.dirty_count}")
                row += f"\tdrift({', '.join(parts)})"
        print(row)

    json_data = [asdict(s) for s in statuses]
    if drift_map:
        for entry in json_data:
            drift = drift_map.get(entry["name"])
            if drift:
                entry["drift"] = asdict(drift)
    print("\njson:")
    print(json.dumps(json_data, indent=2, ensure_ascii=False))


def print_plan(actions: List[SyncAction], apply: bool) -> None:
    mode = "APPLY" if apply else "DRY-RUN"
    print(f"mode: {mode}")
    print("name\taction\treason\texpected_target")
    for a in actions:
        print(f"{a.name}\t{a.action}\t{a.reason}\t{a.expected_target}")
    print("\njson:")
    print(json.dumps([asdict(a) for a in actions], indent=2, ensure_ascii=False))


def print_discovery_report(
    source_specs: List[SourceSpec],
    excluded_sources: List[Path],
    collapse_identical: bool,
    items: List[DiscoveryItem],
    choices: List[DiscoveryChoice],
    canonical_items: List[DiscoveryItem],
    summary: DiscoverySummary,
    summary_only: bool,
) -> None:
    if summary_only:
        print("discovery summary:")
        print(
            "total_skills\traw_candidates\teffective_candidates\tduplicate_skills\t"
            "hash_conflict_skills\tcollapsed_identical_candidates"
        )
        print(
            f"{summary.total_skills}\t{summary.raw_candidates}\t{summary.effective_candidates}\t"
            f"{summary.duplicate_skills}\t{summary.hash_conflict_skills}\t"
            f"{summary.collapsed_identical_candidates}"
        )
        print("\njson:")
        print(json.dumps(asdict(summary), indent=2, ensure_ascii=False))
        return

    print("sources (priority order):")
    for idx, spec in enumerate(source_specs):
        plat = ",".join(spec.platforms)
        print(f"{idx}\t{spec.path.expanduser()}\t{plat}")
    print("\nexcluded source roots:")
    if excluded_sources:
        for src in excluded_sources:
            print(f"- {src.expanduser()}")
    else:
        print("- (none)")
    print(f"\ncollapse_identical: {str(collapse_identical).lower()}")

    print("\nall discovered candidates:")
    print(
        "skill_name\tsource_priority\tsource_platforms\t"
        "source_root\tskill_root\thash"
    )
    for item in sorted(items, key=lambda x: (x.skill_name, x.source_priority, x.skill_root)):
        plat = ",".join(item.source_platforms)
        print(
            f"{item.skill_name}\t{item.source_priority}\t{plat}\t{item.source_root}\t"
            f"{item.skill_root}\t{item.content_hash[:12]}"
        )

    print("\ncanonical injection preview:")
    print(
        "skill_name\tcanonical_skill_root\tcanonical_source_platforms\t"
        "total_candidates\teffective_candidates\tcollapsed_identical\thash_conflict"
    )
    for choice, can_item in zip(choices, canonical_items):
        plat = ",".join(can_item.source_platforms)
        print(
            f"{choice.skill_name}\t{choice.canonical_skill_root}\t{plat}\t"
            f"{choice.total_candidates}\t{choice.effective_candidates}\t"
            f"{len(choice.collapsed_identical_roots)}\t{str(choice.hash_conflict).lower()}"
        )

    print("\njson:")
    print(
        json.dumps(
            {
                "sources": [
                    {
                        "path": str(spec.path.expanduser()),
                        "platform": list(spec.platforms),
                    }
                    for spec in source_specs
                ],
                "excluded_sources": [str(s.expanduser()) for s in excluded_sources],
                "collapse_identical": collapse_identical,
                "candidates": [asdict(i) for i in items],
                "choices": [asdict(c) for c in choices],
                "canonical_preview": [asdict(c) for c in canonical_items],
                "summary": asdict(summary),
            },
            indent=2,
            ensure_ascii=False,
        )
    )


def print_drift_report(drifts: List[DriftStatus]) -> None:
    print("name\tsynced\tbranch\tahead\tbehind\tdirty\tdisplay_target")
    for d in drifts:
        sync_label = "synced" if d.synced else "DRIFT"
        if d.error:
            sync_label = d.error
        print(
            f"{d.name}\t{sync_label}\t{d.branch or '-'}\t"
            f"{d.ahead}\t{d.behind}\t{d.dirty_count}\t{d.display_target}"
        )

    synced_count = sum(1 for d in drifts if d.synced)
    drift_count = sum(1 for d in drifts if not d.synced and not d.error)
    error_count = sum(1 for d in drifts if d.error)
    print(f"\nsummary: {synced_count} synced, {drift_count} drifted, {error_count} errors")

    print("\njson:")
    print(json.dumps([asdict(d) for d in drifts], indent=2, ensure_ascii=False))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit and sync local skill folders.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_audit = sub.add_parser("audit", help="Audit current skills directory state")
    p_audit.add_argument(
        "--skills-dir",
        action="append",
        dest="skills_dirs",
        metavar="DIR",
        help="Skill root (repeat for multiple, e.g. Cursor + Claude Code). Default: ~/.cursor/skills",
    )
    p_audit.add_argument(
        "--with-drift", action="store_true",
        help="Include git drift check for symlinked skills (fetches remote).",
    )
    p_audit.add_argument(
        "--skip-duplicate-name-check",
        action="store_true",
        help="Skip the default scan for duplicate `name:` in nested SKILL.md under each bundle.",
    )
    p_audit.add_argument(
        "--fail-on-duplicate-names",
        action="store_true",
        help="Exit with code 4 if any bundle has multiple SKILL.md declaring the same name.",
    )

    p_drift = sub.add_parser("drift-check", help="Check git sync status for symlinked skills")
    p_drift.add_argument(
        "--skills-dir",
        action="append",
        dest="skills_dirs",
        metavar="DIR",
        help="Skill root (repeat for multiple). Default: ~/.cursor/skills",
    )

    p_sync = sub.add_parser("sync", help="Plan or apply skill relinking based on map file")
    p_sync.add_argument(
        "--skills-dir",
        action="append",
        dest="skills_dirs",
        metavar="DIR",
        help="Skill root (repeat for multiple). Default: ~/.cursor/skills",
    )
    p_sync.add_argument("--map-file", required=True, help="JSON map file: {name: targetPath}")
    p_sync.add_argument(
        "--target-platform",
        metavar="NAME",
        help=(
            "Only sync skills whose map target path matches a profile source that allows "
            "this platform (e.g. claude-code, cursor). Requires --discovery-profile."
        ),
    )
    p_sync.add_argument(
        "--discovery-profile",
        metavar="FILE",
        help="Discovery profile JSON (same as audit-discovery) for platform-aware sync.",
    )
    p_sync.add_argument("--apply", action="store_true", help="Apply actions (default is dry-run)")

    p_dedup = sub.add_parser(
        "dedup",
        help="Detect duplicate frontmatter names and replace copies with symlinks to the canonical file",
    )
    p_dedup.add_argument(
        "--skills-dir",
        action="append",
        dest="skills_dirs",
        metavar="DIR",
        help="Skill root (repeat for multiple). Default: ~/.cursor/skills",
    )
    p_dedup.add_argument(
        "--apply", action="store_true",
        help="Actually replace duplicates with symlinks (default is dry-run).",
    )

    # ── route: Select-One Routing with state machine trace ──
    p_route = sub.add_parser(
        "route",
        help="Select-One Routing: classify variants by platform, keep one, resolve rest",
    )
    p_route.add_argument(
        "--skills-dir", action="append", dest="skills_dirs", metavar="DIR",
        help="Skill root (repeat for multiple). Default: ~/.cursor/skills",
    )
    p_route.add_argument(
        "--platform", required=True,
        help="Active platform (e.g. cursor, codex, factory, claude-code).",
    )
    p_route.add_argument(
        "--strategy", default="archive", choices=["archive", "delete", "keep"],
        help="How to resolve superseded variants (default: archive).",
    )
    p_route.add_argument(
        "--apply", action="store_true",
        help="Execute the routing plan (default is dry-run).",
    )
    p_route.add_argument(
        "--trace-dir", metavar="DIR",
        help="Override trace output directory (default: ~/.skills-auditor/traces/).",
    )

    # ── audit-state-machine: validate accumulated traces ──
    p_sm = sub.add_parser(
        "audit-state-machine",
        help="Validate accumulated run traces against state machine transition rules",
    )
    p_sm.add_argument(
        "--trace-dir", metavar="DIR",
        help="Trace directory to audit (default: ~/.skills-auditor/traces/).",
    )

    p_discovery = sub.add_parser(
        "audit-discovery",
        help="Audit discovery-layer collisions and canonical skill selection",
    )
    p_discovery.add_argument(
        "--source",
        action="append",
        default=[],
        help="Discovery source root. Repeatable. If omitted, uses default sources.",
    )
    p_discovery.add_argument(
        "--exclude-source",
        action="append",
        default=[],
        help="Exclude source root/prefix from discovery scan. Repeatable.",
    )
    p_discovery.add_argument(
        "--profile-file",
        help="Discovery profile JSON with sources/exclude_sources/collapse_identical.",
    )
    p_discovery.add_argument(
        "--no-collapse-identical",
        action="store_true",
        help="Disable same-hash candidate folding in discovery report.",
    )
    p_discovery.add_argument(
        "--summary-only",
        action="store_true",
        help="Print only summary counters and JSON summary.",
    )
    p_discovery.add_argument(
        "--fail-on-conflict",
        action="store_true",
        help="Exit with code 2 if any duplicate skill remains after collapse.",
    )
    p_discovery.add_argument(
        "--fail-on-hash-conflict",
        action="store_true",
        help="Exit with code 3 if any same-name skill has hash conflict.",
    )

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "audit":
        duplicate_exit = False
        for idx, skills_dir in enumerate(resolve_skills_dirs(args.skills_dirs)):
            if idx > 0:
                print()
            print(f"skills-dir: {skills_dir}")
            statuses = scan_skills(skills_dir)
            drift_map: Optional[Dict[str, DriftStatus]] = None
            if args.with_drift:
                drift_map = {}
                for s in statuses:
                    if s.entry_type == "symlink" and s.link_status == "ok" and s.resolved_target:
                        drift_map[s.name] = check_drift_for_path(s.name, Path(s.resolved_target))
            print_audit(statuses, drift_map)
            if not args.skip_duplicate_name_check:
                dup_findings = collect_duplicate_skill_names(skills_dir)
                print_duplicate_name_check(skills_dir, dup_findings)
                if dup_findings:
                    duplicate_exit = True
        if args.fail_on_duplicate_names and duplicate_exit:
            return 4
        return 0

    if args.command == "drift-check":
        for idx, skills_dir in enumerate(resolve_skills_dirs(args.skills_dirs)):
            if idx > 0:
                print()
            print(f"skills-dir: {skills_dir}")
            statuses = scan_skills(skills_dir)
            drifts: List[DriftStatus] = []
            for s in statuses:
                if s.entry_type == "symlink" and s.link_status == "ok" and s.resolved_target:
                    drifts.append(check_drift_for_path(s.name, Path(s.resolved_target)))
            print_drift_report(drifts)
        return 0

    if args.command == "sync":
        if args.target_platform and not args.discovery_profile:
            print(
                "error: --target-platform requires --discovery-profile "
                "(need source path → platform tags).",
                file=sys.stderr,
            )
            return 2
        map_file = Path(args.map_file).expanduser()
        mapping = load_mapping(map_file)
        sync_specs: Optional[List[SourceSpec]] = None
        if args.discovery_profile:
            prof = load_discovery_profile(Path(args.discovery_profile).expanduser())
            sync_specs = prof["source_specs"]  # type: ignore[assignment]
        roots = resolve_skills_dirs(args.skills_dirs)
        for idx, skills_dir in enumerate(roots):
            if idx > 0:
                print()
            print(f"skills-dir: {skills_dir}")
            actions = plan_sync(
                skills_dir,
                mapping,
                target_platform=args.target_platform,
                source_specs=sync_specs,
            )
            print_plan(actions, args.apply)
            if args.apply:
                apply_actions(skills_dir, actions)
        if args.apply:
            print("\nApplied actions. Re-run audit to verify final state.")
        return 0

    if args.command == "dedup":
        for idx, skills_dir in enumerate(resolve_skills_dirs(args.skills_dirs)):
            if idx > 0:
                print()
            print(f"skills-dir: {skills_dir}")
            actions, findings = plan_dedup(skills_dir)
            print_dedup_plan(actions, findings, args.apply)
            if args.apply and actions:
                applied = apply_dedup(actions)
                print(f"\nApplied: {applied} symlink(s). Re-run audit to verify.")
        return 0

    if args.command == "route":
        td = Path(args.trace_dir).expanduser() if args.trace_dir else None
        for idx, skills_dir in enumerate(resolve_skills_dirs(args.skills_dirs)):
            if idx > 0:
                print()
            print(f"skills-dir: {skills_dir}")
            trace, actions = route_pipeline(
                skills_dir,
                active_platform=args.platform,
                resolve_strategy=args.strategy,
                trace_dir=td,
            )
            print_route_plan(trace, actions, args.apply)
            if args.apply and actions:
                applied = apply_route(actions, skills_dir)
                print(f"\nApplied: {applied} action(s). Re-run audit to verify.")
        return 0

    if args.command == "audit-state-machine":
        from skills_auditor.state_machine import audit_traces, load_traces as _load_traces
        td = Path(args.trace_dir).expanduser() if args.trace_dir else None
        traces = _load_traces(td)
        if not traces:
            print("No traces found. Run 'route' first to generate trace data.")
            return 0
        findings = audit_traces(traces)
        print(f"traces analyzed: {len(traces)}")
        print(f"findings: {len(findings)}")
        errors = [f for f in findings if f.severity == "error"]
        warnings = [f for f in findings if f.severity == "warning"]
        infos = [f for f in findings if f.severity == "info"]
        print(f"  errors: {len(errors)}, warnings: {len(warnings)}, info: {len(infos)}")
        for f in findings:
            prefix = {"error": "ERR", "warning": "WARN", "info": "INFO"}.get(f.severity, "?")
            parts = [f"[{prefix}] {f.check}: {f.detail}"]
            if f.run_id:
                parts.append(f"run={f.run_id}")
            if f.skill_name:
                parts.append(f"skill={f.skill_name}")
            if f.variant_path:
                parts.append(f"path={f.variant_path}")
            print("  " + "  ".join(parts))
        print("\njson:")
        print(json.dumps(
            [asdict(f) for f in findings],
            indent=2, ensure_ascii=False,
        ))
        return 1 if errors else 0

    if args.command == "audit-discovery":
        profile: Dict[str, object] = {}
        profile_excluded: List[Path] = []
        profile_collapse = True
        source_specs: List[SourceSpec] = []

        cli_sources = [Path(s).expanduser() for s in args.source]
        cli_excluded = [Path(s).expanduser() for s in args.exclude_source]

        if args.profile_file:
            profile = load_discovery_profile(Path(args.profile_file).expanduser())
            source_specs = profile["source_specs"]  # type: ignore[assignment]
            profile_excluded = [
                Path(s).expanduser() for s in profile.get("exclude_sources", [])
            ]
            profile_collapse = bool(profile.get("collapse_identical", True))

        if cli_sources:
            source_specs = [
                SourceSpec(p, [PLATFORM_WILDCARD]) for p in cli_sources
            ]
        elif not source_specs:
            defaults = default_discovery_sources()
            source_specs = [
                SourceSpec(p, infer_default_platforms_for_source(p)) for p in defaults
            ]

        excluded_sources = profile_excluded + cli_excluded
        collapse_identical = profile_collapse and (not args.no_collapse_identical)

        all_items: List[DiscoveryItem] = []
        for idx, spec in enumerate(source_specs):
            all_items.extend(
                discover_from_source(
                    spec.path, idx, excluded_sources, spec.platforms,
                    exclude_patterns=spec.exclude_patterns,
                )
            )

        choices, canonical_items = build_discovery(all_items, collapse_identical=collapse_identical)
        summary = summarize_discovery(choices)
        print_discovery_report(
            source_specs,
            excluded_sources,
            collapse_identical,
            all_items,
            choices,
            canonical_items,
            summary,
            args.summary_only,
        )
        if args.fail_on_conflict and summary.duplicate_skills > 0:
            print("\nFAIL: duplicate skills remain after collapse.")
            return 2
        if args.fail_on_hash_conflict and summary.hash_conflict_skills > 0:
            print("\nFAIL: hash conflicts detected.")
            return 3
        return 0

    parser.print_help()
    return 1
