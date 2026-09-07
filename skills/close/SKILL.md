---
name: skills-auditor-close
description: >
  Skills Auditor cycle 6 — repeat discover audit (prefer --with-drift) to confirm end state after
  dedup/route/sync. Sub-skill of skills-auditor.
---

# Skills Auditor — Close (cycle 6)

## Before every invocation

Run `skills-audit skill-trace check` before this cycle, including direct invocation.
If capture is disabled, continue quietly. If enabled but unverified, stale, or errored,
report the capture gap once and continue the cycle; do not auto-enable or trust hooks.
See the [parent preflight contract](../../SKILL.md#before-every-invocation-optional-skill-trace-preflight).

## When to use

- Final leg of full pipeline after cycles 1–5 (or 1–4 if sync skipped).
- Scoped: `SKILLS_AUDITOR_MODE=close`.

## Commands

Same as discover: re-run `skills-audit audit` with the same `--skills-dir` list as cycle 1, including `--with-drift` when drift matters.

```bash
skills-audit audit \
  --skills-dir "$HOME/.cursor/skills" \
  --skills-dir "$HOME/.claude/skills" \
  --with-drift
```

## Ledger behavior

- Mode: read-only, except optional `git fetch` during drift checks.
- Suggested row: final `skill-run` for close verification with `status=completed` only when duplicate, metadata, drift, trace, and sync expectations are acceptable for the run.
- If any previous cycle left `active`, `failed`, or unreasoned `blocked` rows, run `skills-audit ledger-check` and update or hand off those rows before closing.
- Close does not delete artifacts, route traces, or local logs; record cleanup as a separate `handoff` if retention work is needed.

## Parent

[`../../SKILL.md`](../../SKILL.md) · Detail: [`../discover/SKILL.md`](../discover/SKILL.md).
