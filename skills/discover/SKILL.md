---
name: skills-auditor-discover
description: >
  Skills Auditor cycle 1 — discover filesystem state: skills-audit audit (optional --with-drift),
  drift-check, and optional audit-discovery with a profile file. Sub-skill of skills-auditor.
---

# Discover installed Skill state

Run `skills-audit skill-trace check`; disabled capture is normal. Report an enabled capture gap once and continue. Do not change capture or hook trust incidentally.

Inspect the actual requested host roots with `skills-audit audit --skills-dir ROOT` (repeat the flag for multiple roots). Resolve Codex's current `.agents/skills` and legacy `.codex/skills` routes rather than assuming a different host's defaults. The recursive scanner includes sibling packages and nested duplicate names.

Report missing/broken links, missing bodies, invalid metadata and duplicate identities. This is filesystem discovery, not proof that the native client loads or follows a Skill.

Only when source drift matters, add `--with-drift` or use `drift-check`; these can run `git fetch`. Non-Git entries are skipped for drift. For multi-source precedence/collision analysis, use `audit-discovery --profile-file FILE`; inspect the actual profile before relying on `--fail-on-conflict` or `--fail-on-hash-conflict`.

Return the roots, findings and requested next action. Do not repair or register during a read-only audit. [Parent and authorization](../../SKILL.md).
