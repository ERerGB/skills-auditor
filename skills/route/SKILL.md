---
name: skills-auditor-route
description: >
  Skills Auditor cycle 3 — plan-first Select-One routing per platform. Writes JSON traces under
  ~/.skills-auditor/traces/ and applies only with explicit authorization.
---

# Route differing Skill variants

Run `skills-audit skill-trace check`; disabled capture is normal. Report an enabled capture gap once and continue. Do not change capture or hook trust incidentally.

Use `skills-audit route --platform HOST --skills-dir ROOT --strategy archive` to plan selection among same-name variants. Scope to the actual host and inspect its inferred classification before application. The route model is DISCOVERED → CLASSIFIED → ROUTED → RESOLVED.

Dry-run writes JSON traces (default `~/.skills-auditor/traces/`); pass `--trace-dir DIR` for an isolated record. It does not change Skill bodies. `archive` retains superseded entries as `SKILL.md.archived-TIMESTAMP`; `keep` retains them in place. `delete` is destructive and requires explicit deletion authorization, not merely general apply consent.

Apply only a reviewed, authorized scope with `--apply`; do not bypass an existing managed owner. Inspect partial results after any failure. Run [trace QA](../traces/SKILL.md) and check actual host discovery before claiming one active route. Preserve trace files as evidence; unresolved selection stays unresolved. [Parent](../../SKILL.md).
