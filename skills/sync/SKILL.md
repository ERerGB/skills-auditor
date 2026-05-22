---
name: skills-auditor-sync
description: >
  Skills Auditor cycle 5 (optional) — skills-audit sync with --map-file or sync-discover
  to plan or apply relinking to canonical skill sources. Sub-skill of skills-auditor.
---

# Skills Auditor — Sync (cycle 5)

## When to use

- Operator has a maintained mapping file (see `config/sources.example.json`) or wants discovery-driven replication from source roots.
- Scoped: “sync dry-run”, `SKILLS_AUDITOR_MODE=sync`, `SKILLS_AUDITOR_SYNC_MAP_FILE`, or requests to replicate skills across agent environments.

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

skills-audit sync-discover \
  --source .agents/skills \
  --source .cursor/skills \
  --source .claude/skills \
  --skills-dir .codex/skills \
  --skills-dir "$HOME/.codex/skills"
```

## Safety

- Raw CLI defaults to dry-run for both `sync` and `sync-discover`; **`/skills-auditor`** top skill defaults to **`--apply`** on sync when `SKILLS_AUDITOR_SYNC_MAP_FILE` is set, unless dry-run or `SKILLS_AUDITOR_DRY_RUN=1`.
- `sync-discover` converts slash names to top-level install aliases and excludes each target root from its own generated plan to avoid replacing canonical local directories with self-links.

## Parent

[`../../SKILL.md`](../../SKILL.md).
