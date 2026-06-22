#!/usr/bin/env python3
"""Codex plugin hook wrapper for Skills Auditor sensor events."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path


def _codex_home() -> Path:
    raw = os.environ.get("CODEX_HOME")
    return Path(raw).expanduser() if raw else Path.home() / ".codex"


def _repo_from_codex_config(here: Path) -> Path | None:
    parts = here.parts
    try:
        cache_idx = parts.index("cache")
    except ValueError:
        return None
    if cache_idx + 1 >= len(parts):
        return None
    marketplace_name = parts[cache_idx + 1]
    config_path = _codex_home() / "config.toml"
    if not config_path.is_file():
        return None
    text = config_path.read_text(encoding="utf-8")
    source = ""
    try:
        import tomllib

        config = tomllib.loads(text)
        marketplace = config.get("marketplaces", {}).get(marketplace_name, {})
        raw_source = marketplace.get("source")
        if isinstance(raw_source, str):
            source = raw_source
    except Exception:
        source = _fallback_marketplace_source(text, marketplace_name)
    if not source:
        return None
    candidate = Path(source).expanduser()
    if (candidate / "skills_auditor" / "observability.py").is_file():
        return candidate
    return None


def _fallback_marketplace_source(config_text: str, marketplace_name: str) -> str:
    header = f"[marketplaces.{marketplace_name}]"
    in_section = False
    for raw in config_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            in_section = line == header
            continue
        if not in_section:
            continue
        match = re.match(r'^source\s*=\s*"([^"]+)"\s*$', line)
        if match:
            return match.group(1)
    return ""


def _add_repo_to_path() -> None:
    candidates = []
    env_repo = os.environ.get("SKILLS_AUDITOR_REPO")
    if env_repo:
        candidates.append(Path(env_repo).expanduser())
    here = Path(__file__).resolve()
    config_repo = _repo_from_codex_config(here)
    if config_repo is not None:
        candidates.append(config_repo)
    candidates.extend(here.parents)
    for candidate in candidates:
        if (candidate / "skills_auditor" / "observability.py").is_file():
            sys.path.insert(0, str(candidate))
            return


def _read_payload(input_file: str) -> dict:
    if input_file == "-":
        raw = sys.stdin.read()
    else:
        raw = Path(input_file).expanduser().read_text(encoding="utf-8")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("hook payload must be a JSON object")
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Record one Skills Auditor sensor event.")
    parser.add_argument("--provider", required=True, help="Provider label, e.g. codex.")
    parser.add_argument("--source", default="hook", help="Sensor source label.")
    parser.add_argument(
        "--log-dir",
        default=os.environ.get("SKILLS_AUDITOR_LOG_DIR", ".skills-auditor-local"),
        help="Local sensor log root.",
    )
    parser.add_argument("--input-file", default="-", help="JSON payload file, or '-' for stdin.")
    parser.add_argument("--resolve-path", action="store_true", help="Resolve observed paths.")
    parser.add_argument("--hash-path", action="store_true", help="Hash observed files.")
    parser.add_argument("--dry-run", action="store_true", help="Print normalized event without writing.")
    parser.add_argument("--print-event", action="store_true", help="Print the normalized event after writing.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    _add_repo_to_path()
    try:
        from skills_auditor.observability import sensor_event_from_payload, write_sensor_event

        payload = _read_payload(args.input_file)
        event = sensor_event_from_payload(
            payload,
            provider=args.provider,
            source=args.source,
            resolve_path=args.resolve_path,
            hash_path=args.hash_path,
        )
        if not args.dry_run:
            write_sensor_event(event, Path(args.log_dir).expanduser())
        if args.dry_run or args.print_event:
            print(json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"skill-trace sensor hook failed: {exc}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
