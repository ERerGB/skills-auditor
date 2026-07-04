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

## Ledger behavior

- Mode: read-only. This cycle audits existing route trace JSON and does not mutate skill files.
- Suggested rows: `skill-run` for the trace QA cycle and `trace` rows for each route trace directory or file audited.
- Use `completed` when there are no trace errors. Use `failed` for trace schema or transition errors. Use `blocked` when required trace files are missing and another cycle must regenerate them.
- Cleanup is limited to operator-approved trace retention; do not delete traces just because the ledger check passed.

## Parent

[`../../SKILL.md`](../../SKILL.md).
