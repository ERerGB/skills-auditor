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

from skills_auditor import __version__
from skills_auditor.environments import BUILTIN_ENVIRONMENTS, builtin_project_skill_roots
from skills_auditor.ledger import DEFAULT_LEDGER_ROOT, VALID_RESOURCE_CLASSES, VALID_STATUSES

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
    action: str  # noop | create_link | replace_link | archive_and_link | skip_error
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
    # Lines from `git status --porcelain` for the entire repository (monorepo-wide).
    dirty_count: int
    # Lines from `git status --porcelain -- <skill-relpath>` scoped to this skill tree.
    skill_dirty_count: int
    synced: bool
    # When synced, display_target shows the remote URL; otherwise local path
    display_target: str
    error: Optional[str] = None


@dataclass
class MetadataFinding:
    skill_md_path: str
    severity: str
    code: str
    message: str


@dataclass
class MetadataRepairAction:
    skill_md_path: str
    action: str  # repair | skip
    reason: str
    codes: List[str]


_SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_FRONTMATTER_SCALAR_RE = re.compile(r"^([^:#][^:]*):\s*(.*)$")
_AUTO_REPAIR_METADATA_CODES = frozenset(
    {
        "missing_frontmatter",
        "missing_name",
        "missing_description",
        "invalid_name",
        "unsafe_plain_scalar",
    }
)


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


def _porcelain_line_count(git_root: Path, pathspec: Optional[str]) -> int:
    """Count porcelain lines; if pathspec is set, scope to that path under git_root."""
    args = ["status", "--porcelain"]
    if pathspec:
        args.extend(["--", pathspec])
    out = _git(args, git_root)
    return len(out.splitlines()) if out else 0


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
            skill_dirty_count=0,
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

    skill_dirty_count = dirty_count
    try:
        rel = resolved.relative_to(git_root_path)
        rel_spec = rel.as_posix()
        if rel_spec != ".":
            skill_dirty_count = _porcelain_line_count(git_root_path, rel_spec)
        # else: resolved path is repo root — skill scope equals full repo
    except ValueError:
        # Skill path outside git root (unusual); treat skill scope as full-repo count
        skill_dirty_count = dirty_count

    synced = ahead == 0 and behind == 0 and dirty_count == 0

    # Build a human-friendly remote display: github URL without .git suffix
    display = local_str
    if synced and remote_url:
        display = remote_url.removesuffix(".git")

    return DriftStatus(
        name=name, local_path=local_str, remote_url=remote_url,
        branch=branch, ahead=ahead, behind=behind,
        dirty_count=dirty_count, skill_dirty_count=skill_dirty_count,
        synced=synced,
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
    # Parse frontmatter first, then fallback to folder name. Slash names are
    # valid logical names; sync-discover turns them into host-safe aliases.
    match = re.search(r"(?m)^name:\s*[\'\"]?([^\'\"\n]+?)[\'\"]?\s*$", text)
    if match:
        return match.group(1).strip()
    return skill_md.parent.name


def _extract_frontmatter(text: str) -> Tuple[Optional[List[str]], Optional[str]]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, "missing_frontmatter"
    for idx, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return lines[1:idx], None
    return None, "unclosed_frontmatter"


def _frontmatter_end_index(lines: List[str]) -> Optional[int]:
    if not lines or lines[0].strip() != "---":
        return None
    for idx, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return idx
    return None


def _clean_frontmatter_value(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1].strip()
    return value


def _frontmatter_value_is_quoted(value: str) -> bool:
    value = value.strip()
    return len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}


def _parse_frontmatter_scalars(
    lines: List[str],
    skill_md: Path,
) -> Tuple[Dict[str, str], List[MetadataFinding]]:
    fields: Dict[str, str] = {}
    findings: List[MetadataFinding] = []
    current_block_key: Optional[str] = None

    for lineno, line in enumerate(lines, start=2):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        if line[:1].isspace():
            if current_block_key and stripped:
                fields[current_block_key] = (fields[current_block_key] + " " + stripped).strip()
            continue

        current_block_key = None
        match = _FRONTMATTER_SCALAR_RE.match(line)
        if not match:
            findings.append(
                MetadataFinding(
                    str(skill_md),
                    "error",
                    "malformed_frontmatter_line",
                    f"line {lineno}: expected a top-level `key: value` entry",
                )
            )
            continue

        key = match.group(1).strip()
        raw_value = match.group(2).strip()
        value = _clean_frontmatter_value(raw_value)
        if (
            raw_value not in {">", "|", ">-", "|-"}
            and not _frontmatter_value_is_quoted(raw_value)
            and ": " in raw_value
        ):
            findings.append(
                MetadataFinding(
                    str(skill_md),
                    "error",
                    "unsafe_plain_scalar",
                    f"line {lineno}: quote `{key}` or use a block scalar because the value contains `: `",
                )
            )
        if key in fields:
            findings.append(
                MetadataFinding(
                    str(skill_md),
                    "error",
                    "duplicate_frontmatter_key",
                    f"line {lineno}: duplicate frontmatter key `{key}`",
                )
            )
        fields[key] = "" if value in {">", "|", ">-", "|-"} else value
        if value in {">", "|", ">-", "|-"}:
            current_block_key = key

    return fields, findings


def validate_skill_metadata(skill_md: Path, platform: str = "codex") -> List[MetadataFinding]:
    text = skill_md.read_text(encoding="utf-8", errors="replace")
    lines, frontmatter_error = _extract_frontmatter(text)
    if frontmatter_error:
        message = (
            "SKILL.md must start with a `---` frontmatter block"
            if frontmatter_error == "missing_frontmatter"
            else "frontmatter block must be closed with `---`"
        )
        return [MetadataFinding(str(skill_md), "error", frontmatter_error, message)]

    assert lines is not None
    fields, findings = _parse_frontmatter_scalars(lines, skill_md)
    required = ["name"]
    if platform == "codex":
        required.append("description")

    for key in required:
        if not fields.get(key, "").strip():
            findings.append(
                MetadataFinding(
                    str(skill_md),
                    "error",
                    f"missing_{key}",
                    f"frontmatter `{key}` is required for {platform}",
                )
            )

    name = fields.get("name", "").strip()
    if name and not _SKILL_NAME_RE.match(name):
        findings.append(
            MetadataFinding(
                str(skill_md),
                "error",
                "invalid_name",
                "frontmatter `name` must start with an alphanumeric character and contain only letters, numbers, dots, underscores, hyphens, or slashes",
            )
        )

    return findings


def _metadata_name_for(skill_md: Path) -> str:
    name = skill_md.parent.name.strip()
    sanitized = re.sub(r"[^A-Za-z0-9._/-]+", "-", name).strip("-._/")
    if not sanitized or not sanitized[0].isalnum():
        sanitized = f"skill-{sanitized}".strip("-")
    return sanitized or "skill"


def _metadata_description_for(skill_md: Path, text: str, name: str) -> str:
    in_code = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("```"):
            in_code = not in_code
            continue
        if in_code or not line:
            continue
        if line == "---" or line.startswith("|"):
            continue
        if line.startswith("#"):
            continue
        if line.startswith(("-", "*")):
            line = line[1:].strip()
        line = re.sub(r"`([^`]+)`", r"\1", line)
        line = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", line)
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            return line[:240]
    return f"Skill instructions for {name}."


def _metadata_description_lines(description: str) -> List[str]:
    return ["description: >", f"  {description}"]


