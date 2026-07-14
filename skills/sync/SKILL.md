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
- A `sync-discover` dry-run exits **1** when discovery entries are missing or stale, **5** for skipped unsafe actions, and **0** only when every discovered entry is already current. Apply creates a missing install root before registering entries.
- In the top-level full pipeline, an explicit map remains authoritative. Without one, existing repository `.agents/.cursor/.claude` roots are discovered and registered into `.codex/skills` when the repository has a `.codex/` directory. Override with `SKILLS_AUDITOR_DISCOVERY_SOURCES` and `SKILLS_AUDITOR_INSTALL_ROOTS`.
- `sync-discover` converts slash names to top-level install aliases and excludes each target root from its own generated plan to avoid replacing canonical local directories with self-links.

## Ledger behavior

- Mode: dry-run is read-only; `--apply` may create, replace, or relink skill entries in each target root.
- Suggested rows: `skill-run` for the sync cycle, `external-resource` for the map file or discovery sources, and `artifact` rows for captured sync plans or applied link changes.
- Applied link changes should include locators for the target skill root entries and metadata such as `action=create_link`, `replace_link`, or `archive_and_link`.
- Skipped or unsafe actions should become `blocked` rows with the reason, or `handoff` rows when a human/operator must decide.

## Parent

[`../../SKILL.md`](../../SKILL.md).
