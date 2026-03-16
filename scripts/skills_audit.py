#!/usr/bin/env python3
"""Audit and synchronize local skill directories.

Default behavior is dry-run. Use --apply to perform filesystem changes.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional


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


def print_audit(statuses: List[EntryStatus]) -> None:
    print("name\tentry_type\tlink_status\thas_skill_md\tresolved_target")
    for item in statuses:
        print(
            f"{item.name}\t{item.entry_type}\t{item.link_status or '-'}\t"
            f"{str(item.has_skill_md).lower()}\t{item.resolved_target or '-'}"
        )
    print("\njson:")
    print(json.dumps([asdict(s) for s in statuses], indent=2, ensure_ascii=False))


def print_plan(actions: List[SyncAction], apply: bool) -> None:
    mode = "APPLY" if apply else "DRY-RUN"
    print(f"mode: {mode}")
    print("name\taction\treason\texpected_target")
    for a in actions:
        print(f"{a.name}\t{a.action}\t{a.reason}\t{a.expected_target}")
    print("\njson:")
    print(json.dumps([asdict(a) for a in actions], indent=2, ensure_ascii=False))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit and sync local skill folders.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_audit = sub.add_parser("audit", help="Audit current skills directory state")
    p_audit.add_argument("--skills-dir", default="~/.cursor/skills", help="Skill root directory")

    p_sync = sub.add_parser("sync", help="Plan or apply skill relinking based on map file")
    p_sync.add_argument("--skills-dir", default="~/.cursor/skills", help="Skill root directory")
    p_sync.add_argument("--map-file", required=True, help="JSON map file: {name: targetPath}")
    p_sync.add_argument("--apply", action="store_true", help="Apply actions (default is dry-run)")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    skills_dir = Path(args.skills_dir).expanduser()
    if args.command == "audit":
        statuses = scan_skills(skills_dir)
        print_audit(statuses)
        return 0

    if args.command == "sync":
        map_file = Path(args.map_file).expanduser()
        mapping = load_mapping(map_file)
        actions = plan_sync(skills_dir, mapping)
        print_plan(actions, args.apply)
        if args.apply:
            apply_actions(skills_dir, actions)
            print("\nApplied actions. Re-run audit to verify final state.")
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
