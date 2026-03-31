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

## Workflow

1. Run filesystem audit first (`audit`).
2. Run drift check (`drift-check`) to verify local-remote sync.
3. Run discovery-layer audit (`audit-discovery`) to detect collisions.
4. **Dedup bundles with mirror copies** (`dedup --apply`) — replaces duplicate SKILL.md files with symlinks to the canonical (shortest-path) file. Best practice after installing or updating skill packs.
5. If user wants canonical sync, prepare a JSON mapping file.
6. Run sync in dry-run mode and review planned actions. For cross-agent installs, use `--discovery-profile` and `--target-platform` (see repo README).
7. Run sync with `--apply` only after approval.
8. Re-run `audit --with-drift` to verify final state.
9. **`audit` runs a duplicate-name check by default** (same `name:` on more than one **resolved** `SKILL.md` under one bundle; symlinks to the same file count once). E.g. gstack `.agents` copies vs primary trees. Use `--skip-duplicate-name-check` to turn off; `--fail-on-duplicate-names` for CI exit code 4.

## Commands

Install once from the repo root: `pip install -e .` — then use the **`skills-audit`** CLI (or `python -m skills_auditor`). From an uninstalled clone you can still run `python3 scripts/skills_audit.py`.

```bash
# Set these for your environment first
PRIMARY_SKILLS_DIR="${PRIMARY_SKILLS_DIR:-/path/to/primary/skills}"
SECONDARY_SKILLS_DIR="${SECONDARY_SKILLS_DIR:-/path/to/secondary/skills}"

# Audit (basic)
skills-audit audit \
  --skills-dir "$PRIMARY_SKILLS_DIR"

# Audit Cursor + Claude Code global skills roots (repeat --skills-dir)
skills-audit audit \
  --skills-dir "$HOME/.cursor/skills" \
  --skills-dir "$HOME/.claude/skills"

# Audit with drift (shows remote URL for synced skills, local path for drifted)
skills-audit audit \
  --skills-dir "$PRIMARY_SKILLS_DIR" \
  --with-drift

# Drift check (standalone — git fetch + ahead/behind/dirty for each symlinked skill)
skills-audit drift-check \
  --skills-dir "$PRIMARY_SKILLS_DIR"

# Dry-run sync
skills-audit sync \
  --skills-dir "$PRIMARY_SKILLS_DIR" \
  --map-file config/sources.example.json

# Dry-run sync both Cursor and Claude Code (same mapping file)
skills-audit sync \
  --skills-dir "$HOME/.cursor/skills" \
  --skills-dir "$HOME/.claude/skills" \
  --map-file config/sources.example.json

# Discovery-layer audit
skills-audit audit-discovery \
  --source "$PRIMARY_SKILLS_DIR" \
  --source "$SECONDARY_SKILLS_DIR"

# Discovery profile (recommended)
skills-audit audit-discovery \
  --profile-file config/discovery-profile.cursor-jz.example.json

# Summary-only + CI gating
skills-audit audit-discovery \
  --profile-file config/discovery-profile.cursor-jz.example.json \
  --summary-only \
  --fail-on-conflict \
  --fail-on-hash-conflict

# Dedup: dry-run (detect duplicate names, plan symlink replacements)
skills-audit dedup \
  --skills-dir "$PRIMARY_SKILLS_DIR"

# Dedup: apply (replace duplicates with symlinks to canonical)
skills-audit dedup \
  --skills-dir "$HOME/.claude/skills" \
  --apply

# Apply sync
skills-audit sync \
  --skills-dir "$PRIMARY_SKILLS_DIR" \
  --map-file config/sources.example.json \
  --apply

# Apply sync to both agent roots
skills-audit sync \
  --skills-dir "$HOME/.cursor/skills" \
  --skills-dir "$HOME/.claude/skills" \
  --map-file config/sources.example.json \
  --apply
```

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
