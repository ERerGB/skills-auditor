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
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple


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


def discover_from_source(
    source_root: Path,
    source_priority: int,
    excluded_roots: List[Path],
) -> List[DiscoveryItem]:
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
                    )
                )

    # Also scan recursively to capture nested distribution layouts.
    for skill_md in sorted(source_root.rglob("SKILL.md"), key=lambda p: str(p).lower()):
        if is_path_excluded(skill_md, excluded_roots):
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


def load_discovery_profile(profile_file: Path) -> Dict[str, object]:
    data = json.loads(profile_file.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Discovery profile must be a JSON object.")
    sources = data.get("sources", [])
    exclude_sources = data.get("exclude_sources", [])
    collapse_identical = data.get("collapse_identical", True)
    if not isinstance(sources, list) or not all(isinstance(x, str) for x in sources):
        raise ValueError("'sources' must be a string list.")
    if not isinstance(exclude_sources, list) or not all(isinstance(x, str) for x in exclude_sources):
        raise ValueError("'exclude_sources' must be a string list.")
    if not isinstance(collapse_identical, bool):
        raise ValueError("'collapse_identical' must be a boolean.")
    return {
        "sources": sources,
        "exclude_sources": exclude_sources,
        "collapse_identical": collapse_identical,
    }


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


def plan_sync(skills_dir: Path, mapping: Dict[str, str]) -> List[SyncAction]:
    actions: List[SyncAction] = []
    for name, target_str in mapping.items():
        entry = skills_dir / name
        target = Path(target_str).expanduser()
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

        if action.action in {"noop", "skip_error"}:
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
    sources: List[Path],
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
    for idx, src in enumerate(sources):
        print(f"{idx}\t{src.expanduser()}")
    print("\nexcluded source roots:")
    if excluded_sources:
        for src in excluded_sources:
            print(f"- {src.expanduser()}")
    else:
        print("- (none)")
    print(f"\ncollapse_identical: {str(collapse_identical).lower()}")

    print("\nall discovered candidates:")
    print("skill_name\tsource_priority\tsource_root\tskill_root\thash")
    for item in sorted(items, key=lambda x: (x.skill_name, x.source_priority, x.skill_root)):
        print(
            f"{item.skill_name}\t{item.source_priority}\t{item.source_root}\t"
            f"{item.skill_root}\t{item.content_hash[:12]}"
        )

    print("\ncanonical injection preview:")
    print(
        "skill_name\tcanonical_skill_root\ttotal_candidates\teffective_candidates\t"
        "collapsed_identical\thash_conflict"
    )
    for choice in choices:
        print(
            f"{choice.skill_name}\t{choice.canonical_skill_root}\t"
            f"{choice.total_candidates}\t{choice.effective_candidates}\t"
            f"{len(choice.collapsed_identical_roots)}\t{str(choice.hash_conflict).lower()}"
        )

    print("\njson:")
    print(
        json.dumps(
            {
                "sources": [str(s.expanduser()) for s in sources],
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
    p_sync.add_argument("--apply", action="store_true", help="Apply actions (default is dry-run)")

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
        map_file = Path(args.map_file).expanduser()
        mapping = load_mapping(map_file)
        roots = resolve_skills_dirs(args.skills_dirs)
        for idx, skills_dir in enumerate(roots):
            if idx > 0:
                print()
            print(f"skills-dir: {skills_dir}")
            actions = plan_sync(skills_dir, mapping)
            print_plan(actions, args.apply)
            if args.apply:
                apply_actions(skills_dir, actions)
        if args.apply:
            print("\nApplied actions. Re-run audit to verify final state.")
        return 0

    if args.command == "audit-discovery":
        profile: Dict[str, object] = {}
        if args.profile_file:
            profile = load_discovery_profile(Path(args.profile_file).expanduser())

        profile_sources = [Path(s).expanduser() for s in profile.get("sources", [])]
        profile_excluded = [Path(s).expanduser() for s in profile.get("exclude_sources", [])]
        profile_collapse = bool(profile.get("collapse_identical", True))

        cli_sources = [Path(s).expanduser() for s in args.source]
        cli_excluded = [Path(s).expanduser() for s in args.exclude_source]

        sources = cli_sources or profile_sources or default_discovery_sources()
        excluded_sources = profile_excluded + cli_excluded
        collapse_identical = profile_collapse and (not args.no_collapse_identical)

        all_items: List[DiscoveryItem] = []
        for idx, src in enumerate(sources):
            all_items.extend(discover_from_source(src, idx, excluded_sources))

        choices, canonical_items = build_discovery(all_items, collapse_identical=collapse_identical)
        summary = summarize_discovery(choices)
        print_discovery_report(
            sources,
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
