---
name: skills-auditor-close
description: >
  Skills Auditor cycle 6 — repeat discover audit (prefer --with-drift) to confirm end state after
  dedup/route/sync. Sub-skill of skills-auditor.
---

# Verify the maintenance result

Run `skills-audit skill-trace check`; disabled capture is normal. Report an enabled capture gap once and continue. Do not change capture or hook trust incidentally.

Repeat `skills-audit audit --skills-dir ROOT` over the same roots inspected at the start. Add `--with-drift` only when remote source alignment is part of the task; it can fetch Git remotes.

Check the expectations relevant to the performed work: metadata, duplicate/routing decisions, selected source revisions, installed bytes and any sync/transaction receipts. Verify native discovery separately where installation changed. An unchanged filesystem audit does not prove successful model or device execution.

For delegated runs, inspect `ledger-check` and resolve or hand off active, failed and unexplained blocked resources. Report incomplete checks explicitly. Retain plans, traces, backups and local logs; closing a run does not authorize deleting them. [Parent](../../SKILL.md).
