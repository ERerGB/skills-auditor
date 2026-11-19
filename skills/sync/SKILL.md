---
name: skills-auditor-sync
description: >
  Skills Auditor cycle 5 (optional) — skills-audit sync with --map-file or sync-discover
  to plan or apply relinking to canonical skill sources. Sub-skill of skills-auditor.
---

# Sync readable canonical Skill sources

Run `skills-audit skill-trace check`; disabled capture is normal. Report an enabled capture gap once and continue. Do not change capture or hook trust incidentally.

For a maintained map, run `skills-audit sync --skills-dir ROOT --map-file FILE`; repeat roots as needed. The explicit map is authoritative. Inspect sources and current target owners before relinking. Plan by default; `--apply` requires the concrete authorized scope, and `SKILLS_AUDITOR_DRY_RUN=1` suppresses changes.

For discovery-based replication, use `sync-discover --source SOURCE_ROOT --skills-dir INSTALL_ROOT`, repeating either flag as needed. Dry-run exits 1 for missing/stale entries, 5 for skipped unsafe actions, and 0 only when all discovered entries are current. Apply can create a missing install root. Slash names become top-level aliases; target roots are excluded from their own discovery so canonical directories are not replaced by self-links.

For first adoption, prefer the parent's `integrate` → exact `apply PLAN` → `verify RECEIPT`, which consumes reviewed bytes rather than rediscovering. Existing managed identities use their established lifecycle. Across filesystems/cloud hosts, follow [locked delivery](../../docs/vendir-integration.md); do not transport absolute symlinks or old approvals.

In the legacy full pipeline, absent an explicit map, repository `.agents/.cursor/.claude` sources can register into `.codex/skills` when the repository has `.codex/`. Resolve actual scope first; override with `SKILLS_AUDITOR_DISCOVERY_SOURCES` and `SKILLS_AUDITOR_INSTALL_ROOTS` when needed.

Verify links and source identity, then native host discovery separately. Record skipped/unsafe items without claiming completion. [Parent](../../SKILL.md).
