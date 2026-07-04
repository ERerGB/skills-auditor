"""Structured execution ledgers for skill and delegated agent runs."""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


SCHEMA_VERSION = "skills-auditor-ledger/v1"
DEFAULT_LEDGER_ROOT = Path(".skills-auditor-local") / "ledgers"

VALID_RESOURCE_CLASSES = frozenset(
    {
        "skill-run",
        "subagent-run",
        "trace",
        "artifact",
        "external-resource",
    }
)
VALID_STATUSES = frozenset(
    {
        "active",
        "completed",
        "preserved",
        "handoff",
        "blocked",
        "failed",
    }
)


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


@dataclass
class LedgerResource:
    id: str
    resource_class: str
    locator: str
    owner: str
    status: str
    notes: List[str] = field(default_factory=list)
    created_at: str = field(default_factory=utc_timestamp)
    updated_at: str = field(default_factory=utc_timestamp)
    metadata: Dict[str, Any] = field(default_factory=dict)
    handoff: Dict[str, str] = field(default_factory=dict)
    blocked_reason: str = ""

    def to_dict(self) -> dict:
        data = asdict(self)
        data["class"] = data.pop("resource_class")
        return data

    @staticmethod
    def from_dict(data: dict) -> "LedgerResource":
        return LedgerResource(
            id=str(data.get("id", "")),
            resource_class=str(data.get("class", data.get("resource_class", ""))),
            locator=str(data.get("locator", "")),
            owner=str(data.get("owner", "")),
            status=str(data.get("status", "")),
            notes=list(data.get("notes", []) or []),
            created_at=str(data.get("created_at", "") or utc_timestamp()),
            updated_at=str(data.get("updated_at", "") or utc_timestamp()),
            metadata=dict(data.get("metadata", {}) or {}),
            handoff=dict(data.get("handoff", {}) or {}),
            blocked_reason=str(data.get("blocked_reason", "") or ""),
        )


@dataclass
class LedgerFinding:
    check: str
    severity: str
    detail: str
    resource_id: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def ledger_path(run_id: str, ledger_root: Optional[Path] = None) -> Path:
    root = ledger_root or DEFAULT_LEDGER_ROOT
    return root / f"{run_id}.json"


def empty_ledger(
    run_id: Optional[str] = None,
    *,
    source: str = "manual",
    mode: str = "",
) -> dict:
    now = utc_timestamp()
    return {
        "schema_version": SCHEMA_VERSION,
        "run": {
            "id": run_id or default_run_id(),
            "source": source,
            "mode": mode,
            "created_at": now,
            "updated_at": now,
        },
        "resources": [],
        "checks": {
            "no_active_resources": False,
            "no_failed_resources": False,
            "handoffs_have_target": False,
            "blocked_have_reason": False,
        },
    }


def save_ledger(ledger: dict, ledger_root: Optional[Path] = None) -> Path:
    run_id = str(ledger.get("run", {}).get("id", "") or default_run_id())
    ledger.setdefault("run", {})["id"] = run_id
    ledger["run"]["updated_at"] = utc_timestamp()
    out = ledger_path(run_id, ledger_root)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(ledger, indent=2, ensure_ascii=False, sort_keys=True)
    out.write_text(payload + "\n", encoding="utf-8")
    return out


def create_ledger(
    *,
    run_id: Optional[str] = None,
    source: str = "manual",
    mode: str = "",
    ledger_root: Optional[Path] = None,
) -> dict:
    ledger = empty_ledger(run_id, source=source, mode=mode)
    save_ledger(ledger, ledger_root)
    return ledger


def load_ledger(run_id: str, ledger_root: Optional[Path] = None) -> dict:
    path = ledger_path(run_id, ledger_root)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"ledger must be a JSON object: {path}")
    data.setdefault("resources", [])
    data.setdefault("checks", {})
    return data


def _find_resource(ledger: dict, resource_id: str) -> Optional[dict]:
    for resource in ledger.get("resources", []) or []:
        if resource.get("id") == resource_id:
            return resource
    return None


def _parse_metadata(raw: List[str]) -> Dict[str, str]:
    metadata: Dict[str, str] = {}
    for item in raw:
        if "=" not in item:
            raise ValueError(f"metadata must be key=value, got: {item}")
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"metadata key is empty in: {item}")
        metadata[key] = value
    return metadata


