---
name: skills-auditor
description: Audit and synchronize local skill directories by detecting broken links, drifted local copies, and ref mismatches, then applying safe relink operations from a mapping file. Use when user asks to clean, organize, or sync skill folders.
---

# Skills Auditor

## When to Use

- User asks to audit one or more local skill directories in their current environment.
- User asks to detect broken skill links.
- User asks to sync local skills to canonical repositories.
- User wants one-click cleanup with dry-run safety.
- User has multi-platform skill packs (Cursor + Codex + Claude Code) and wants Select-One Routing.

## Isolation: Run as Subagent

Each audit or route run should produce **reproducible, context-free results**.
To avoid session context bleeding between runs, invoke `skills-audit` from
an **isolated subagent** whenever the runtime supports it.

> **Principle**: Trace data is fact. Interpretation is inference. Keep them
> separated by running the fact-collection phase in a zero-context subagent.

**Minimum viable isolation**: use your IDE's native subagent/Task mechanism
with `readonly: true` (Cursor `Task` tool, Claude Code `--allowedTools`).

For dedicated harness options (SSOT compilation, git-worktree parallelism,
role-based delegation), see the
[Isolation Harness Recommendations](https://github.com/ERerGB/skills-auditor#isolation-harness-recommendations)
section in the project README.

## Workflow

1. Run filesystem audit first (`audit`).
2. Run drift check (`drift-check`) to verify local-remote sync.
3. Run discovery-layer audit (`audit-discovery`) to detect collisions.
4. **Dedup bundles with mirror copies** (`dedup --apply`) — replaces duplicate SKILL.md files with symlinks to the canonical (shortest-path) file. Best practice after installing or updating skill packs.
5. **Select-One Routing** (`route --platform <name>`) — for multi-platform skill packs, classifies each variant by platform convention, selects one per identity, resolves the rest (archive / delete / keep). Writes a structured trace to `~/.skills-auditor/traces/`.
6. **Audit state machine** (`audit-state-machine`) — validates accumulated traces against transition rules (illegal transitions, terminal coverage, dead paths, signal gaps, cross-run consistency).
7. If user wants canonical sync, prepare a JSON mapping file.
8. Run sync in dry-run mode and review planned actions. For cross-agent installs, use `--discovery-profile` and `--target-platform` (see repo README).
9. Run sync with `--apply` only after approval.
10. Re-run `audit --with-drift` to verify final state.
11. **`audit` runs a duplicate-name check by default** (same `name:` on more than one **resolved** `SKILL.md` under one bundle; symlinks to the same file count once). Use `--skip-duplicate-name-check` to turn off; `--fail-on-duplicate-names` for CI exit code 4.

## Commands

Install once from the repo root: `pip install -e .` — then use the **`skills-audit`** CLI (or `python -m skills_auditor`). From an uninstalled clone you can still run `python3 scripts/skills_audit.py`.

```bash
PRIMARY_SKILLS_DIR="${PRIMARY_SKILLS_DIR:-/path/to/primary/skills}"
SECONDARY_SKILLS_DIR="${SECONDARY_SKILLS_DIR:-/path/to/secondary/skills}"

# Audit (basic)
skills-audit audit \
  --skills-dir "$PRIMARY_SKILLS_DIR"

# Audit Cursor + Claude Code global skills roots (repeat --skills-dir)
skills-audit audit \
  --skills-dir "$HOME/.cursor/skills" \
  --skills-dir "$HOME/.claude/skills"

# Audit with drift
skills-audit audit \
  --skills-dir "$PRIMARY_SKILLS_DIR" \
  --with-drift

# Drift check (standalone)
skills-audit drift-check \
  --skills-dir "$PRIMARY_SKILLS_DIR"

# Dry-run sync
skills-audit sync \
  --skills-dir "$PRIMARY_SKILLS_DIR" \
  --map-file config/sources.example.json

# Discovery-layer audit
skills-audit audit-discovery \
  --profile-file config/discovery-profile.cursor-jz.example.json

# Summary-only + CI gating
skills-audit audit-discovery \
  --profile-file config/discovery-profile.cursor-jz.example.json \
  --summary-only \
  --fail-on-conflict \
  --fail-on-hash-conflict

# ── Dedup (legacy, hash-aware) ──

# Dry-run
skills-audit dedup \
  --skills-dir "$PRIMARY_SKILLS_DIR"

# Apply
skills-audit dedup \
  --skills-dir "$HOME/.claude/skills" \
  --apply

# ── Select-One Routing (v0.6.0+) ──

# Dry-run: see what route would do for Cursor
skills-audit route \
  --platform cursor \
  --skills-dir "$HOME/.cursor/skills"

# Route for Codex with delete strategy
skills-audit route \
  --platform codex \
  --skills-dir "$HOME/.claude/skills" \
  --strategy delete

# Apply routing
skills-audit route \
  --platform cursor \
  --skills-dir "$HOME/.cursor/skills" \
  --strategy archive \
  --apply

# Custom trace output dir
skills-audit route \
  --platform cursor \
  --skills-dir "$HOME/.cursor/skills" \
  --trace-dir ./my-traces

# ── State Machine Audit ──

# Validate all accumulated traces
skills-audit audit-state-machine

# Audit traces from a specific directory
skills-audit audit-state-machine \
  --trace-dir ./my-traces

# Apply sync
skills-audit sync \
  --skills-dir "$PRIMARY_SKILLS_DIR" \
  --map-file config/sources.example.json \
  --apply
```

## Select-One Routing (v0.6.0)

Four-phase pipeline for multi-platform skill packs:

```
DISCOVERED → CLASSIFIED → ROUTED → RESOLVED

Phase 1 (Discover):  hash all variants
Phase 2 (Classify):  infer platform via path convention (.agents/→codex, .factory/→factory)
Phase 3 (Route):     exact match > wildcard; select one per identity
Phase 4 (Resolve):   ACTIVE / ARCHIVED / DELETED / KEPT_HIDDEN / FLAGGED
```

Every transition is recorded in a JSON trace file (`~/.skills-auditor/traces/`).
Use `audit-state-machine` to batch-validate traces against transition rules.

### Resolve strategies

| Strategy | Superseded variants become | Use when |
|----------|---------------------------|----------|
| `archive` (default) | Renamed to `SKILL.md.archived-<timestamp>` | Safe — can undo |
| `delete` | Removed from disk | Confident cleanup |
| `keep` | Left in place (no filesystem change) | Audit-only, no side effects |

## Drift Check Behavior

- Only checks symlinked skills (directories without git context are skipped).
- Runs `git fetch` for each skill's repo to get latest remote state.
- Reports: `synced` (ahead=0, behind=0, dirty=0) or `DRIFT` with details.
- **Display rule**: synced skills show the remote GitHub URL as their target; drifted skills show the local filesystem path. This makes the canonical source of truth immediately visible.
- `audit --with-drift` merges drift data into the standard audit table.

## Safety Rules

- Default mode is dry-run.
- Never apply without user confirmation.
- For non-symlink existing entries, archive to timestamped backup before relinking.
- If target has no `SKILL.md`, skip and report as error.
- For discovery collisions, prefer profile-based source priority and keep same-hash folding enabled.
- Use `--summary-only` and fail flags for periodic CI-style health checks.
- **Isolation**: prefer subagent invocation for audit runs to avoid context pollution between sessions.