def _frontmatter_scalar_value(text: str, key: str) -> Optional[str]:
    lines, frontmatter_error = _extract_frontmatter(text)
    if frontmatter_error or lines is None:
        return None
    pattern = re.compile(rf"^{re.escape(key)}\s*:\s*(.*)$")
    for line in lines:
        match = pattern.match(line)
        if match:
            value = match.group(1).strip()
            if value in {">", "|", ">-", "|-"}:
                return None
            return _clean_frontmatter_value(value)
    return None


def repair_skill_metadata(
    skill_md: Path,
    platform: str = "codex",
    apply: bool = False,
) -> List[MetadataRepairAction]:
    findings = validate_skill_metadata(skill_md, platform=platform)
    if not findings:
        return []

    codes = [f.code for f in findings]
    unsupported = [c for c in codes if c not in _AUTO_REPAIR_METADATA_CODES]
    if unsupported:
        return [
            MetadataRepairAction(
                str(skill_md),
                "skip",
                "metadata findings require manual repair",
                codes,
            )
        ]

    text = skill_md.read_text(encoding="utf-8", errors="replace")
    name = _metadata_name_for(skill_md)
    description = (
        _frontmatter_scalar_value(text, "description")
        if "unsafe_plain_scalar" in codes
        else None
    ) or _metadata_description_for(skill_md, text, name)

    if "missing_frontmatter" in codes:
        new_text = (
            "---\n"
            f"name: {name}\n"
            + "\n".join(_metadata_description_lines(description))
            + "\n"
            "---\n\n"
            f"{text}"
        )
    else:
        lines = text.splitlines()
        end_idx = _frontmatter_end_index(lines)
        if end_idx is None:
            return [
                MetadataRepairAction(
                    str(skill_md),
                    "skip",
                    "frontmatter block is not safely editable",
                    codes,
                )
            ]

        frontmatter = lines[: end_idx + 1]
        body = lines[end_idx + 1 :]

        if "invalid_name" in codes:
            for idx in range(1, end_idx):
                if re.match(r"^name\s*:", frontmatter[idx]):
                    frontmatter[idx] = f"name: {name}"
                    break
        if "unsafe_plain_scalar" in codes:
            for idx in range(1, len(frontmatter) - 1):
                if re.match(r"^description\s*:", frontmatter[idx]):
                    frontmatter[idx: idx + 1] = _metadata_description_lines(description)
                    end_idx += 1
                    break
        if "missing_name" in codes:
            frontmatter.insert(1, f"name: {name}")
            end_idx += 1
        if platform == "codex" and "missing_description" in codes:
            frontmatter[end_idx:end_idx] = _metadata_description_lines(description)

        new_text = "\n".join(frontmatter + body)
        if text.endswith("\n"):
            new_text += "\n"

    if apply:
        skill_md.write_text(new_text, encoding="utf-8")

    return [
        MetadataRepairAction(
            str(skill_md),
            "repair",
            "added or normalized Codex frontmatter metadata",
            codes,
        )
    ]


def collect_metadata_findings(skills_dir: Path, platform: str = "codex") -> List[MetadataFinding]:
    findings: List[MetadataFinding] = []
    for skill_md in iter_visible_skill_mds(skills_dir):
        findings.extend(validate_skill_metadata(skill_md, platform=platform))
    return findings


def collect_metadata_repair_actions(
    skills_dir: Path,
    platform: str = "codex",
    apply: bool = False,
) -> List[MetadataRepairAction]:
    actions: List[MetadataRepairAction] = []
    for skill_md in iter_visible_skill_mds(skills_dir):
        actions.extend(repair_skill_metadata(skill_md, platform=platform, apply=apply))
    return actions


def iter_visible_skill_mds(skills_dir: Path) -> List[Path]:
    """Return visible SKILL.md files, following top-level skill symlinks.

    ``Path.rglob`` does not descend into symlinked directories on common Python builds,
    but Codex/Cursor install roots often expose skills as top-level symlinks. This
    function treats those symlinks as visible install entries and scans their resolved
    targets while preserving the historical top-level hidden-directory exclusion.
    """
    if not skills_dir.exists() or not skills_dir.is_dir():
        return []

    roots: List[Path] = []
    own_skill_md = skills_dir / "SKILL.md"
    if own_skill_md.exists():
        roots.append(skills_dir)

    for child in sorted(skills_dir.iterdir(), key=lambda p: p.name.lower()):
        if child.name.startswith("."):
            continue
        if child.is_symlink():
            try:
                resolved = child.resolve()
            except OSError:
                continue
            if resolved.is_dir():
                roots.append(resolved)
        elif child.is_dir():
            roots.append(child)

    out: List[Path] = []
    seen: set[str] = set()
    for root in roots:
        for skill_md in sorted(root.rglob("SKILL.md"), key=lambda p: str(p).lower()):
            if _skill_md_path_is_under_ignored_segment(skill_md):
                continue
            try:
                key = str(skill_md.resolve())
            except OSError:
                continue
            if key in seen:
                continue
            seen.add(key)
            out.append(skill_md)
    return out


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


def _skill_md_under_visible_install_tree(skill_md: Path, skills_dir: Path) -> bool:
    """Match historical behavior: never scan under top-level hidden dirs of skills_dir.

    Nested segments like `.github/` or `.agents/` are still allowed — only the first
    path component under *skills_dir* must not start with a dot.
    """
    try:
        rel = skill_md.relative_to(skills_dir)
    except ValueError:
        return False
    if not rel.parts:
        return False
    return not rel.parts[0].startswith(".")


@dataclass
class DuplicateSkillNameFinding:
    """Same frontmatter `name:` declared by more than one distinct SKILL.md under one skills install root.

    Paths listed are one representative path per resolved real file (symlinks to the same
    inode count once).

    ``bundle`` is the **top-level skill folder** containing the canonical (shortest-path) file,
    for stable reporting and trace identity — even when duplicates span sibling folders
    (e.g. ``browse/SKILL.md`` vs ``gstack/browse/SKILL.md``).
    """

    bundle: str
    skill_name: str
    skill_md_paths: List[str]


def collect_duplicate_skill_names(skills_dir: Path) -> List[DuplicateSkillNameFinding]:
    """Find duplicate ``name:`` values across **all** SKILL.md files under *skills_dir*.

    This matches how Slash / many hosts index skills: recursive discovery over the install
    root, not isolated per top-level child folder.

    Covers:

    - Nested mirrors inside one pack (``gstack/SKILL.md`` vs ``gstack/.agents/.../SKILL.md``).
    - **Cross-folder duplicates** that old per-bundle scans missed (e.g. both
      ``<root>/browse/SKILL.md`` and ``<root>/gstack/browse/SKILL.md``), which inflate Slash lists.

    Multiple paths that are symlinks to the same resolved file are folded into one entry
    (dedupe by ``Path.resolve()``), avoiding false positives for DRY symlink layouts
    (see https://github.com/ERerGB/skills-auditor/issues/2).
    """
    findings: List[DuplicateSkillNameFinding] = []
    if not skills_dir.exists() or not skills_dir.is_dir():
        return findings

    # name -> { resolved_realpath_str: representative Path }
    by_name: Dict[str, Dict[str, Path]] = {}
    try:
        for skill_md in skills_dir.rglob("SKILL.md"):
            if not _skill_md_under_visible_install_tree(skill_md, skills_dir):
                continue
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
        return findings

    for skill_name in sorted(by_name.keys()):
        reps = sorted(by_name[skill_name].values(), key=lambda p: str(p).lower())
        if len(reps) <= 1:
            continue
        canonical = min(reps, key=lambda p: (len(str(p)), str(p).lower()))
        try:
            bundle_label = _bundle_root_for(canonical, skills_dir).name
        except ValueError:
            bundle_label = canonical.parent.name
        findings.append(
            DuplicateSkillNameFinding(
                bundle=bundle_label,
                skill_name=skill_name,
                skill_md_paths=[str(p) for p in reps],
            )
        )
    return findings


