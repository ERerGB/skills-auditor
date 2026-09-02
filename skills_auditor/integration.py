"""High-level, reviewable skill integration contract.

The old commands remain useful primitives.  This module supplies the small adoption
surface they intentionally do not provide:

    IntegrationSpec -> immutable Plan -> Apply -> Receipt -> Verify

Apply never rediscovers sources.  It validates full source-tree hashes and target-entry
snapshots captured by the reviewed plan, then executes only those recorded actions.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from skills_auditor.cli import (
    collect_metadata_findings,
    directory_tree_hash,
    discover_sync_mapping,
    plan_sync,
)
from skills_auditor.environments import BUILTIN_ENVIRONMENTS


SPEC_SCHEMA = "skills-auditor-integration/v1"
PLAN_SCHEMA = "skills-auditor-plan/v1"
RECEIPT_SCHEMA = "skills-auditor-receipt/v1"
VERIFICATION_SCHEMA = "skills-auditor-verification/v1"
ERROR_SCHEMA = "skills-auditor-error/v1"
DEFAULT_CONFIG = "skills-auditor.json"
DEFAULT_LOCAL_ROOT = ".skills-auditor-local"

EXIT_INPUT = 2
EXIT_CONTRACT = 3

_ACTIONABLE = {"create_link", "replace_link", "archive_and_link"}
_SUPPORTED_ACTIONS = _ACTIONABLE | {"noop"}


class IntegrationError(ValueError):
    """Expected integration failure with a stable machine-readable code."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        exit_code: int = EXIT_INPUT,
        details: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code
        self.details = details or []

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": ERROR_SCHEMA,
            "status": "error",
            "error": {
                "code": self.code,
                "message": str(self),
                "details": self.details,
            },
        }


@dataclass(frozen=True)
class IntegrationTarget:
    environment: str
    scope: str = "project"
    root: Optional[Path] = None


@dataclass(frozen=True)
class IntegrationSpec:
    project_root: Path
    sources: Tuple[Path, ...]
    targets: Tuple[IntegrationTarget, ...]
    metadata_platform: str = "codex"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _resolve_path(raw: str, base: Path) -> Path:
    expanded = Path(os.path.expandvars(raw)).expanduser()
    if not expanded.is_absolute():
        expanded = base / expanded
    return expanded.resolve(strict=False)


def _dedupe_paths(paths: Iterable[Path]) -> Tuple[Path, ...]:
    out: List[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path.resolve(strict=False))
        if key not in seen:
            seen.add(key)
            out.append(path)
    return tuple(out)


def _parse_target(value: object, base: Path) -> IntegrationTarget:
    if isinstance(value, str):
        environment, separator, scope = value.partition("@")
        scope = scope if separator else "project"
        if not environment.strip():
            raise IntegrationError("invalid_target", "target environment must not be empty")
        if scope not in {"project", "global"}:
            raise IntegrationError(
                "invalid_target_scope",
                f"target {value!r} must use @project or @global",
            )
        return IntegrationTarget(environment.strip(), scope)

    if not isinstance(value, dict):
        raise IntegrationError(
            "invalid_target",
            "each target must be an environment string or an object",
        )
    unknown = set(value) - {"environment", "scope", "root"}
    if unknown:
        raise IntegrationError(
            "invalid_target",
            f"target contains unknown fields: {sorted(unknown)}",
        )
    environment = value.get("environment")
    scope = value.get("scope", "project")
    root = value.get("root")
    if not isinstance(environment, str) or not environment.strip():
        raise IntegrationError("invalid_target", "target.environment must be a non-empty string")
    if scope not in {"project", "global"}:
        raise IntegrationError(
            "invalid_target_scope",
            f"target {environment!r} must use project or global scope",
        )
    if root is not None and not isinstance(root, str):
        raise IntegrationError("invalid_target_root", "target.root must be a string")
    return IntegrationTarget(
        environment.strip(),
        str(scope),
        _resolve_path(root, base) if isinstance(root, str) else None,
    )


def _parse_target_root(value: str, base: Path) -> IntegrationTarget:
    environment, separator, root = value.partition("=")
    if not separator or not environment.strip() or not root.strip():
        raise IntegrationError(
            "invalid_target_root",
            "--target-root must be NAME=PATH",
        )
    return IntegrationTarget(environment.strip(), "project", _resolve_path(root, base))