def upsert_resource(
    ledger: dict,
    *,
    resource_id: str,
    resource_class: str,
    locator: str,
    owner: str,
    status: str,
    note: str = "",
    metadata: Optional[Dict[str, Any]] = None,
    handoff_target: str = "",
    blocked_reason: str = "",
) -> dict:
    if resource_class not in VALID_RESOURCE_CLASSES:
        raise ValueError(f"invalid resource class: {resource_class}")
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid resource status: {status}")

    now = utc_timestamp()
    resource = _find_resource(ledger, resource_id)
    if resource is None:
        resource_obj = LedgerResource(
            id=resource_id,
            resource_class=resource_class,
            locator=locator,
            owner=owner,
            status=status,
            metadata=dict(metadata or {}),
        )
        resource = resource_obj.to_dict()
        ledger.setdefault("resources", []).append(resource)
    else:
        resource.update(
            {
                "class": resource_class,
                "locator": locator,
                "owner": owner,
                "status": status,
                "updated_at": now,
            }
        )
        existing_metadata = dict(resource.get("metadata", {}) or {})
        existing_metadata.update(metadata or {})
        resource["metadata"] = existing_metadata

    if note:
        resource.setdefault("notes", []).append(note)
    if handoff_target:
        resource["handoff"] = {"target": handoff_target}
    if blocked_reason:
        resource["blocked_reason"] = blocked_reason
    ledger.setdefault("run", {})["updated_at"] = now
    return ledger


def audit_ledger(ledger: dict) -> List[LedgerFinding]:
    findings: List[LedgerFinding] = []
    if ledger.get("schema_version") != SCHEMA_VERSION:
        findings.append(
            LedgerFinding(
                "schema_version",
                "error",
                f"expected {SCHEMA_VERSION}, got {ledger.get('schema_version')!r}",
            )
        )
    run = ledger.get("run")
    if not isinstance(run, dict) or not run.get("id"):
        findings.append(LedgerFinding("run_id", "error", "run.id is required"))

    seen: set[str] = set()
    for raw in ledger.get("resources", []) or []:
        resource = LedgerResource.from_dict(raw)
        if not resource.id:
            findings.append(LedgerFinding("resource_id", "error", "resource id is required"))
            continue
        if resource.id in seen:
            findings.append(LedgerFinding("duplicate_resource", "error", "resource id is duplicated", resource.id))
        seen.add(resource.id)
        if resource.resource_class not in VALID_RESOURCE_CLASSES:
            findings.append(
                LedgerFinding("resource_class", "error", f"invalid class: {resource.resource_class}", resource.id)
            )
        if resource.status not in VALID_STATUSES:
            findings.append(LedgerFinding("status", "error", f"invalid status: {resource.status}", resource.id))
        if not resource.locator:
            findings.append(LedgerFinding("locator", "error", "locator is required", resource.id))
        if not resource.owner:
            findings.append(LedgerFinding("owner", "error", "owner is required", resource.id))
        if resource.status == "active":
            findings.append(LedgerFinding("active_resource", "warning", "resource is still active", resource.id))
        if resource.status == "failed":
            findings.append(LedgerFinding("failed_resource", "error", "resource failed", resource.id))
        if resource.status == "handoff" and not resource.handoff.get("target"):
            findings.append(
                LedgerFinding(
                    "handoff_target",
                    "error",
                    "handoff resource needs handoff.target",
                    resource.id,
                )
            )
        if resource.status == "blocked" and not resource.blocked_reason:
            findings.append(
                LedgerFinding(
                    "blocked_reason",
                    "error",
                    "blocked resource needs blocked_reason",
                    resource.id,
                )
            )
    return findings


def update_checks(ledger: dict, findings: List[LedgerFinding]) -> dict:
    resources = [LedgerResource.from_dict(r) for r in ledger.get("resources", []) or []]
    ledger["checks"] = {
        "no_active_resources": not any(r.status == "active" for r in resources),
        "no_failed_resources": not any(r.status == "failed" for r in resources),
        "handoffs_have_target": not any(
            r.status == "handoff" and not r.handoff.get("target") for r in resources
        ),
        "blocked_have_reason": not any(r.status == "blocked" and not r.blocked_reason for r in resources),
        "schema_valid": not any(
            f.severity == "error" for f in findings if f.check in {"schema_version", "run_id"}
        ),
    }
    return ledger


def ledger_summary(ledger: dict) -> dict:
    by_status: Dict[str, int] = {}
    by_class: Dict[str, int] = {}
    for raw in ledger.get("resources", []) or []:
        resource = LedgerResource.from_dict(raw)
        by_status[resource.status] = by_status.get(resource.status, 0) + 1
        by_class[resource.resource_class] = by_class.get(resource.resource_class, 0) + 1
    return {
        "run_id": ledger.get("run", {}).get("id", ""),
        "resource_count": len(ledger.get("resources", []) or []),
        "by_status": by_status,
        "by_class": by_class,
        "checks": dict(ledger.get("checks", {}) or {}),
    }


def metadata_from_cli(items: Optional[List[str]]) -> Dict[str, str]:
    return _parse_metadata(items or [])
