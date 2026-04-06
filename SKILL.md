---
name: skills-auditor
description: >
  Top entry for the Skills Auditor skill system: DEFAULT full pipeline applies filesystem changes
  (dedup/route/sync use --apply) unless the operator explicitly asks for dry-run or sets
  SKILLS_AUDITOR_DRY_RUN=1. Sub-skills under skills/. Triggers: /skills-auditor, skills audit, dedup,
  route, drift, discovery profile.
---

# Skills Auditor (top)

## Default behavior (full pipeline + apply)

When the user invokes **`/skills-auditor`** or this skill **without** asking to stay read-only:

1. Load optional config: if `SKILLS_AUDITOR_CONFIG` points to an env file, `source` it.
2. If **`SKILLS_AUDITOR_MODE`** is unset or `full`, run **all** cycles in order. For **`dedup`**, **`route`**, and **`sync`**, pass **`--apply`** by default (mutating steps actually change disk).
3. **Dry-run exception:** if the user **explicitly** asks for dry-run (e.g. “dry run”, “只预览”, “不要 apply”, “plan only”) **or** **`SKILLS_AUDITOR_DRY_RUN=1`**, then **omit** `--apply` on those commands.
4. If **`SKILLS_AUDITOR_MODE`** is a single cycle name, run **only** that cycle (same apply vs dry-run rules).

**Narrowing without env files:** “audit only” → discover only (never mutating). “dedup dry-run” → dedup without `--apply`. “route Codex” with no dry-run wording → route **with** `--apply` for that run.

## Configuration (optional)

Template: [`config/skills-auditor.pipeline.example.env`](config/skills-auditor.pipeline.example.env).

| Variable | Purpose |
| --- | --- |
| `SKILLS_AUDITOR_ROOTS` | Space-separated skill roots → expand to repeated `--skills-dir` |
| `SKILLS_AUDITOR_EXTRA_ROOTS` | Optional extra roots appended to the list |
| `SKILLS_AUDITOR_WITH_DRIFT` | `1` → add `--with-drift` on discover + close audits |
| `SKILLS_AUDITOR_MODE` | `full` (default) \| `discover` \| `dedup` \| `route` \| `traces` \| `sync` \| `close` |
| `SKILLS_AUDITOR_ROUTE_PLATFORMS` | Comma-separated, e.g. `cursor,codex` |
| `SKILLS_AUDITOR_ROUTE_STRATEGY` | `archive` (default) \| `delete` \| `keep` |
| `SKILLS_AUDITOR_SYNC_MAP_FILE` | If non-empty, sync cycle runs with this `--map-file` |
| `SKILLS_AUDITOR_DRY_RUN` | `1` → **no** `--apply` on dedup / route / sync (overrides default apply) |
| `SKILLS_AUDITOR_CONFIG` | Path to an env file to source for the above |

**Default roots** when nothing is configured: `$HOME/.cursor/skills` and `$HOME/.claude/skills`.

### Shell helper (build `--skills-dir` args)

```bash
_roots="${SKILLS_AUDITOR_ROOTS:-$HOME/.cursor/skills $HOME/.claude/skills}"
_roots="$_roots ${SKILLS_AUDITOR_EXTRA_ROOTS:-}"
AUDIT_DIRS=()
for d in $_roots; do
  [ -d "$d" ] && AUDIT_DIRS+=(--skills-dir "$d")
done
DRIFT_FLAG=()
[ "${SKILLS_AUDITOR_WITH_DRIFT:-0}" = "1" ] && DRIFT_FLAG=(--with-drift)
```

Use `"${AUDIT_DIRS[@]}"` and `"${DRIFT_FLAG[@]}"` in `skills-audit` invocations.

## Sub-skill system (progressive disclosure)

| Cycle | Sub-skill |
| --- | --- |
| 1 — Discover | [`skills/discover/SKILL.md`](skills/discover/SKILL.md) |
| 2 — Dedup | [`skills/dedup/SKILL.md`](skills/dedup/SKILL.md) |
| 3 — Route | [`skills/route/SKILL.md`](skills/route/SKILL.md) |
| 4 — Trace QA | [`skills/traces/SKILL.md`](skills/traces/SKILL.md) |
| 5 — Sync (optional) | [`skills/sync/SKILL.md`](skills/sync/SKILL.md) |
| 6 — Close | [`skills/close/SKILL.md`](skills/close/SKILL.md) |