def print_duplicate_name_check(
    skills_dir: Path,
    findings: List[DuplicateSkillNameFinding],
) -> None:
    print("\nduplicate frontmatter name: check (install root / Slash host view)")
    print(
        "Detects multiple distinct SKILL.md files (by resolved path) declaring the same `name:` "
        "anywhere under this install root — including sibling-folder duplicates "
        "(e.g. browse/ vs gstack/browse/). Symlinks to the same file count once."
    )
    if not findings:
        print("status: ok (no duplicate names under this install root)")
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


def print_metadata_check(
    skills_dir: Path,
    findings: List[MetadataFinding],
    platform: str,
) -> None:
    print(f"\nmetadata: check (platform={platform})")
    if not findings:
        print(f"status: ok (no frontmatter metadata problems under {skills_dir})")
        print("\njson:")
        print(json.dumps({"skills_dir": str(skills_dir), "platform": platform, "findings": []}, indent=2))
        return

    print("status: findings present")
    print("severity\tcode\tpath\tmessage")
    for f in findings:
        print(f"{f.severity}\t{f.code}\t{f.skill_md_path}\t{f.message}")
    print("\njson:")
    print(
        json.dumps(
            {
                "skills_dir": str(skills_dir),
                "platform": platform,
                "findings": [asdict(f) for f in findings],
            },
            indent=2,
            ensure_ascii=False,
        )
    )


