---
name: skills-auditor-traces
description: >
  Skills Auditor cycle 4 — skills-audit audit-state-machine over routing traces under
  ~/.skills-auditor/traces/. Sub-skill of skills-auditor.
---

# Skills Auditor — Trace QA (cycle 4)

## When to use

- After one or more `route` runs (dry-run or `--apply`) produced trace JSON.
- Scoped: `SKILLS_AUDITOR_MODE=traces`.

## Commands

```bash
skills-audit audit-state-machine

skills-audit audit-state-machine --trace-dir ./my-traces
```

## Interpretation

- **errors**: fix routing or file state before relying on traces.
- **warnings / info**: e.g. unused state-machine states on small trace sets — often benign.

## Parent

[`../../SKILL.md`](../../SKILL.md).