Index: [`skills/README.md`](skills/README.md).

## Full pipeline recipe (agent-executable)

**Default:** mutating steps use `--apply` unless `SKILLS_AUDITOR_DRY_RUN=1`.

```bash
set -euo pipefail
# Optional: set -a && source "$SKILLS_AUDITOR_CONFIG" && set +a

MODE="${SKILLS_AUDITOR_MODE:-full}"
_roots="${SKILLS_AUDITOR_ROOTS:-$HOME/.cursor/skills $HOME/.claude/skills}"
_roots="$_roots ${SKILLS_AUDITOR_EXTRA_ROOTS:-}"
AUDIT_DIRS=()
for d in $_roots; do [ -d "$d" ] && AUDIT_DIRS+=(--skills-dir "$d"); done
DRIFT_FLAG=()
[ "${SKILLS_AUDITOR_WITH_DRIFT:-1}" = "1" ] && DRIFT_FLAG=(--with-drift)
RSTRAT="${SKILLS_AUDITOR_ROUTE_STRATEGY:-archive}"

APPLY_MUT=()
if [ "${SKILLS_AUDITOR_DRY_RUN:-0}" != "1" ]; then
  APPLY_MUT=(--apply)
fi

run_discover() { skills-audit audit "${AUDIT_DIRS[@]}" "${DRIFT_FLAG[@]}"; }
run_close()    { skills-audit audit "${AUDIT_DIRS[@]}" "${DRIFT_FLAG[@]}"; }
run_dedup()    { skills-audit dedup "${AUDIT_DIRS[@]}" "${APPLY_MUT[@]}"; }
run_traces()   { skills-audit audit-state-machine; }
run_sync() {
  [ -n "${SKILLS_AUDITOR_SYNC_MAP_FILE:-}" ] || return 0
  skills-audit sync "${AUDIT_DIRS[@]}" --map-file "$SKILLS_AUDITOR_SYNC_MAP_FILE" "${APPLY_MUT[@]}"
}

run_route_all() {
  IFS=',' read -r -a plats <<< "${SKILLS_AUDITOR_ROUTE_PLATFORMS:-cursor,codex}"
  for p in "${plats[@]}"; do
    p="$(echo "$p" | xargs)"
    [ -n "$p" ] || continue
    skills-audit route --platform "$p" "${AUDIT_DIRS[@]}" --strategy "$RSTRAT" "${APPLY_MUT[@]}"
  done
}

case "$MODE" in
  discover) run_discover ;;
  dedup)    run_dedup ;;
  route)    run_route_all ;;
  traces)   run_traces ;;
  sync)     run_sync ;;
  close)    run_close ;;
  full)
    run_discover
    run_dedup
    run_route_all
    run_traces
    run_sync
    run_close
    ;;
  *) echo "Unknown SKILLS_AUDITOR_MODE=$MODE" >&2; exit 1 ;;
esac
```

## Isolation

**Apply runs change the filesystem** — do **not** use `readonly` subagents for the default `/skills-auditor` pass. Use **readonly** only when the operator asked for **dry-run** / `SKILLS_AUDITOR_DRY_RUN=1`. See [Isolation Harness Recommendations](https://github.com/ERerGB/skills-auditor#isolation-harness-recommendations).

## Safety rules (this skill contract)

- **`/skills-auditor` default:** `dedup`, `route`, and `sync` run with **`--apply`** when the user does **not** ask for dry-run.
- **Opt out:** explicit “dry run” / “preview only” / **`SKILLS_AUDITOR_DRY_RUN=1`** → omit `--apply`.
- **Destructive strategy:** `SKILLS_AUDITOR_ROUTE_STRATEGY=delete` removes files; only use when the operator is explicit.

## CLI install

`pip install -e .` from this repo, or `python3 scripts/skills_audit.py` from the repo root. Full command reference lives in sub-skills and in [`README.md`](README.md).
