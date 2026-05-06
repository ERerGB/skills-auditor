---
name: skills-auditor-discover
description: >
  Skills Auditor cycle 1 — discover filesystem state: skills-audit audit (optional --with-drift),
  drift-check, and optional audit-discovery with a profile file. Sub-skill of skills-auditor.
---

# Skills Auditor — Discover (cycle 1)

## When to use

- Scoped request: “audit only”, “drift check”, “discovery profile”, first leg of pipeline.
- Operator narrowed `SKILLS_AUDITOR_MODE=discover` (see top [`SKILL.md`](../../SKILL.md)).

## Commands

Install: `pip install -e .` from repo root, or `python3 scripts/skills_audit.py`.

```bash
# Basic audit (repeat --skills-dir for each root)
skills-audit audit --skills-dir "$HOME/.cursor/skills"

skills-audit audit \
  --skills-dir "$HOME/.cursor/skills" \
  --skills-dir "$HOME/.claude/skills" \
  --with-drift

skills-audit drift-check --skills-dir "$HOME/.cursor/skills"
```

## Optional — discovery-layer audit

When multi-source collision maps matter:

```bash
skills-audit audit-discovery \
  --profile-file config/discovery-profile.cursor-jz.example.json

skills-audit audit-discovery \
  --profile-file config/discovery-profile.cursor-jz.example.json \
  --summary-only \
  --fail-on-conflict \
  --fail-on-hash-conflict
```

## Outputs to hand off

- Tables: `link_status`, `has_skill_md`, duplicate `name:` **install-root** status (Slash-style recursive scan), optional drift columns.
- JSON blocks in CLI output for scripting.

## Drift behavior

- Symlinked skills with git context: `git fetch`, report synced vs DRIFT (`ahead` / `behind` / `dirty`).
- Plain directories without git: drift skipped for those entries.

## Parent

Return to full pipeline: [`../../SKILL.md`](../../SKILL.md).
