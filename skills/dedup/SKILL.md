---
name: skills-auditor-dedup
description: >
  Skills Auditor cycle 2 — plan-first hash-aware deduplication. Applies only when the operator
  explicitly authorizes writes. Sub-skill of skills-auditor.
---

# Deduplicate identical Skill entries

Run `skills-audit skill-trace check`; disabled capture is normal. Report an enabled capture gap once and continue. Do not change capture or hook trust incidentally.

Run `skills-audit dedup --skills-dir ROOT` on the actual requested install root; repeat roots if needed. Inspect the complete recursive scope, including sibling packages. Confirm duplicate identity and identical content hashes before relinking.

Different hashes produce `skip_multi_version`; they need [routing](../route/SKILL.md), not content destruction. Plan by default. Add `--apply` only for the concrete scope already authorized; `SKILLS_AUDITOR_DRY_RUN=1` suppresses it. Existing managed installations use their lifecycle and must not be bypassed by this primitive.

After apply, verify source/target identity and native discovery separately: a symlink that the filesystem follows may still be skipped by a host scanner. Preserve unrelated entries and report unresolved variants. [Parent](../../SKILL.md).
