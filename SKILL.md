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
2. Run discovery-layer audit (`audit-discovery`) to detect collisions.
3. If user wants canonical sync, prepare a JSON mapping file.
4. Run sync in dry-run mode and review planned actions.
5. Run sync with `--apply` only after approval.
6. Re-run `audit` + `audit-discovery` to verify final state.

## Commands

```bash
# Set these for your environment first
PRIMARY_SKILLS_DIR="${PRIMARY_SKILLS_DIR:-/path/to/primary/skills}"
SECONDARY_SKILLS_DIR="${SECONDARY_SKILLS_DIR:-/path/to/secondary/skills}"

# Audit
python3 scripts/skills_audit.py audit \
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

## Safety Rules

- Default mode is dry-run.
- Never apply without user confirmation.
- For non-symlink existing entries, archive to timestamped backup before relinking.
- If target has no `SKILL.md`, skip and report as error.
- For discovery collisions, prefer profile-based source priority and keep same-hash folding enabled.
- Use `--summary-only` and fail flags for periodic CI-style health checks.
