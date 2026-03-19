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
4. If user wants canonical sync, prepare a JSON mapping file.
5. Run sync in dry-run mode and review planned actions.
6. Run sync with `--apply` only after approval.
7. Re-run `audit --with-drift` to verify final state.

## Commands

```bash
# Set these for your environment first
PRIMARY_SKILLS_DIR="${PRIMARY_SKILLS_DIR:-/path/to/primary/skills}"
SECONDARY_SKILLS_DIR="${SECONDARY_SKILLS_DIR:-/path/to/secondary/skills}"

# Audit (basic)
python3 scripts/skills_audit.py audit \
  --skills-dir "$PRIMARY_SKILLS_DIR"

# Audit with drift (shows remote URL for synced skills, local path for drifted)
python3 scripts/skills_audit.py audit \
  --skills-dir "$PRIMARY_SKILLS_DIR" \
  --with-drift

# Drift check (standalone — git fetch + ahead/behind/dirty for each symlinked skill)
python3 scripts/skills_audit.py drift-check \
  --skills-dir "$PRIMARY_SKILLS_DIR"

# Dry-run sync
python3 scripts/skills_audit.py sync \
  --skills-dir "$PRIMARY_SKILLS_DIR" \
  --map-file config/sources.example.json

# Discovery-layer audit
python3 scripts/skills_audit.py audit-discovery \
  --source "$PRIMARY_SKILLS_DIR" \
  --source "$SECONDARY_SKILLS_DIR"

# Discovery profile (recommended)
python3 scripts/skills_audit.py audit-discovery \
  --profile-file config/discovery-profile.example.json

# Summary-only + CI gating
python3 scripts/skills_audit.py audit-discovery \
  --profile-file config/discovery-profile.example.json \
  --summary-only \
  --fail-on-conflict \
  --fail-on-hash-conflict

# Apply sync
python3 scripts/skills_audit.py sync \
  --skills-dir "$PRIMARY_SKILLS_DIR" \
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
