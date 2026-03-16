---
name: skills-auditor
description: Audit and synchronize local skill directories by detecting broken links, drifted local copies, and ref mismatches, then applying safe relink operations from a mapping file. Use when user asks to clean, organize, or sync skill folders.
---

# Skills Auditor

## When to Use

- User asks to audit `~/.cursor/skills` or similar directories.
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
# Audit
python3 scripts/skills_audit.py audit \
  --skills-dir "$HOME/.cursor/skills"

# Dry-run sync
python3 scripts/skills_audit.py sync \
  --skills-dir "$HOME/.cursor/skills" \
  --map-file config/sources.example.json

# Discovery-layer audit
python3 scripts/skills_audit.py audit-discovery \
  --source "$HOME/.cursor/skills" \
  --source "$HOME/.cursor/skills-cursor"

# Discovery profile (recommended)
python3 scripts/skills_audit.py audit-discovery \
  --profile-file config/discovery-profile.cursor-jz.example.json

# Apply sync
python3 scripts/skills_audit.py sync \
  --skills-dir "$HOME/.cursor/skills" \
  --map-file config/sources.example.json \
  --apply
```

## Safety Rules

- Default mode is dry-run.
- Never apply without user confirmation.
- For non-symlink existing entries, archive to timestamped backup before relinking.
- If target has no `SKILL.md`, skip and report as error.
- For discovery collisions, prefer profile-based source priority and keep same-hash folding enabled.
