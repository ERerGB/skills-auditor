---
name: skills-auditor-traces
description: >
  Skills Auditor cycle 4 — skills-audit audit-state-machine over routing traces under
  ~/.skills-auditor/traces/. Sub-skill of skills-auditor.
---

# Check routing trace consistency

Run `skills-audit skill-trace check`; disabled capture is normal. Report an enabled capture gap once and continue. Do not change capture or hook trust incidentally.

Run `skills-audit audit-state-machine --trace-dir DIR` on the actual trace directory from the route step; omitting the flag uses `~/.skills-auditor/traces/`.

Inspect transition/state findings before relying on the route result. This command is not a complete trace-schema validator: a valid JSON object without routing records can exit successfully. Confirm the input actually contains the intended route's records and identity; an exit code or analyzed-file count alone is insufficient. Warnings such as unused states in a small sample are not automatically failures. A missing/empty trace set cannot prove a route was executed successfully.

This command is read-only and audits route state-machine evidence. It does not measure model quality, semantic Skill use, native client loading or Skill Trace hook health. Report concrete errors and missing evidence; preserve logs unless retention cleanup is separately authorized. [Parent](../../SKILL.md).