def print_metadata_repair(
    skills_dir: Path,
    actions: List[MetadataRepairAction],
    platform: str,
    apply: bool,
) -> None:
    mode = "APPLY" if apply else "DRY-RUN"
    print(f"\nmetadata-repair mode: {mode} (platform={platform})")
    if not actions:
        print(f"status: ok (no metadata repairs needed under {skills_dir})")
        print("\njson:")
        print(
            json.dumps(
                {
                    "skills_dir": str(skills_dir),
                    "platform": platform,
                    "mode": mode.lower(),
                    "actions": [],
                },
                indent=2,
            )
        )
        return

    repairs = [a for a in actions if a.action == "repair"]
    skips = [a for a in actions if a.action == "skip"]
    print(f"planned: {len(repairs)} repair(s), {len(skips)} skip(s)")
    print("action\tpath\tcodes\treason")
    for a in actions:
        print(f"{a.action}\t{a.skill_md_path}\t{','.join(a.codes)}\t{a.reason}")
    print("\njson:")
    print(
        json.dumps(
            {
                "skills_dir": str(skills_dir),
                "platform": platform,
                "mode": mode.lower(),
                "actions": [asdict(a) for a in actions],
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


def directory_tree_hash(root: Path, *, reject_external_symlinks: bool = False) -> str:
    """Hash a directory's complete, non-following filesystem tree.

    Regular-file bytes and permission bits are covered. Symlinks contribute their
    literal link target and are never followed, so hashing cannot escape ``root``.
    """

    records: List[List[str]] = []
    resolved_root = root.resolve(strict=False)

    def walk(current: Path) -> None:
        for path in sorted(current.iterdir(), key=lambda item: item.name):
            relative = path.relative_to(root).as_posix()
            mode = format(path.lstat().st_mode & 0o7777, "04o")
            if path.is_symlink():
                if reject_external_symlinks:
                    try:
                        path.resolve(strict=False).relative_to(resolved_root)
                    except ValueError as exc:
                        raise ValueError(f"source symlink escapes skill tree: {path}") from exc
                records.append(["symlink", relative, mode, os.readlink(path)])
            elif path.is_dir():
                records.append(["directory", relative, mode])
                walk(path)
            elif path.is_file():
                records.append(["file", relative, mode, file_hash(path)])
            else:
                records.append(["other", relative, mode])

    walk(root)
    payload = json.dumps(records, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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



def skill_alias_for_install_root(logical_name: str) -> str:
    """Return a top-level, host-safe folder name for a logical skill name."""
    alias = re.sub(r"[\\/]+", "-", logical_name)
    alias = re.sub(r"[^A-Za-z0-9._-]+", "-", alias)
    alias = re.sub(r"-+", "-", alias).strip("-")
    return alias


def _walk_skill_dirs(source_root: Path) -> List[Path]:
    """Recursively find directories containing SKILL.md under one source root.

    Source roots often contain symlinked skill directories. Follow directory
    symlinks so those installed entries can be replicated into other agent
    environments, while tracking resolved directories to avoid cycles.
    """
    if not source_root.exists() or not source_root.is_dir():
        return []

    out: List[Path] = []
    seen_dirs: set[str] = set()

    def walk_dir(current: Path) -> None:
        try:
            resolved_current = str(current.resolve())
        except OSError:
            return
        if resolved_current in seen_dirs:
            return
        seen_dirs.add(resolved_current)

        skill_md = current / "SKILL.md"
        if skill_md.exists() and not _skill_md_path_is_under_ignored_segment(skill_md):
            out.append(current)

        try:
            entries = sorted(current.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            return
        for entry in entries:
            if entry.name in _IGNORE_SKILL_SCAN_SEGMENTS:
                continue
            try:
                if not entry.is_dir():
                    continue
            except OSError:
                continue
            walk_dir(entry)

    walk_dir(source_root)
    return out


def discover_sync_mapping(
    sources: List[Path],
    *,
    exclude_target_root: Optional[Path] = None,
) -> Dict[str, str]:
    """Build a sync map by discovering SKILL.md files under source roots.

    The returned mapping is suitable for ``plan_sync``: ``{install_alias: absolute_path}``.
    If ``exclude_target_root`` is passed, a skill whose canonical path already occupies
    ``exclude_target_root / alias`` is omitted for that target to avoid backing up a
    canonical local directory and replacing it with a symlink to itself.
    """
    by_alias: Dict[str, Tuple[str, str]] = {}
    seen_resolved: set[str] = set()
    conflicts: List[str] = []
    excluded_root = exclude_target_root.expanduser().resolve() if exclude_target_root else None

    for source in sources:
        source_root = source.expanduser()
        if not source_root.exists():
            continue
        for skill_root in _walk_skill_dirs(source_root):
            skill_md = skill_root / "SKILL.md"
            try:
                logical_name = parse_skill_name(skill_md)
                resolved = skill_root.resolve()
            except OSError:
                continue
            alias = skill_alias_for_install_root(logical_name)
            if not alias:
                raise ValueError(f"Could not derive install alias for {skill_md}")

            if excluded_root is not None and resolved == excluded_root / alias:
                continue

            resolved_str = str(resolved)
            if resolved_str in seen_resolved:
                continue
            seen_resolved.add(resolved_str)

            content_hash = directory_tree_hash(resolved)
            previous = by_alias.get(alias)
            if previous and previous[0] != resolved_str:
                previous_hash = directory_tree_hash(Path(previous[0]))
                if previous_hash != content_hash:
                    conflicts.append(f"{alias}: {previous[0]} vs {resolved_str}")
                    continue
            if previous is None:
                by_alias[alias] = (resolved_str, logical_name)

    if conflicts:
        raise ValueError("Conflicting skill aliases:\n" + "\n".join(conflicts))
    return {alias: value for alias, (value, _logical) in sorted(by_alias.items())}


def default_sync_discover_sources(include_global_sources: bool) -> List[Path]:
    cwd = Path.cwd()
    sources = [cwd / ".agents" / "skills", *builtin_project_skill_roots(cwd)]
    if include_global_sources:
        home = Path("~").expanduser()
        sources.extend(
            root
            for environment in BUILTIN_ENVIRONMENTS.all()
            for root in environment.global_roots(home)
        )
    return sources


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
                    action="archive_and_link",
                    reason="native entry exists and must be archived before linking",
                )
            )
            continue

    return actions


def apply_actions(skills_dir: Path, actions: List[SyncAction]) -> None:
    actionable = {"create_link", "replace_link", "archive_and_link"}
    if any(action.action in actionable for action in actions):
        skills_dir.mkdir(parents=True, exist_ok=True)
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

        if action.action == "archive_and_link":
            archive_name = f"{action.name}.archived-{timestamp}"
            archive_path = skills_dir / archive_name
            entry.rename(archive_path)
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
            elif (
                drift.ahead == 0
                and drift.behind == 0
                and drift.skill_dirty_count == 0
                and drift.dirty_count > 0
            ):
                # Monorepo: skill subtree clean; other paths in the repo are dirty
                row += f"\tskill_clean (repo_dirty={drift.dirty_count})"
            else:
                parts = []
                if drift.ahead > 0:
                    parts.append(f"ahead={drift.ahead}")
                if drift.behind > 0:
                    parts.append(f"behind={drift.behind}")
                if drift.dirty_count > 0:
                    parts.append(f"repo_dirty={drift.dirty_count}")
                if drift.skill_dirty_count > 0:
                    parts.append(f"skill_dirty={drift.skill_dirty_count}")
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
    print("name\tsynced\tbranch\tahead\tbehind\trepo_dirty\tskill_dirty\tdisplay_target")
    for d in drifts:
        sync_label = "synced" if d.synced else "DRIFT"
        if d.error:
            sync_label = d.error
        print(
            f"{d.name}\t{sync_label}\t{d.branch or '-'}\t"
            f"{d.ahead}\t{d.behind}\t{d.dirty_count}\t{d.skill_dirty_count}\t{d.display_target}"
        )

    synced_count = sum(1 for d in drifts if d.synced)
    drift_count = sum(1 for d in drifts if not d.synced and not d.error)
    error_count = sum(1 for d in drifts if d.error)
    print(f"\nsummary: {synced_count} synced, {drift_count} drifted, {error_count} errors")

    print("\njson:")
    print(json.dumps([asdict(d) for d in drifts], indent=2, ensure_ascii=False))


def build_parser(prog: Optional[str] = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Audit and sync local skill folders.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_integrate = sub.add_parser(
        "integrate",
        help="Create and save a reviewable integration plan for named host targets",
    )
    p_integrate.add_argument(
        "--config",
        default="",
        help="Integration spec JSON (default: ./skills-auditor.json when present).",
    )
    p_integrate.add_argument(
        "--source",
        action="append",
        default=[],
        help="Canonical SKILL.md source root. Repeatable; overrides config sources.",
    )
    p_integrate.add_argument(
        "--target",
        action="append",
        default=[],
        help="Named host target, optionally @global (cursor, claude-code, codex). Repeatable.",
    )
    p_integrate.add_argument(
        "--target-root",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="Custom host target root. Repeatable.",
    )
    p_integrate.add_argument(
        "--metadata-platform",
        default="",
        help="Metadata profile override (default: config value or codex).",
    )
    p_integrate.add_argument(
        "--plan-out",
        default="",
        help="Plan output path (default: .skills-auditor-local/plans/<plan-id>.json).",
    )
    p_integrate.add_argument(
        "--format",
        choices=["human", "json"],
        default="human",
        help="Output format; json writes one JSON object to stdout.",
    )

    p_apply_plan = sub.add_parser(
        "apply",
        help="Apply one reviewed integration plan without rediscovering sources",
    )
    p_apply_plan.add_argument("plan", help="Path to a skills-auditor-plan/v1 JSON file.")
    p_apply_plan.add_argument(
        "--receipt-out",
        default="",
        help="Receipt output path (default: .skills-auditor-local/receipts/<receipt-id>.json).",
    )
    p_apply_plan.add_argument(
        "--format",
        choices=["human", "json"],
        default="human",
        help="Output format; json writes one JSON object to stdout.",
    )

    p_verify_receipt = sub.add_parser(
        "verify",
        help="Verify installed links and source-tree hashes from an integration receipt",
    )
    p_verify_receipt.add_argument("receipt", help="Path to a skills-auditor-receipt/v1 JSON file.")
    p_verify_receipt.add_argument(
        "--format",
        choices=["human", "json"],
        default="human",
        help="Output format; json writes one JSON object to stdout.",
    )

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
        help=(
            "Skip the default scan for duplicate `name:` across SKILL.md under this install root "
            "(Slash-style recursive view)."
        ),
    )
    p_audit.add_argument(
        "--fail-on-duplicate-names",
        action="store_true",
        help=(
            "Exit with code 4 if this install root has multiple SKILL.md declaring the same name."
        ),
    )
    p_audit.add_argument(
        "--check-metadata",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    p_audit.add_argument(
        "--skip-metadata-check",
        action="store_true",
        help="Skip the default SKILL.md frontmatter metadata validation.",
    )
    p_audit.add_argument(
        "--metadata-platform",
        default="codex",
        help="Metadata validation platform profile (default: codex).",
    )
    p_audit.add_argument(
        "--fail-on-invalid-metadata",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    p_audit.add_argument(
        "--allow-invalid-metadata",
        action="store_true",
        help="Report metadata findings but do not fail audit with exit code 5.",
    )

    p_metadata = sub.add_parser(
        "metadata",
        help="Validate SKILL.md frontmatter metadata under one or more skill roots",
    )
    p_metadata.add_argument(
        "--skills-dir",
        action="append",
        dest="skills_dirs",
        metavar="DIR",
        help="Skill root (repeat for multiple). Default: ~/.cursor/skills",
    )
    p_metadata.add_argument(
        "--platform",
        default="codex",
        help="Metadata validation platform profile (default: codex).",
    )
    p_metadata.add_argument(
        "--fail-on-invalid",
        action="store_true",
        help="Exit with code 5 if invalid frontmatter metadata is found.",
    )

    p_metadata_repair = sub.add_parser(
        "metadata-repair",
        help="Plan or apply safe, idempotent SKILL.md frontmatter metadata repairs",
    )
    p_metadata_repair.add_argument(
        "--skills-dir",
        action="append",
        dest="skills_dirs",
        metavar="DIR",
        help="Skill root (repeat for multiple). Default: ~/.cursor/skills",
    )
    p_metadata_repair.add_argument(
        "--platform",
        default="codex",
        help="Metadata validation platform profile (default: codex).",
    )
    p_metadata_repair.add_argument(
        "--apply",
        action="store_true",
        help="Apply metadata repairs (default is dry-run).",
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

    p_sync_discover = sub.add_parser(
        "sync-discover",
        help="Discover SKILL.md sources and sync them into one or more install roots",
    )
    p_sync_discover.add_argument(
        "--skills-dir",
        action="append",
        dest="skills_dirs",
        metavar="DIR",
        help="Target skill root. Repeat for multiple. Default: ~/.cursor/skills",
    )
    p_sync_discover.add_argument(
        "--source",
        action="append",
        default=[],
        help=(
            "Source root to scan recursively for SKILL.md. Repeatable. "
            "If omitted, scans .agents/skills plus every registered built-in native "
            "environment under cwd."
        ),
    )
    p_sync_discover.add_argument(
        "--include-global-sources",
        action="store_true",
        help="Also append every built-in native environment's global skill roots.",
    )
    p_sync_discover.add_argument(
        "--apply", action="store_true", help="Apply actions (default is dry-run)"
    )

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

    p_record_trigger = sub.add_parser(
        "record-trigger-log",
        help="Append a local JSONL trigger/observability log event",
    )
    p_record_trigger.add_argument(
        "--kind",
        default="skill-trigger",
        choices=["skill-trigger", "observability-trigger", "trace"],
        help="Event kind to record (default: skill-trigger).",
    )
    p_record_trigger.add_argument(
        "--log-dir",
        default=".skills-auditor-local",
        help="Local log root (default: .skills-auditor-local).",
    )
    p_record_trigger.add_argument("--source", default="manual", help="Producer name.")
    p_record_trigger.add_argument("--prompt-id", default="", help="Stable prompt/case id.")
    p_record_trigger.add_argument("--prompt-hash", default="", help="Hash of the prompt, not raw text.")
    p_record_trigger.add_argument("--prompt-summary", default="", help="Privacy-preserving prompt summary.")
    p_record_trigger.add_argument("--context-summary", default="", help="Recent context summary.")
    p_record_trigger.add_argument("--skill", default="", help="Skill involved in the event.")
    p_record_trigger.add_argument("--expected-skill", default="", help="Expected skill label.")
    p_record_trigger.add_argument("--actual-skill", default="", help="Observed skill label.")
    p_record_trigger.add_argument("--expected-mode", default="", help="Expected mode label.")
    p_record_trigger.add_argument("--actual-mode", default="", help="Observed mode label.")
    p_record_trigger.add_argument("--decision", default="", help="Routing/trigger decision.")
    p_record_trigger.add_argument("--confidence", type=float, default=None, help="Optional confidence score.")
    p_record_trigger.add_argument(
        "--verdict",
        default="unknown",
        help="Regression verdict: correct, incorrect, false-positive, false-negative, ambiguous, unknown.",
    )
    p_record_trigger.add_argument("--trace-path", default="", help="Referenced state-machine trace path.")
    p_record_trigger.add_argument("--notes", default="", help="Short operator note.")

    p_record_sensor = sub.add_parser(
        "record-sensor-event",
        help="Normalize one agent hook/transcript JSON payload into the local sensor log",
    )
    p_record_sensor.add_argument(
        "--provider",
        required=True,
        help="Agent/runtime provider label, e.g. claude-code, codex, cursor, generic.",
    )
    p_record_sensor.add_argument(
        "--source",
        default="hook",
        help="Sensor source type, e.g. hook, transcript, fs-proxy (default: hook).",
    )
    p_record_sensor.add_argument(
        "--log-dir",
        default=".skills-auditor-local",
        help="Local log root (default: .skills-auditor-local).",
    )
    p_record_sensor.add_argument(
        "--input-file",
        default="-",
        help="JSON payload file. Use '-' to read stdin (default: -).",
    )
    p_record_sensor.add_argument(
        "--resolve-path",
        action="store_true",
        help="Resolve an observed access path to realpath when it exists.",
    )
    p_record_sensor.add_argument(
        "--hash-path",
        action="store_true",
        help="Hash the observed file path when it exists and is a regular file.",
    )

    p_audit_trigger_logs = sub.add_parser(
        "audit-trigger-logs",
        help="Validate local trigger/observability logs and print regression counters",
    )
    p_audit_trigger_logs.add_argument(
        "--log-dir",
        default=".skills-auditor-local",
        help="Local log root (default: .skills-auditor-local).",
    )
    p_audit_trigger_logs.add_argument(
        "--kind",
        default="",
        choices=["", "skill-trigger", "observability-trigger", "trace"],
        help="Optional event kind filter.",
    )
    p_audit_trigger_logs.add_argument(
        "--fail-on-error",
        action="store_true",
        help="Exit non-zero when log validation has errors.",
    )

    p_audit_sensor_logs = sub.add_parser(
        "audit-sensor-logs",
        help="Validate local agent sensor logs and print basic counters",
    )
    p_audit_sensor_logs.add_argument(
        "--log-dir",
        default=".skills-auditor-local",
        help="Local log root (default: .skills-auditor-local).",
    )
    p_audit_sensor_logs.add_argument(
        "--provider",
        default="",
        help="Optional provider filter, e.g. claude-code or codex.",
    )
    p_audit_sensor_logs.add_argument(
        "--fail-on-error",
        action="store_true",
        help="Exit non-zero when log validation has errors.",
    )

    p_aggregate_sensor_claims = sub.add_parser(
        "aggregate-sensor-claims",
        help="Dry-run aggregate local sensor events into confidence-rated claims",
    )
    p_aggregate_sensor_claims.add_argument(
        "--log-dir",
        default=".skills-auditor-local",
        help="Local log root (default: .skills-auditor-local).",
    )
    p_aggregate_sensor_claims.add_argument(
        "--provider",
        default="",
        help="Optional provider filter, e.g. claude-code or codex.",
    )

    p_log_stats = sub.add_parser(
        "log-stats",
        help="Summarize local trigger logs and route traces storage usage",
    )
    p_log_stats.add_argument(
        "--log-dir",
        default=".skills-auditor-local",
        help="Local trigger log root (default: .skills-auditor-local).",
    )
    p_log_stats.add_argument(
        "--trace-dir",
        default="",
        help="Route trace directory (default: ~/.skills-auditor/traces/).",
    )
    p_log_stats.add_argument(
        "--events-per-day",
        type=float,
        default=0.0,
        help="Optional planning input for storage estimate.",
    )
    p_log_stats.add_argument(
        "--retention-days",
        type=int,
        default=30,
        help="Retention window for estimate (default: 30).",
    )
    p_log_stats.add_argument(
        "--index-multiplier",
        type=float,
        default=0.1,
        help="Index/summary overhead as a fraction of raw logs (default: 0.1).",
    )

    p_ledger_create = sub.add_parser(
        "ledger-create",
        help="Create a local skill-run execution ledger",
    )
    p_ledger_create.add_argument(
        "--ledger-dir",
        default=str(DEFAULT_LEDGER_ROOT),
        help=f"Local ledger directory (default: {DEFAULT_LEDGER_ROOT}).",
    )
    p_ledger_create.add_argument(
        "--run-id",
        default="",
        help="Stable run id. If omitted, one is generated.",
    )
    p_ledger_create.add_argument(
        "--source",
        default="manual",
        help="Producer or orchestrator name (default: manual).",
    )
    p_ledger_create.add_argument(
        "--mode",
        default="",
        help="Run mode, e.g. dry-run, apply, review.",
    )

    p_ledger_upsert = sub.add_parser(
        "ledger-upsert",
        help="Create or update one resource row in a local skill-run ledger",
    )
    p_ledger_upsert.add_argument(
        "--ledger-dir",
        default=str(DEFAULT_LEDGER_ROOT),
        help=f"Local ledger directory (default: {DEFAULT_LEDGER_ROOT}).",
    )
    p_ledger_upsert.add_argument("--run-id", required=True, help="Ledger run id.")
    p_ledger_upsert.add_argument(
        "--create-if-missing",
        action="store_true",
        help="Create the ledger before upserting when it does not exist.",
    )
    p_ledger_upsert.add_argument(
        "--source",
        default="manual",
        help="Producer or orchestrator name for created ledgers (default: manual).",
    )
    p_ledger_upsert.add_argument(
        "--mode",
        default="",
        help="Run mode for created ledgers, e.g. dry-run, apply, review.",
    )
    p_ledger_upsert.add_argument("--id", required=True, help="Stable resource id.")
    p_ledger_upsert.add_argument(
        "--class",
        dest="resource_class",
        required=True,
        choices=sorted(VALID_RESOURCE_CLASSES),
        help="Resource class.",
    )
    p_ledger_upsert.add_argument("--locator", required=True, help="Path, URL, trace id, or external locator.")
    p_ledger_upsert.add_argument("--owner", required=True, help="Owning skill, sub-skill, or subagent.")
    p_ledger_upsert.add_argument(
        "--status",
        required=True,
        choices=sorted(VALID_STATUSES),
        help="Resource status.",
    )
    p_ledger_upsert.add_argument("--note", default="", help="Optional note appended to the row.")
    p_ledger_upsert.add_argument(
        "--metadata",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Metadata key/value. Repeatable.",
    )
    p_ledger_upsert.add_argument(
        "--handoff-target",
        default="",
        help="Required target when status=handoff.",
    )
    p_ledger_upsert.add_argument(
        "--blocked-reason",
        default="",
        help="Required reason when status=blocked.",
    )

    p_ledger_check = sub.add_parser(
        "ledger-check",
        help="Validate a local skill-run execution ledger",
    )
    p_ledger_check.add_argument(
        "--ledger-dir",
        default=str(DEFAULT_LEDGER_ROOT),
        help=f"Local ledger directory (default: {DEFAULT_LEDGER_ROOT}).",
    )
    p_ledger_check.add_argument("--run-id", required=True, help="Ledger run id.")
    p_ledger_check.add_argument(
        "--fail-on-warning",
        action="store_true",
        help="Return non-zero when warnings exist, including active resources.",
    )

    p_ledger_summary = sub.add_parser(
        "ledger-summary",
        help="Summarize a local skill-run execution ledger",
    )
    p_ledger_summary.add_argument(
        "--ledger-dir",
        default=str(DEFAULT_LEDGER_ROOT),
        help=f"Local ledger directory (default: {DEFAULT_LEDGER_ROOT}).",
    )
    p_ledger_summary.add_argument(
        "--run-id",
        default="",
        help="Optional ledger run id. If omitted, summarizes every *.json ledger.",
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


def main(prog: Optional[str] = None) -> int:
    parser = build_parser(prog=prog)
    args = parser.parse_args()

    if args.command == "integrate":
        from skills_auditor.integration import (
            IntegrationError,
            build_integration_plan,
            load_integration_spec,
            save_plan,
        )

        try:
            spec = load_integration_spec(
                config_path=Path(args.config) if args.config else None,
                cli_sources=args.source,
                cli_targets=args.target,
                cli_target_roots=args.target_root,
                metadata_platform=args.metadata_platform,
            )
            plan = build_integration_plan(spec)
            plan_path = save_plan(plan, Path(args.plan_out) if args.plan_out else None)
        except IntegrationError as exc:
            if args.format == "json":
                print(json.dumps(exc.to_dict(), indent=2, ensure_ascii=False))
            else:
                print(f"error [{exc.code}]: {exc}", file=sys.stderr)
                for detail in exc.details:
                    print(f"  {json.dumps(detail, ensure_ascii=False)}", file=sys.stderr)
            return exc.exit_code

        if args.format == "json":
            output = dict(plan)
            output["plan_path"] = str(plan_path)
            print(json.dumps(output, indent=2, ensure_ascii=False))
            return 0

        summary = plan["summary"]
        print(f"plan: {plan_path}")
        print(f"plan id: {plan['plan_id']}")
        print(
            f"skills: {summary['skills']} | targets: {summary['targets']} | "
            f"actions: {summary['actions']} | changes: {summary['changes']}"
        )
        for target in plan["targets"]:
            counts: Dict[str, int] = {}
            for action in target["actions"]:
                name = action["action"]
                counts[name] = counts.get(name, 0) + 1
            print(
                f"  {target['environment']}@{target['scope']}: {target['root']} "
                f"{json.dumps(counts, sort_keys=True)}"
            )
        print("\nReview the plan, then apply that exact file:")
        print(f"  skills-audit apply {plan_path}")
        return 0

    if args.command == "apply":
        from skills_auditor.integration import (
            IntegrationError,
            apply_integration_plan,
            load_json_object,
        )

        try:
            plan = load_json_object(Path(args.plan), "plan")
            receipt, receipt_path = apply_integration_plan(
                plan,
                Path(args.receipt_out) if args.receipt_out else None,
            )
        except IntegrationError as exc:
            if args.format == "json":
                print(json.dumps(exc.to_dict(), indent=2, ensure_ascii=False))
            else:
                print(f"error [{exc.code}]: {exc}", file=sys.stderr)
                for detail in exc.details:
                    print(f"  {json.dumps(detail, ensure_ascii=False)}", file=sys.stderr)
            return exc.exit_code

        if args.format == "json":
            output = dict(receipt)
            output["receipt_path"] = str(receipt_path)
            print(json.dumps(output, indent=2, ensure_ascii=False))
        else:
            print(f"receipt: {receipt_path}")
            print(f"receipt id: {receipt['receipt_id']}")
            print(f"plan id: {receipt['plan_id']}")
            print(f"status: {receipt['status']} | verified entries: {len(receipt['results'])}")
            print("\nVerify the installed state:")
            print(f"  skills-audit verify {receipt_path}")
        return 0

    if args.command == "verify":
        from skills_auditor.integration import (
            IntegrationError,
            load_json_object,
            verify_receipt,
        )

        try:
            receipt = load_json_object(Path(args.receipt), "receipt")
            verification = verify_receipt(receipt)
        except IntegrationError as exc:
            if args.format == "json":
                print(json.dumps(exc.to_dict(), indent=2, ensure_ascii=False))
            else:
                print(f"error [{exc.code}]: {exc}", file=sys.stderr)
                for detail in exc.details:
                    print(f"  {json.dumps(detail, ensure_ascii=False)}", file=sys.stderr)
            return exc.exit_code

        if args.format == "json":
            print(json.dumps(verification, indent=2, ensure_ascii=False))
        else:
            summary = verification["summary"]
            approval = verification["approval"]
            print(f"status: {verification['status']}")
            print(
                f"approval: {approval['state']} | re-approval required: "
                f"{'yes' if approval['requires_reapproval'] else 'no'}"
            )
            print(
                f"checks: {summary['checks']} | passed: {summary['passed']} | "
                f"failed: {summary['failed']}"
            )
            for check in verification["checks"]:
                if not check.get("ok"):
                    print(f"  FAIL {check['code']}: {json.dumps(check, ensure_ascii=False)}")
            if approval["requires_reapproval"]:
                print(f"approval reasons: {', '.join(approval['reason_codes'])}")
                print("Next steps:")
                print("  1. Run skills-audit integrate with the same source and target options.")
                print("  2. Review the emitted plan.")
                print("  3. Explicitly re-approve and run skills-audit apply <new-plan.json>.")
        return 0 if verification["status"] == "passed" else 3

    if args.command == "audit":
        duplicate_exit = False
        metadata_exit = False
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
            if not args.skip_metadata_check:
                metadata_findings = collect_metadata_findings(
                    skills_dir,
                    platform=args.metadata_platform,
                )
                print_metadata_check(skills_dir, metadata_findings, args.metadata_platform)
                if metadata_findings:
                    metadata_exit = True
        if metadata_exit and not args.allow_invalid_metadata:
            return 5
        if args.fail_on_duplicate_names and duplicate_exit:
            return 4
        return 0

    if args.command == "metadata":
        metadata_exit = False
        for idx, skills_dir in enumerate(resolve_skills_dirs(args.skills_dirs)):
            if idx > 0:
                print()
            print(f"skills-dir: {skills_dir}")
            findings = collect_metadata_findings(skills_dir, platform=args.platform)
            print_metadata_check(skills_dir, findings, args.platform)
            if findings:
                metadata_exit = True
        if args.fail_on_invalid and metadata_exit:
            return 5
        return 0

    if args.command == "metadata-repair":
        needs_apply = False
        skipped = False
        for idx, skills_dir in enumerate(resolve_skills_dirs(args.skills_dirs)):
            if idx > 0:
                print()
            print(f"skills-dir: {skills_dir}")
            actions = collect_metadata_repair_actions(
                skills_dir,
                platform=args.platform,
                apply=args.apply,
            )
            print_metadata_repair(skills_dir, actions, args.platform, args.apply)
            if any(a.action == "repair" for a in actions):
                needs_apply = True
            if any(a.action == "skip" for a in actions):
                skipped = True
        if skipped:
            return 5
        if needs_apply and not args.apply:
            return 1
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

    if args.command == "sync-discover":
        sources = (
            [Path(s).expanduser() for s in args.source]
            if args.source
            else default_sync_discover_sources(args.include_global_sources)
        )
        roots = resolve_skills_dirs(args.skills_dirs)
        needs_apply = False
        skipped = False
        for idx, skills_dir in enumerate(roots):
            if idx > 0:
                print()
            print(f"skills-dir: {skills_dir}")
            mapping = discover_sync_mapping(sources, exclude_target_root=skills_dir)
            print(f"discovered: {len(mapping)} sync entr{'y' if len(mapping) == 1 else 'ies'}")
            actions = plan_sync(skills_dir, mapping)
            print_plan(actions, args.apply)
            needs_apply = needs_apply or any(
                action.action in {"create_link", "replace_link", "archive_and_link"}
                for action in actions
            )
            skipped = skipped or any(action.action.startswith("skip_") for action in actions)
            if args.apply:
                apply_actions(skills_dir, actions)
        if args.apply:
            print("\nApplied actions. Re-run audit to verify final state.")
        if skipped:
            return 5
        if needs_apply and not args.apply:
            return 1
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

    if args.command == "record-trigger-log":
        from skills_auditor.observability import TriggerLogEvent, write_trigger_log

        event = TriggerLogEvent(
            kind=args.kind,
            source=args.source,
            prompt_id=args.prompt_id,
            prompt_hash=args.prompt_hash,
            prompt_summary=args.prompt_summary,
            context_summary=args.context_summary,
            skill=args.skill,
            expected_skill=args.expected_skill,
            actual_skill=args.actual_skill,
            expected_mode=args.expected_mode,
            actual_mode=args.actual_mode,
            decision=args.decision,
            confidence=args.confidence,
            verdict=args.verdict,
            trace_path=args.trace_path,
            notes=args.notes,
        )
        out = write_trigger_log(event, Path(args.log_dir).expanduser())
        print(f"log written: {out}")
        print("\njson:")
        print(json.dumps(event.to_dict(), indent=2, ensure_ascii=False))
        return 0

    if args.command == "record-sensor-event":
        from skills_auditor.observability import sensor_event_from_payload, write_sensor_event

        if args.input_file == "-":
            raw = sys.stdin.read()
        else:
            raw = Path(args.input_file).expanduser().read_text(encoding="utf-8")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            print("sensor payload must be a JSON object", file=sys.stderr)
            return 2
        event = sensor_event_from_payload(
            payload,
            provider=args.provider,
            source=args.source,
            resolve_path=args.resolve_path,
            hash_path=args.hash_path,
        )
        out = write_sensor_event(event, Path(args.log_dir).expanduser())
        print(f"sensor log written: {out}")
        print("\njson:")
        print(json.dumps(event.to_dict(), indent=2, ensure_ascii=False))
        return 0

    if args.command == "audit-trigger-logs":
        from skills_auditor.observability import (
            audit_trigger_logs,
            load_trigger_logs,
            summarize_trigger_logs,
        )

        events, parse_findings = load_trigger_logs(
            Path(args.log_dir).expanduser(),
            kind=args.kind,
        )
        findings = parse_findings + audit_trigger_logs(events)
        summary = summarize_trigger_logs(events)
        print(f"events analyzed: {summary.total_events}")
        print(f"findings: {len(findings)}")
        errors = [f for f in findings if f.severity == "error"]
        warnings = [f for f in findings if f.severity == "warning"]
        infos = [f for f in findings if f.severity == "info"]
        print(f"  errors: {len(errors)}, warnings: {len(warnings)}, info: {len(infos)}")
        if summary.labeled_events:
            print(f"  labeled accuracy: {summary.accuracy:.3f}")
            print(
                "  false positives: "
                f"{summary.false_positive_events}, false negatives: {summary.false_negative_events}"
            )
        print(f"  by kind: {summary.by_kind}")
        print(f"  by verdict: {summary.by_verdict}")
        for f in findings:
            prefix = {"error": "ERR", "warning": "WARN", "info": "INFO"}.get(f.severity, "?")
            parts = [f"[{prefix}] {f.check}: {f.detail}"]
            if f.event_id:
                parts.append(f"event={f.event_id}")
            if f.path:
                parts.append(f"path={f.path}")
            print("  " + "  ".join(parts))
        print("\njson:")
        print(json.dumps(
            {
                "summary": asdict(summary),
                "findings": [asdict(f) for f in findings],
            },
            indent=2,
            ensure_ascii=False,
        ))
        return 1 if args.fail_on_error and errors else 0

    if args.command == "audit-sensor-logs":
        from skills_auditor.observability import audit_sensor_events, load_sensor_events

        events, parse_findings = load_sensor_events(
            Path(args.log_dir).expanduser(),
            provider=args.provider,
        )
        findings = parse_findings + audit_sensor_events(events)
        by_provider: Dict[str, int] = {}
        by_type: Dict[str, int] = {}
        skill_accesses = 0
        for event in events:
            provider = str(event.get("provider", "") or "unknown")
            event_type = str(event.get("event_type", "") or "unknown")
            by_provider[provider] = by_provider.get(provider, 0) + 1
            by_type[event_type] = by_type.get(event_type, 0) + 1
            if event_type == "skill_file_access":
                skill_accesses += 1
        print(f"sensor events analyzed: {len(events)}")
        print(f"findings: {len(findings)}")
        errors = [f for f in findings if f.severity == "error"]
        warnings = [f for f in findings if f.severity == "warning"]
        infos = [f for f in findings if f.severity == "info"]
        print(f"  errors: {len(errors)}, warnings: {len(warnings)}, info: {len(infos)}")
        print(f"  skill file accesses: {skill_accesses}")
        print(f"  by provider: {by_provider}")
        print(f"  by event type: {by_type}")
        for f in findings:
            prefix = {"error": "ERR", "warning": "WARN", "info": "INFO"}.get(f.severity, "?")
            parts = [f"[{prefix}] {f.check}: {f.detail}"]
            if f.event_id:
                parts.append(f"event={f.event_id}")
            if f.path:
                parts.append(f"path={f.path}")
            print("  " + "  ".join(parts))
        print("\njson:")
        print(json.dumps(
            {
                "summary": {
                    "total_events": len(events),
                    "skill_file_accesses": skill_accesses,
                    "by_provider": by_provider,
                    "by_event_type": by_type,
                },
                "findings": [asdict(f) for f in findings],
            },
            indent=2,
            ensure_ascii=False,
        ))
        return 1 if args.fail_on_error and errors else 0

    if args.command == "aggregate-sensor-claims":
        from skills_auditor.observability import aggregate_sensor_claims, load_sensor_events

        events, parse_findings = load_sensor_events(
            Path(args.log_dir).expanduser(),
            provider=args.provider,
        )
        claims = aggregate_sensor_claims(events)
        by_confidence: Dict[str, int] = {}
        by_status: Dict[str, int] = {}
        for claim in claims:
            by_confidence[claim.confidence] = by_confidence.get(claim.confidence, 0) + 1
            by_status[claim.status] = by_status.get(claim.status, 0) + 1
        print(f"sensor events analyzed: {len(events)}")
        print(f"parse findings: {len(parse_findings)}")
        print(f"claims: {len(claims)}")
        print(f"  by confidence: {by_confidence}")
        print(f"  by status: {by_status}")
        for finding in parse_findings:
            print(f"  [ERR] {finding.check}: {finding.detail}")
        for claim in claims:
            print(
                "  "
                f"{claim.confidence}\t{claim.claim_type}\t{claim.provider}\t"
                f"{claim.operation}\t{claim.skill_name or '-'}\t{claim.path or '-'}"
            )
        print("\njson:")
        print(json.dumps(
            {
                "summary": {
                    "sensor_events": len(events),
                    "parse_findings": len(parse_findings),
                    "claims": len(claims),
                    "by_confidence": by_confidence,
                    "by_status": by_status,
                },
                "claims": [claim.to_dict() for claim in claims],
                "findings": [asdict(finding) for finding in parse_findings],
            },
            indent=2,
            ensure_ascii=False,
        ))
        return 1 if parse_findings else 0

    if args.command == "log-stats":
        from skills_auditor.observability import collect_storage_stats, estimate_retention_bytes
        from skills_auditor.state_machine import TRACE_DIR

        log_dir = Path(args.log_dir).expanduser()
        trace_dir = Path(args.trace_dir).expanduser() if args.trace_dir else TRACE_DIR
        stats = collect_storage_stats(
            [
                ("trigger_logs", log_dir / "logs"),
                ("sensor_logs", log_dir / "sensors"),
                ("route_traces", trace_dir),
            ]
        )
        total_bytes = sum(s.total_bytes for s in stats)
        total_records = sum(s.record_count for s in stats)
        average_record_bytes = (total_bytes / total_records) if total_records else 0.0
        print("storage scopes:")
        for s in stats:
            print(
                f"  {s.label}: path={s.path} exists={s.exists} files={s.file_count} "
                f"records={s.record_count} bytes={s.total_bytes} "
                f"avg_record_bytes={s.average_record_bytes:.1f}"
            )
        print(f"\ntotal bytes: {total_bytes}")
        print(f"total records: {total_records}")
        print(f"observed average record bytes: {average_record_bytes:.1f}")
        print("\nformula:")
        print(
            "  storage_bytes ~= events_per_day * retention_days * "
            "average_record_bytes * (1 + index_multiplier)"
        )
        print(
            "  compute_seconds ~= events * (parse_seconds + regression_seconds + "
            "optional_llm_judge_seconds)"
        )
        if args.events_per_day:
            estimated = estimate_retention_bytes(
                average_record_bytes,
                args.events_per_day,
                args.retention_days,
                args.index_multiplier,
            )
            print(
                "\nestimate: "
                f"{estimated:.1f} bytes for {args.events_per_day:g} events/day, "
                f"{args.retention_days} days, index_multiplier={args.index_multiplier:g}"
            )
        print("\njson:")
        print(json.dumps(
            {
                "scopes": [asdict(s) for s in stats],
                "total_bytes": total_bytes,
                "total_records": total_records,
                "average_record_bytes": average_record_bytes,
                "storage_formula": (
                    "events_per_day * retention_days * average_record_bytes * "
                    "(1 + index_multiplier)"
                ),
                "compute_formula": (
                    "events * (parse_seconds + regression_seconds + "
                    "optional_llm_judge_seconds)"
                ),
            },
            indent=2,
            ensure_ascii=False,
        ))
        return 0

    if args.command == "ledger-create":
        from skills_auditor.ledger import create_ledger, ledger_path

        ledger_root = Path(args.ledger_dir).expanduser()
        ledger = create_ledger(
            run_id=args.run_id or None,
            source=args.source,
            mode=args.mode,
            ledger_root=ledger_root,
        )
        out = ledger_path(ledger["run"]["id"], ledger_root)
        print(f"ledger written: {out}")
        print("\njson:")
        print(json.dumps(ledger, indent=2, ensure_ascii=False))
        return 0

    if args.command == "ledger-upsert":
        from skills_auditor.ledger import (
            create_ledger,
            load_ledger,
            metadata_from_cli,
            save_ledger,
            upsert_resource,
        )

        ledger_root = Path(args.ledger_dir).expanduser()
        try:
            metadata = metadata_from_cli(args.metadata)
            try:
                ledger = load_ledger(args.run_id, ledger_root)
            except FileNotFoundError:
                if not args.create_if_missing:
                    raise
                ledger = create_ledger(
                    run_id=args.run_id,
                    source=args.source,
                    mode=args.mode,
                    ledger_root=ledger_root,
                )
            upsert_resource(
                ledger,
                resource_id=args.id,
                resource_class=args.resource_class,
                locator=args.locator,
                owner=args.owner,
                status=args.status,
                note=args.note,
                metadata=metadata,
                handoff_target=args.handoff_target,
                blocked_reason=args.blocked_reason,
            )
        except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        out = save_ledger(ledger, ledger_root)
        print(f"ledger updated: {out}")
        print("\njson:")
        print(json.dumps(ledger, indent=2, ensure_ascii=False))
        return 0

    if args.command == "ledger-check":
        from skills_auditor.ledger import audit_ledger, load_ledger, save_ledger, update_checks

        ledger_root = Path(args.ledger_dir).expanduser()
        try:
            ledger = load_ledger(args.run_id, ledger_root)
        except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        findings = audit_ledger(ledger)
        update_checks(ledger, findings)
        save_ledger(ledger, ledger_root)
        errors = [f for f in findings if f.severity == "error"]
        warnings = [f for f in findings if f.severity == "warning"]
        infos = [f for f in findings if f.severity == "info"]
        print(f"ledger: {args.run_id}")
        print(f"findings: {len(findings)}")
        print(f"  errors: {len(errors)}, warnings: {len(warnings)}, info: {len(infos)}")
        for f in findings:
            prefix = {"error": "ERR", "warning": "WARN", "info": "INFO"}.get(f.severity, "?")
            parts = [f"[{prefix}] {f.check}: {f.detail}"]
            if f.resource_id:
                parts.append(f"resource={f.resource_id}")
            print("  " + "  ".join(parts))
        print("\njson:")
        print(json.dumps(
            {
                "checks": ledger.get("checks", {}),
                "findings": [f.to_dict() for f in findings],
            },
            indent=2,
            ensure_ascii=False,
        ))
        if errors or (args.fail_on_warning and warnings):
            return 1
        return 0

    if args.command == "ledger-summary":
        from skills_auditor.ledger import ledger_summary, load_ledger

        ledger_root = Path(args.ledger_dir).expanduser()
        summaries = []
        try:
            if args.run_id:
                summaries.append(ledger_summary(load_ledger(args.run_id, ledger_root)))
            elif ledger_root.exists():
                for path in sorted(ledger_root.glob("*.json")):
                    summaries.append(ledger_summary(load_ledger(path.stem, ledger_root)))
        except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        total_resources = sum(s["resource_count"] for s in summaries)
        print(f"ledgers: {len(summaries)}")
        print(f"resources: {total_resources}")
        for summary in summaries:
            print(
                f"  {summary['run_id']}: resources={summary['resource_count']} "
                f"by_status={summary['by_status']} by_class={summary['by_class']}"
            )
        print("\njson:")
        print(json.dumps(
            {
                "ledger_count": len(summaries),
                "resource_count": total_resources,
                "ledgers": summaries,
            },
            indent=2,
            ensure_ascii=False,
        ))
        return 0

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


if __name__ == "__main__":
    raise SystemExit(main())
