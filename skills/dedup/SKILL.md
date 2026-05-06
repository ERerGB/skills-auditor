---
name: skills-auditor-dedup
description: >
  Skills Auditor cycle 2 — skills-audit dedup; top entry defaults to --apply unless dry-run.
  Hash-aware fold when duplicate names share content. Sub-skill of skills-auditor.
---

# Skills Auditor — Dedup (cycle 2)

## When to use

- After discover shows duplicate `name:` with **identical** content (same hash).
- Scoped: “dedup dry-run”, `SKILLS_AUDITOR_MODE=dedup`.

## Important

- Dedup scans the **entire** install root passed to `--skills-dir` (Slash-style recursive view), so it catches both in-pack mirrors and **sibling-folder** duplicates (e.g. `browse/` vs `gstack/browse/`).
- **Different hashes** → dedup reports `skip_multi_version`; use **route** sub-skill instead.
- **Top skill default:** `/skills-auditor` runs dedup **with** `--apply` unless the operator asks for dry-run or sets `SKILLS_AUDITOR_DRY_RUN=1`.

## Commands

```bash
# Plan only
skills-audit dedup --skills-dir "$HOME/.cursor/skills"

# Default /skills-auditor style (apply)
skills-audit dedup --skills-dir "$HOME/.cursor/skills" --skills-dir "$HOME/.claude/skills" --apply
```

## Parent

[`../../SKILL.md`](../../SKILL.md) · Related: [`../route/SKILL.md`](../route/SKILL.md).
