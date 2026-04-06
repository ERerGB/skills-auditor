---
name: skills-auditor-sync
description: >
  Skills Auditor cycle 5 (optional) — skills-audit sync with --map-file to plan or apply relinking
  to canonical skill sources. Sub-skill of skills-auditor.
---

# Skills Auditor — Sync (cycle 5)

## When to use

- Operator has a maintained mapping file (see `config/sources.example.json`).
- Scoped: “sync dry-run”, `SKILLS_AUDITOR_MODE=sync`, or `SKILLS_AUDITOR_SYNC_MAP_FILE` set in pipeline env.

## Commands

```bash
skills-audit sync \
  --skills-dir "$HOME/.cursor/skills" \
  --map-file config/sources.example.json

skills-audit sync \
  --skills-dir "$HOME/.cursor/skills" \
  --skills-dir "$HOME/.claude/skills" \
  --map-file config/sources.example.json \
  --apply
```

## Safety

- Raw CLI defaults to dry-run; **`/skills-auditor`** top skill defaults to **`--apply`** on sync when `SKILLS_AUDITOR_SYNC_MAP_FILE` is set, unless dry-run or `SKILLS_AUDITOR_DRY_RUN=1`.

## Parent

[`../../SKILL.md`](../../SKILL.md).
