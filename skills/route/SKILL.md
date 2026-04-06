---
name: skills-auditor-route
description: >
  Skills Auditor cycle 3 — Select-One routing per platform; top entry defaults to --apply unless
  dry-run. Writes JSON traces under ~/.skills-auditor/traces/. Sub-skill of skills-auditor.
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

## Parent

[`../../SKILL.md`](../../SKILL.md) · Next: [`../traces/SKILL.md`](../traces/SKILL.md).