def load_integration_spec(
    *,
    config_path: Optional[Path] = None,
    cli_sources: Sequence[str] = (),
    cli_targets: Sequence[str] = (),
    cli_target_roots: Sequence[str] = (),
    metadata_platform: str = "",
    cwd: Optional[Path] = None,
) -> IntegrationSpec:
    """Load one integration spec; CLI lists replace config lists when supplied."""

    working_dir = (cwd or Path.cwd()).resolve(strict=False)
    selected_config = config_path
    if selected_config is None:
        candidate = working_dir / DEFAULT_CONFIG
        selected_config = candidate if candidate.exists() else None

    data: Dict[str, Any] = {}
    config_base = working_dir
    if selected_config is not None:
        selected_config = selected_config.expanduser().resolve(strict=False)
        if not selected_config.is_file():
            raise IntegrationError(
                "config_not_found",
                f"integration config not found: {selected_config}",
            )
        try:
            loaded = json.loads(selected_config.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise IntegrationError("invalid_config", f"could not read config: {exc}") from exc
        if not isinstance(loaded, dict):
            raise IntegrationError("invalid_config", "integration config must be a JSON object")
        data = loaded
        unknown = set(data) - {
            "schema_version",
            "project_root",
            "sources",
            "targets",
            "metadata_platform",
        }
        if unknown:
            raise IntegrationError(
                "invalid_config",
                f"integration config contains unknown fields: {sorted(unknown)}",
            )
        if data.get("schema_version") != SPEC_SCHEMA:
            raise IntegrationError(
                "invalid_config_schema",
                f"expected schema_version {SPEC_SCHEMA!r}",
            )
        config_base = selected_config.parent

    project_raw = data.get("project_root", ".")
    if not isinstance(project_raw, str):
        raise IntegrationError("invalid_project_root", "project_root must be a string")
    project_root = _resolve_path(project_raw, config_base)
    if not project_root.is_dir():
        raise IntegrationError(
            "invalid_project_root",
            f"project_root is not a directory: {project_root}",
        )

    source_values: object = list(cli_sources) if cli_sources else data.get("sources", [])
    if not isinstance(source_values, list) or not source_values:
        raise IntegrationError(
            "missing_sources",
            "provide --source or add a non-empty sources list to skills-auditor.json",
        )
    if not all(isinstance(item, str) and item.strip() for item in source_values):
        raise IntegrationError("invalid_sources", "sources must be non-empty path strings")
    source_base = working_dir if cli_sources else project_root
    sources = _dedupe_paths(_resolve_path(str(item), source_base) for item in source_values)

    target_values: object = (
        list(cli_targets)
        if cli_targets or cli_target_roots
        else data.get("targets", [])
    )
    if not isinstance(target_values, list):
        raise IntegrationError("invalid_targets", "targets must be a list")
    target_base = working_dir if cli_targets or cli_target_roots else project_root
    targets = [_parse_target(value, target_base) for value in target_values]
    targets.extend(_parse_target_root(value, working_dir) for value in cli_target_roots)
    if not targets:
        raise IntegrationError(
            "missing_targets",
            "provide --target/--target-root or add targets to skills-auditor.json",
        )

    selected_metadata = metadata_platform or data.get("metadata_platform", "codex")
    if not isinstance(selected_metadata, str) or not selected_metadata.strip():
        raise IntegrationError("invalid_metadata_platform", "metadata_platform must be a string")

    return IntegrationSpec(
        project_root=project_root,
        sources=sources,
        targets=tuple(targets),
        metadata_platform=selected_metadata.strip(),
    )


def _target_root(target: IntegrationTarget, project_root: Path) -> Path:
    if target.root is not None:
        return target.root.resolve(strict=False)
    try:
        environment = BUILTIN_ENVIRONMENTS.get(target.environment)
    except ValueError as exc:
        raise IntegrationError(
            "unknown_environment",
            f"{exc}; use --target-root {target.environment}=PATH for a custom host",
        ) from exc
    if target.scope == "global":
        return environment.primary_global_root(Path.home()).resolve(strict=False)
    return environment.primary_project_root(project_root).resolve(strict=False)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def entry_snapshot(path: Path) -> Dict[str, Any]:
    """Capture only state that matters to replacing this exact install entry."""

    if path.is_symlink():
        return {
            "kind": "symlink",
            "target": os.readlink(path),
            "resolved": str(path.resolve(strict=False)),
        }
    if not path.exists():
        return {"kind": "missing"}
    if path.is_file():
        return {"kind": "file", "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    if path.is_dir():
        return {"kind": "directory", "tree_sha256": directory_tree_hash(path)}
    return {"kind": "other", "mode": path.lstat().st_mode}


def _snapshot_for_plan(path: Path) -> Dict[str, Any]:
    try:
        return entry_snapshot(path)
    except OSError as exc:
        raise IntegrationError(
            "target_unreadable",
            f"could not snapshot target entry {path}: {exc}",
            exit_code=EXIT_CONTRACT,
        ) from exc


def _source_tree_hash(skill_root: Path) -> str:
    try:
        return directory_tree_hash(skill_root, reject_external_symlinks=True)
    except ValueError as exc:
        raise IntegrationError(
            "source_symlink_escape",
            str(exc),
            exit_code=EXIT_CONTRACT,
        ) from exc
    except OSError as exc:
        raise IntegrationError(
            "source_unreadable",
            f"could not hash source tree {skill_root}: {exc}",
            exit_code=EXIT_CONTRACT,
        ) from exc


def _validate_spec(spec: IntegrationSpec) -> List[Tuple[IntegrationTarget, Path]]:
    if not spec.project_root.is_dir():
        raise IntegrationError(
            "invalid_project_root",
            f"project_root is not a directory: {spec.project_root}",
        )
    if not spec.sources:
        raise IntegrationError("missing_sources", "integration spec has no source roots")
    if not spec.targets:
        raise IntegrationError("missing_targets", "integration spec has no targets")
    if not _nonempty_string(spec.metadata_platform):
        raise IntegrationError("invalid_metadata_platform", "metadata platform must not be empty")
    for source in spec.sources:
        if not source.is_dir():
            raise IntegrationError("source_not_found", f"source directory not found: {source}")

    findings_by_path: Dict[str, Dict[str, Any]] = {}
    for source in spec.sources:
        for finding in collect_metadata_findings(source, platform=spec.metadata_platform):
            findings_by_path[finding.skill_md_path] = asdict(finding)
    if findings_by_path:
        raise IntegrationError(
            "invalid_metadata",
            f"{len(findings_by_path)} source skill(s) failed metadata validation",
            exit_code=EXIT_CONTRACT,
            details=list(findings_by_path.values()),
        )

    resolved_targets: List[Tuple[IntegrationTarget, Path]] = []
    for target in spec.targets:
        if not _nonempty_string(target.environment) or target.scope not in {"project", "global"}:
            raise IntegrationError("invalid_target", "integration target is invalid")
        root = _target_root(target, spec.project_root)
        for _, existing_root in resolved_targets:
            if _is_within(root, existing_root) or _is_within(existing_root, root):
                raise IntegrationError(
                    "overlapping_targets",
                    f"target roots must not contain each other: {existing_root} and {root}",
                )
        if root.exists() and not root.is_dir():
            raise IntegrationError(
                "invalid_target_root",
                f"target root exists but is not a directory: {root}",
            )
        for source in spec.sources:
            if _is_within(root, source):
                raise IntegrationError(
                    "target_inside_source",
                    f"target root {root} is inside source root {source}; choose a canonical source tree",
                )
            if _is_within(source, root):
                raise IntegrationError(
                    "source_inside_target",
                    f"source root {source} is inside target root {root}; "
                    "keep canonical and installed trees disjoint",
                )
        resolved_targets.append((target, root))
    return resolved_targets


def _spec_dict(
    spec: IntegrationSpec,
    resolved_targets: Sequence[Tuple[IntegrationTarget, Path]],
) -> Dict[str, Any]:
    return {
        "schema_version": SPEC_SCHEMA,
        "project_root": str(spec.project_root),
        "sources": [str(path) for path in spec.sources],
        "targets": [
            {
                "environment": target.environment,
                "scope": target.scope,
                "root": str(root),
            }
            for target, root in resolved_targets
        ],
        "metadata_platform": spec.metadata_platform,
    }


def _plan_material(plan: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: value
        for key, value in plan.items()
        if key not in {"plan_id", "plan_path"}
    }


def _plan_id(plan: Dict[str, Any]) -> str:
    return "plan-" + _digest(_plan_material(plan))[:20]


def _archive_destination(
    root: Path,
    name: str,
    created_at: str,
    reserved_paths: set[str],
) -> Path:
    stamp = created_at.replace("-", "").replace(":", "")
    candidate = root / f"{name}.archived-{stamp}"
    suffix = 1
    while (
        candidate.exists()
        or candidate.is_symlink()
        or str(candidate) in reserved_paths
    ):
        candidate = root / f"{name}.archived-{stamp}.{suffix}"
        suffix += 1
    reserved_paths.add(str(candidate))
    return candidate


def _normalize_spec(spec: IntegrationSpec) -> IntegrationSpec:
    return IntegrationSpec(
        project_root=spec.project_root.expanduser().resolve(strict=False),
        sources=tuple(
            source.expanduser().resolve(strict=False) for source in spec.sources
        ),
        targets=tuple(
            IntegrationTarget(
                target.environment,
                target.scope,
                target.root.expanduser().resolve(strict=False)
                if target.root is not None
                else None,
            )
            for target in spec.targets
        ),
        metadata_platform=spec.metadata_platform,
    )


def build_integration_plan(spec: IntegrationSpec) -> Dict[str, Any]:
    spec = _normalize_spec(spec)
    resolved_targets = _validate_spec(spec)
    try:
        mapping = discover_sync_mapping(list(spec.sources))
    except ValueError as exc:
        raise IntegrationError(
            "source_conflict",
            str(exc),
            exit_code=EXIT_CONTRACT,
        ) from exc
    except OSError as exc:
        raise IntegrationError(
            "source_unreadable",
            f"could not inspect source trees: {exc}",
            exit_code=EXIT_CONTRACT,
        ) from exc
    if not mapping:
        raise IntegrationError("no_skills", "no SKILL.md definitions were discovered")

    source_skills = [
        {
            "name": name,
            "skill_root": target,
            "tree_sha256": _source_tree_hash(Path(target)),
        }
        for name, target in sorted(mapping.items())
    ]
    hash_by_name = {item["name"]: item["tree_sha256"] for item in source_skills}
    created_at = _utc_now()

    target_plans: List[Dict[str, Any]] = []
    for target, root in resolved_targets:
        actions = plan_sync(root, mapping)
        reserved_paths = {
            str(root / action.name) for action in actions
        }
        records: List[Dict[str, Any]] = []
        for action in actions:
            if action.action not in _SUPPORTED_ACTIONS:
                raise IntegrationError(
                    "unplannable_action",
                    f"{target.environment}/{action.name}: {action.action}: {action.reason}",
                    exit_code=EXIT_CONTRACT,
                )
            record = asdict(action)
            record["expected_tree_sha256"] = hash_by_name[action.name]
            record["entry_before"] = _snapshot_for_plan(root / action.name)
            archive_path: Optional[Path] = None
            if action.action == "archive_and_link":
                archive_path = _archive_destination(
                    root,
                    action.name,
                    created_at,
                    reserved_paths,
                )
            record["archive_path"] = str(archive_path) if archive_path else None
            record["archive_before"] = (
                _snapshot_for_plan(archive_path) if archive_path else None
            )
            records.append(record)
        target_plans.append(
            {
                "environment": target.environment,
                "scope": target.scope,
                "root": str(root),
                "actions": records,
            }
        )

    plan: Dict[str, Any] = {
        "schema_version": PLAN_SCHEMA,
        "created_at": created_at,
        "spec": _spec_dict(spec, resolved_targets),
        "source_skills": source_skills,
        "targets": target_plans,
    }
    plan["summary"] = {
        "skills": len(source_skills),
        "targets": len(target_plans),
        "actions": sum(len(target_plan["actions"]) for target_plan in target_plans),
        "changes": sum(
            1
            for target_plan in target_plans
            for action in target_plan["actions"]
            if action["action"] in _ACTIONABLE
        ),
    }
    plan["plan_id"] = _plan_id(plan)
    return plan


def _atomic_write_json(path: Path, value: Dict[str, Any]) -> Path:
    path = path.expanduser().resolve(strict=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    temporary = Path(handle.name)
    try:
        with handle:
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    return path


def save_plan(plan: Dict[str, Any], output: Optional[Path] = None) -> Path:
    validate_plan(plan)
    if output is None:
        project_root = Path(plan["spec"]["project_root"])
        output = project_root / DEFAULT_LOCAL_ROOT / "plans" / f"{plan['plan_id']}.json"
    try:
        return _atomic_write_json(output, plan)
    except OSError as exc:
        raise IntegrationError(
            "plan_write_failed",
            f"could not write integration plan: {exc}",
            exit_code=EXIT_CONTRACT,
        ) from exc


def load_json_object(path: Path, label: str) -> Dict[str, Any]:
    try:
        value = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IntegrationError(f"invalid_{label}", f"could not read {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise IntegrationError(f"invalid_{label}", f"{label} must be a JSON object")
    return value


def _nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _safe_install_name(value: object) -> bool:
    return (
        _nonempty_string(value)
        and value not in {".", ".."}
        and Path(value).name == value
    )


def _absolute_path(value: object) -> bool:
    return _nonempty_string(value) and Path(value).is_absolute()


def _canonical_absolute_path(value: object) -> bool:
    return _absolute_path(value) and str(Path(value).resolve(strict=False)) == value


def _sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _exact_keys(
    value: Dict[str, Any],
    required: set[str],
    optional: Optional[set[str]] = None,
) -> bool:
    keys = set(value)
    return required <= keys <= required | (optional or set())


def _valid_snapshot(value: object) -> bool:
    if not isinstance(value, dict) or not _nonempty_string(value.get("kind")):
        return False
    kind = value["kind"]
    if kind == "missing":
        return _exact_keys(value, {"kind"})
    if kind == "symlink":
        return (
            _exact_keys(value, {"kind", "target", "resolved"})
            and isinstance(value["target"], str)
            and isinstance(value["resolved"], str)
        )
    if kind == "file":
        return _exact_keys(value, {"kind", "sha256"}) and _sha256(value["sha256"])
    if kind == "directory":
        return _exact_keys(value, {"kind", "tree_sha256"}) and _sha256(
            value["tree_sha256"]
        )
    if kind == "other":
        return _exact_keys(value, {"kind", "mode"}) and isinstance(value["mode"], int)
    return False


def validate_plan(plan: Dict[str, Any]) -> None:
    if plan.get("schema_version") != PLAN_SCHEMA:
        raise IntegrationError(
            "invalid_plan_schema",
            f"expected schema_version {PLAN_SCHEMA!r}",
        )
    if plan.get("plan_id") != _plan_id(plan):
        raise IntegrationError(
            "invalid_plan_id",
            "plan content does not match plan_id",
            exit_code=EXIT_CONTRACT,
        )
    if not _exact_keys(
        plan,
        {"schema_version", "plan_id", "created_at", "spec", "source_skills", "targets", "summary"},
        {"plan_path"},
    ):
        raise IntegrationError("invalid_plan", "plan contains missing or unknown fields")
    spec = plan.get("spec")
    sources = plan.get("source_skills")
    targets = plan.get("targets")
    if (
        not isinstance(spec, dict)
        or not _exact_keys(
            spec,
            {"schema_version", "project_root", "sources", "targets", "metadata_platform"},
        )
        or spec.get("schema_version") != SPEC_SCHEMA
    ):
        raise IntegrationError("invalid_plan", "plan spec is missing or invalid")
    if not _canonical_absolute_path(spec.get("project_root")):
        raise IntegrationError("invalid_plan", "plan project_root must be canonical and absolute")
    if (
        not isinstance(spec.get("sources"), list)
        or not spec["sources"]
        or not all(_canonical_absolute_path(path) for path in spec["sources"])
        or not _nonempty_string(spec.get("metadata_platform"))
    ):
        raise IntegrationError("invalid_plan", "plan spec sources or metadata are invalid")
    if not isinstance(sources, list) or not sources:
        raise IntegrationError("invalid_plan", "plan source_skills must be a non-empty list")
    if not isinstance(targets, list) or not targets:
        raise IntegrationError("invalid_plan", "plan targets must be a non-empty list")

    source_by_name: Dict[str, Dict[str, Any]] = {}
    for source in sources:
        if (
            not isinstance(source, dict)
            or not _exact_keys(source, {"name", "skill_root", "tree_sha256"})
            or not _safe_install_name(source.get("name"))
            or not _canonical_absolute_path(source.get("skill_root"))
            or not _sha256(source.get("tree_sha256"))
        ):
            raise IntegrationError("invalid_plan", "plan contains an invalid source record")
        name = source["name"]
        if name in source_by_name:
            raise IntegrationError("invalid_plan", f"plan repeats source name {name!r}")
        source_by_name[name] = source

    spec_targets = spec.get("targets")
    if not isinstance(spec_targets, list) or len(spec_targets) != len(targets):
        raise IntegrationError("invalid_plan", "plan targets do not match its spec")

    total_actions = 0
    total_changes = 0
    resolved_target_roots: List[Path] = []
    for index, target in enumerate(targets):
        if (
            not isinstance(target, dict)
            or not _exact_keys(target, {"environment", "scope", "root", "actions"})
            or not _nonempty_string(target.get("environment"))
            or target.get("scope") not in {"project", "global"}
            or not _canonical_absolute_path(target.get("root"))
            or not isinstance(target.get("actions"), list)
        ):
            raise IntegrationError("invalid_plan", "plan contains an invalid target record")
        expected_target_spec = {
            "environment": target["environment"],
            "scope": target["scope"],
            "root": target["root"],
        }
        if spec_targets[index] != expected_target_spec:
            raise IntegrationError("invalid_plan", "plan target does not match its spec")

        root = Path(target["root"]).resolve(strict=False)
        for source_root_text in spec["sources"]:
            source_root = Path(source_root_text)
            if _is_within(root, source_root) or _is_within(source_root, root):
                raise IntegrationError(
                    "invalid_plan",
                    "plan source and target roots must be disjoint",
                )
        for existing_root in resolved_target_roots:
            if _is_within(root, existing_root) or _is_within(existing_root, root):
                raise IntegrationError(
                    "invalid_plan",
                    "plan target roots must not contain each other",
                )
        resolved_target_roots.append(root)

        action_names: set[str] = set()
        reserved_paths: set[str] = set()
        for action in target["actions"]:
            if (
                not isinstance(action, dict)
                or not _exact_keys(
                    action,
                    {
                        "name",
                        "expected_target",
                        "action",
                        "reason",
                        "expected_tree_sha256",
                        "entry_before",
                        "archive_path",
                        "archive_before",
                    },
                )
                or not _safe_install_name(action.get("name"))
                or action.get("action") not in _SUPPORTED_ACTIONS
                or not _canonical_absolute_path(action.get("expected_target"))
                or not _sha256(action.get("expected_tree_sha256"))
                or not _nonempty_string(action.get("reason"))
                or not _valid_snapshot(action.get("entry_before"))
            ):
                raise IntegrationError(
                    "invalid_plan_action",
                    "plan contains an invalid action record",
                )
            name = action["name"]
            source = source_by_name.get(name)
            if name in action_names or source is None:
                raise IntegrationError(
                    "invalid_plan_action",
                    f"plan target repeats or does not define source {name!r}",
                )
            action_names.add(name)
            entry_path = str(root / name)
            if entry_path in reserved_paths:
                raise IntegrationError(
                    "invalid_plan_action",
                    "plan contains colliding target or archive paths",
                )
            reserved_paths.add(entry_path)
            if (
                action["expected_target"] != source["skill_root"]
                or action["expected_tree_sha256"] != source["tree_sha256"]
            ):
                raise IntegrationError(
                    "invalid_plan_action",
                    f"plan action for {name!r} does not match its source record",
                )

            before_kind = action["entry_before"].get("kind")
            expected_kinds = {
                "create_link": {"missing"},
                "replace_link": {"symlink"},
                "archive_and_link": {"file", "directory", "other"},
                "noop": {"symlink"},
            }
            if before_kind not in expected_kinds[action["action"]]:
                raise IntegrationError(
                    "invalid_plan_action",
                    f"plan action {action['action']!r} contradicts its entry snapshot",
                )

            archive_path = action.get("archive_path")
            archive_before = action.get("archive_before")
            if action["action"] == "archive_and_link":
                if (
                    not _canonical_absolute_path(archive_path)
                    or archive_before != {"kind": "missing"}
                ):
                    raise IntegrationError(
                        "invalid_plan_action",
                        "archive action must reserve one missing archive path",
                    )
                archive = Path(archive_path)
                if (
                    archive.parent.resolve(strict=False) != root
                    or not archive.name.startswith(f"{name}.archived-")
                ):
                    raise IntegrationError(
                        "invalid_plan_action",
                        "archive path must stay beside the target entry",
                    )
                if str(archive) in reserved_paths:
                    raise IntegrationError(
                        "invalid_plan_action",
                        "plan contains colliding target or archive paths",
                    )
                reserved_paths.add(str(archive))
            elif archive_path is not None or archive_before is not None:
                raise IntegrationError(
                    "invalid_plan_action",
                    "non-archive action must not contain an archive destination",
                )

            total_actions += 1
            if action["action"] in _ACTIONABLE:
                total_changes += 1

        if action_names != set(source_by_name):
            raise IntegrationError(
                "invalid_plan_action",
                "each target must contain exactly one action per source skill",
            )

    expected_summary = {
        "skills": len(sources),
        "targets": len(targets),
        "actions": total_actions,
        "changes": total_changes,
    }
    if plan.get("summary") != expected_summary:
        raise IntegrationError("invalid_plan", "plan summary does not match its actions")


def check_plan_preconditions(plan: Dict[str, Any]) -> List[Dict[str, Any]]:
    validate_plan(plan)
    issues: List[Dict[str, Any]] = []
    for source in plan["source_skills"]:
        name = source["name"]
        skill_root = Path(source["skill_root"])
        expected = source["tree_sha256"]
        try:
            actual = (
                directory_tree_hash(skill_root, reject_external_symlinks=True)
                if skill_root.is_dir()
                else None
            )
        except (OSError, ValueError):
            actual = None
        if actual != expected:
            issues.append(
                {
                    "code": "source_changed",
                    "name": name,
                    "path": str(skill_root),
                    "expected": expected,
                    "actual": actual,
                }
            )

    for target in plan["targets"]:
        root = Path(target["root"])
        for action in target["actions"]:
            name = action["name"]
            try:
                actual_snapshot = entry_snapshot(root / name)
            except OSError as exc:
                actual_snapshot = {"kind": "unreadable", "error": str(exc)}
            if actual_snapshot != action["entry_before"]:
                issues.append(
                    {
                        "code": "target_changed",
                        "target": str(root),
                        "name": name,
                        "expected": action["entry_before"],
                        "actual": actual_snapshot,
                    }
                )
            archive_path = action["archive_path"]
            if archive_path is not None:
                try:
                    actual_archive = entry_snapshot(Path(archive_path))
                except OSError as exc:
                    actual_archive = {"kind": "unreadable", "error": str(exc)}
                if actual_archive != action["archive_before"]:
                    issues.append(
                        {
                            "code": "archive_changed",
                            "target": str(root),
                            "name": name,
                            "path": archive_path,
                            "expected": action["archive_before"],
                            "actual": actual_archive,
                        }
                    )
    return issues


def _receipt_material(receipt: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: value
        for key, value in receipt.items()
        if key not in {"receipt_id", "receipt_path"}
    }


def _receipt_id(receipt: Dict[str, Any]) -> str:
    return "receipt-" + _digest(_receipt_material(receipt))[:20]


def _default_receipt_path(plan: Dict[str, Any], receipt_id: str) -> Path:
    project_root = Path(plan["spec"]["project_root"])
    return project_root / DEFAULT_LOCAL_ROOT / "receipts" / f"{receipt_id}.json"


def _build_receipt(
    plan: Dict[str, Any],
    status: str,
    results: List[Dict[str, Any]],
    error: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    receipt: Dict[str, Any] = {
        "schema_version": RECEIPT_SCHEMA,
        "plan_id": plan["plan_id"],
        "applied_at": _utc_now(),
        "status": status,
        "results": results,
        "error": error,
    }
    receipt["receipt_id"] = _receipt_id(receipt)
    return receipt


def _apply_exact_action(root: Path, action: Dict[str, Any]) -> None:
    entry = root / action["name"]
    target = Path(action["expected_target"])
    operation = action["action"]
    if operation == "noop":
        return

    root.mkdir(parents=True, exist_ok=True)
    if operation == "create_link":
        os.symlink(str(target), str(entry))
        return

    if operation == "replace_link":
        previous_target = os.readlink(entry)
        entry.unlink()
        try:
            os.symlink(str(target), str(entry))
        except Exception as apply_error:
            try:
                if not entry.exists() and not entry.is_symlink():
                    os.symlink(previous_target, str(entry))
            except Exception as rollback_error:
                raise RuntimeError(
                    f"replace failed ({apply_error}); rollback failed ({rollback_error})"
                ) from apply_error
            raise
        return

    if operation == "archive_and_link":
        archive = Path(action["archive_path"])
        entry.rename(archive)
        try:
            os.symlink(str(target), str(entry))
        except Exception as apply_error:
            try:
                if not entry.exists() and not entry.is_symlink():
                    archive.rename(entry)
            except Exception as rollback_error:
                raise RuntimeError(
                    f"archive/link failed ({apply_error}); rollback failed ({rollback_error})"
                ) from apply_error
            raise
        return

    raise IntegrationError(
        "invalid_plan_action",
        f"unsupported integration action: {operation}",
    )


def apply_integration_plan(
    plan: Dict[str, Any],
    receipt_output: Optional[Path] = None,
) -> Tuple[Dict[str, Any], Path]:
    issues = check_plan_preconditions(plan)
    if issues:
        raise IntegrationError(
            "stale_plan",
            "plan preconditions changed; generate and review a new plan",
            exit_code=EXIT_CONTRACT,
            details=issues,
        )

    results: List[Dict[str, Any]] = []
    try:
        for target in plan["targets"]:
            root = Path(target["root"])
            for action_data in target["actions"]:
                expected_hash = action_data["expected_tree_sha256"]
                source_root = Path(action_data["expected_target"])
                current_hash = (
                    directory_tree_hash(source_root, reject_external_symlinks=True)
                    if source_root.is_dir()
                    else None
                )
                if current_hash != expected_hash:
                    raise IntegrationError(
                        "stale_plan",
                        "source changed while applying plan",
                        exit_code=EXIT_CONTRACT,
                        details=[
                            {
                                "code": "source_changed",
                                "name": action_data["name"],
                                "path": str(source_root),
                                "expected": expected_hash,
                                "actual": current_hash,
                            }
                        ],
                    )
                current = entry_snapshot(root / action_data["name"])
                if current != action_data["entry_before"]:
                    raise IntegrationError(
                        "stale_plan",
                        "target changed while applying plan",
                        exit_code=EXIT_CONTRACT,
                        details=[
                            {
                                "code": "target_changed",
                                "target": str(root),
                                "name": action_data["name"],
                                "expected": action_data["entry_before"],
                                "actual": current,
                            }
                        ],
                    )
                archive_path = action_data.get("archive_path")
                if archive_path is not None:
                    current_archive = entry_snapshot(Path(archive_path))
                    if current_archive != action_data["archive_before"]:
                        raise IntegrationError(
                            "stale_plan",
                            "archive destination changed while applying plan",
                            exit_code=EXIT_CONTRACT,
                            details=[
                                {
                                    "code": "archive_changed",
                                    "name": action_data["name"],
                                    "path": archive_path,
                                    "expected": action_data["archive_before"],
                                    "actual": current_archive,
                                }
                            ],
                        )
                _apply_exact_action(root, action_data)
                entry = root / action_data["name"]
                linked = entry.is_symlink() and entry.resolve(strict=False) == Path(
                    action_data["expected_target"]
                ).resolve(strict=False)
                if not linked:
                    raise IntegrationError(
                        "apply_verification_failed",
                        f"{entry} does not resolve to {action_data['expected_target']}",
                        exit_code=EXIT_CONTRACT,
                    )
                after_hash = (
                    directory_tree_hash(source_root, reject_external_symlinks=True)
                    if source_root.is_dir()
                    else None
                )
                if after_hash != expected_hash:
                    raise IntegrationError(
                        "apply_verification_failed",
                        f"source tree changed while applying {action_data['name']}",
                        exit_code=EXIT_CONTRACT,
                    )
                results.append(
                    {
                        "environment": target["environment"],
                        "scope": target["scope"],
                        "root": str(root),
                        "name": action_data["name"],
                        "action": action_data["action"],
                        "expected_target": action_data["expected_target"],
                        "expected_tree_sha256": expected_hash,
                        "archive_path": archive_path,
                        "entry_after": entry_snapshot(entry),
                        "verified": True,
                    }
                )
    except Exception as exc:
        error = (
            exc.to_dict()["error"]
            if isinstance(exc, IntegrationError)
            else {"code": "apply_failed", "message": str(exc), "details": []}
        )
        failed = _build_receipt(plan, "failed", results, error)
        failed_path = receipt_output or _default_receipt_path(plan, failed["receipt_id"])
        try:
            failed_path = _atomic_write_json(failed_path, failed)
        except OSError as receipt_error:
            raise IntegrationError(
                "apply_failed_without_receipt",
                f"apply failed and receipt could not be written: {receipt_error}",
                exit_code=EXIT_CONTRACT,
                details=[{"apply_error": str(exc)}],
            ) from exc
        if isinstance(exc, IntegrationError):
            exc.details.append({"receipt_path": str(failed_path)})
            raise
        raise IntegrationError(
            "apply_failed",
            f"apply failed; receipt written to {failed_path}: {exc}",
            exit_code=EXIT_CONTRACT,
            details=[{"receipt_path": str(failed_path)}],
        ) from exc

    receipt = _build_receipt(plan, "completed", results, None)
    receipt_path = receipt_output or _default_receipt_path(plan, receipt["receipt_id"])
    try:
        return receipt, _atomic_write_json(receipt_path, receipt)
    except OSError as exc:
        raise IntegrationError(
            "receipt_write_failed",
            f"integration completed but receipt could not be written: {exc}",
            exit_code=EXIT_CONTRACT,
        ) from exc


def validate_receipt(receipt: Dict[str, Any]) -> None:
    if receipt.get("schema_version") != RECEIPT_SCHEMA:
        raise IntegrationError(
            "invalid_receipt_schema",
            f"expected schema_version {RECEIPT_SCHEMA!r}",
        )
    if receipt.get("receipt_id") != _receipt_id(receipt):
        raise IntegrationError(
            "invalid_receipt_id",
            "receipt content does not match receipt_id",
            exit_code=EXIT_CONTRACT,
        )
    if not _exact_keys(
        receipt,
        {
            "schema_version",
            "receipt_id",
            "plan_id",
            "applied_at",
            "status",
            "results",
            "error",
        },
        {"receipt_path"},
    ):
        raise IntegrationError("invalid_receipt", "receipt contains missing or unknown fields")
    status = receipt.get("status")
    results = receipt.get("results")
    error = receipt.get("error")
    if not _nonempty_string(receipt.get("plan_id")):
        raise IntegrationError("invalid_receipt", "receipt plan_id must be a string")
    if status not in {"completed", "failed"} or not isinstance(results, list):
        raise IntegrationError("invalid_receipt", "receipt status or results are invalid")
    if (status == "completed" and error is not None) or (
        status == "failed" and not isinstance(error, dict)
    ):
        raise IntegrationError("invalid_receipt", "receipt status and error disagree")
    for result in results:
        if (
            not isinstance(result, dict)
            or not _exact_keys(
                result,
                {
                    "environment",
                    "scope",
                    "root",
                    "name",
                    "action",
                    "expected_target",
                    "expected_tree_sha256",
                    "archive_path",
                    "entry_after",
                    "verified",
                },
            )
            or not _nonempty_string(result.get("environment"))
            or result.get("scope") not in {"project", "global"}
            or not _absolute_path(result.get("root"))
            or not _safe_install_name(result.get("name"))
            or result.get("action") not in _SUPPORTED_ACTIONS
            or not _absolute_path(result.get("expected_target"))
            or not _sha256(result.get("expected_tree_sha256"))
            or not _valid_snapshot(result.get("entry_after"))
            or result["entry_after"].get("kind") != "symlink"
            or result.get("verified") is not True
        ):
            raise IntegrationError("invalid_receipt", "receipt contains an invalid result")
        archive_path = result.get("archive_path")
        if archive_path is not None and not _absolute_path(archive_path):
            raise IntegrationError("invalid_receipt", "receipt archive_path must be absolute")


def verify_receipt(receipt: Dict[str, Any]) -> Dict[str, Any]:
    validate_receipt(receipt)
    checks: List[Dict[str, Any]] = []
    if receipt.get("status") != "completed":
        checks.append(
            {
                "code": "receipt_not_completed",
                "ok": False,
                "detail": f"receipt status is {receipt.get('status')!r}",
            }
        )

    for result in receipt["results"]:
        root = Path(str(result.get("root", "")))
        name = str(result.get("name", ""))
        entry = root / name
        expected_target = Path(str(result.get("expected_target", "")))
        try:
            linked = entry.is_symlink() and entry.resolve(
                strict=False
            ) == expected_target.resolve(strict=False)
            actual_target = str(entry.resolve(strict=False)) if entry.is_symlink() else None
        except OSError:
            linked = False
            actual_target = None
        checks.append(
            {
                "code": "target_link",
                "ok": linked,
                "target": str(root),
                "name": name,
                "expected": str(expected_target),
                "actual": actual_target,
            }
        )
        try:
            actual_hash = (
                directory_tree_hash(expected_target, reject_external_symlinks=True)
                if expected_target.is_dir()
                else None
            )
        except (OSError, ValueError):
            actual_hash = None
        expected_hash = result.get("expected_tree_sha256")
        checks.append(
            {
                "code": "source_tree",
                "ok": actual_hash == expected_hash,
                "name": name,
                "path": str(expected_target),
                "expected": expected_hash,
                "actual": actual_hash,
            }
        )

    failed_reason_codes = list(
        dict.fromkeys(str(check["code"]) for check in checks if not check.get("ok"))
    )
    passed = not failed_reason_codes
    return {
        "schema_version": VERIFICATION_SCHEMA,
        "checked_at": _utc_now(),
        "receipt_id": receipt.get("receipt_id"),
        "plan_id": receipt.get("plan_id"),
        "status": "passed" if passed else "failed",
        "checks": checks,
        "approval": {
            "state": "valid" if passed else "invalidated",
            "requires_reapproval": not passed,
            "reason_codes": failed_reason_codes,
        },
        "summary": {
            "checks": len(checks),
            "passed": sum(1 for check in checks if check.get("ok")),
            "failed": sum(1 for check in checks if not check.get("ok")),
        },
    }
