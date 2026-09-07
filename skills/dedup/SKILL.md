---
name: skills-auditor-dedup
description: >
  Skills Auditor cycle 2 — plan-first hash-aware deduplication. Applies only when the operator
  explicitly authorizes writes. Sub-skill of skills-auditor.
---

# Skills Auditor — Dedup (cycle 2)

## Before every invocation

Run `skills-audit skill-trace check` before this cycle, including direct invocation.
If capture is disabled, continue quietly. If enabled but unverified, stale, or errored,
report the capture gap once and continue the cycle; do not auto-enable or trust hooks.
See the [parent preflight contract](../../SKILL.md#before-every-invocation-optional-skill-trace-preflight).

## When to use

- After discover shows duplicate `name:` with **identical** content (same hash).
- Scoped: “dedup dry-run”, `SKILLS_AUDITOR_MODE=dedup`.

## Important

- Dedup scans the **entire** install root passed to `--skills-dir` (Slash-style recursive view), so it catches both in-pack mirrors and **sibling-folder** duplicates (e.g. `browse/` vs `gstack/browse/`).
- **Different hashes** → dedup reports `skip_multi_version`; use **route** sub-skill instead.
- **Top skill default:** `/skills-auditor` plans dedup. It adds `--apply` only after explicit
  authorization or `SKILLS_AUDITOR_APPLY=1`; `SKILLS_AUDITOR_DRY_RUN=1` always suppresses apply.

## Commands

```bash
# Plan only
skills-audit dedup --skills-dir "$HOME/.cursor/skills"

# Explicit apply
skills-audit dedup --skills-dir "$HOME/.cursor/skills" --skills-dir "$HOME/.claude/skills" --apply
```

## Ledger behavior

- Mode: dry-run is read-only; `--apply` replaces duplicate files with symlinks.
- Suggested rows: `skill-run` for the dedup cycle and `artifact` rows for any generated report or captured plan output.
- Applied symlink replacements should be recorded as `artifact` or `external-resource` rows with `status=completed` and locators pointing to the affected skill paths.
- `skip_multi_version` and other unresolved duplicate cases should be recorded as `blocked` or `handoff` with the next owner.

## Parent

[`../../SKILL.md`](../../SKILL.md) · Related: [`../route/SKILL.md`](../route/SKILL.md).
