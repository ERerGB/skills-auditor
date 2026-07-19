---
name: skills-auditor-route
description: >
  Skills Auditor cycle 3 — plan-first Select-One routing per platform. Writes JSON traces under
  ~/.skills-auditor/traces/ and applies only with explicit authorization.
---

# Skills Auditor — Route (cycle 3)

## When to use

- Duplicate `name:` with **different** content (multi-version) inside a bundle (e.g. gstack).
- Scoped: “route Codex”, `SKILLS_AUDITOR_MODE=route`.

## Model

```
DISCOVERED → CLASSIFIED → ROUTED → RESOLVED
```

Phases: hash variants → infer platform from path (e.g. `.agents/` → codex) → pick one identity → archive/delete/keep the rest.

## Strategies

| Strategy | Superseded files |
|----------|------------------|
| `archive` (default) | `SKILL.md.archived-<timestamp>` |
| `delete` | removed |
| `keep` | unchanged (audit-only) |

## Commands

```bash
skills-audit route --platform cursor --skills-dir "$HOME/.cursor/skills" --strategy archive

skills-audit route --platform codex --skills-dir "$HOME/.claude/skills" --strategy archive

skills-audit route \
  --platform cursor \
  --skills-dir "$HOME/.cursor/skills" \
  --strategy archive \
  --apply

skills-audit route \
  --platform codex \
  --skills-dir "$HOME/.claude/skills" \
  --trace-dir ./my-traces
```

## Ledger behavior

- Mode: dry-run writes route trace JSON but does not change skill files; `--apply` may archive, delete, or keep superseded variants based on `--strategy`.
- Record the route cycle as a `skill-run` row. While work is open use `status=active`; after trace audit and any apply step, move it to `completed`, `handoff`, `blocked`, or `failed`.
- Record the emitted state-machine trace as a `trace` row with `status=preserved` and `locator` set to the trace file path.
- Applied archives/deletes should be recorded as `artifact` or `external-resource` rows with locators for the affected `SKILL.md` or archived path. Do not remove the route trace when cleaning up; the ledger points to it.
- If routing cannot choose one active variant, mark the relevant row `blocked` with `--blocked-reason` or `handoff` with `--handoff-target`.

## Parent

[`../../SKILL.md`](../../SKILL.md) · Next: [`../traces/SKILL.md`](../traces/SKILL.md).
