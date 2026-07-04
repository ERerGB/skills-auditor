# Examples and recipes

Use these command shapes as starting points. Keep them in dry-run mode until the output is boring and expected.

## Inspect one install root

```bash
skills-audit audit --skills-dir "$HOME/.cursor/skills"
```

## Inspect Cursor and Claude Code together

```bash
skills-audit audit \
  --skills-dir "$HOME/.cursor/skills" \
  --skills-dir "$HOME/.claude/skills"
```

## Fail on duplicate skill names

```bash
skills-audit audit \
  --skills-dir "$HOME/.claude/skills" \
  --fail-on-duplicate-names
```

## Preview mapped sync

```bash
skills-audit sync \
  --skills-dir "$HOME/.cursor/skills" \
  --map-file config/sources.example.json
```

Add `--apply` only after reviewing the dry-run plan.

## Preview discovery-driven sync

```bash
skills-audit sync-discover \
  --source .agents/skills \
  --source .cursor/skills \
  --skills-dir .codex/skills
```

## Use a discovery profile

```bash
skills-audit audit-discovery \
  --profile-file config/discovery-profile.gstack-multiplatform.example.json
```

Discovery profiles let the management layer describe which source roots apply to Cursor, Claude Code, or all targets.

## Repair metadata in dry-run mode

```bash
skills-audit metadata-repair \
  --platform codex \
  --skills-dir "$HOME/.codex/skills"
```

Run again with `--apply` only after reading the proposed edits.

## Record a delegated run ledger

```bash
RUN_ID="skills-auditor-$(date -u +%Y%m%dT%H%M%SZ)"

skills-audit ledger-create \
  --run-id "$RUN_ID" \
  --source issue-lifecycle-router \
  --mode apply

skills-audit ledger-upsert \
  --run-id "$RUN_ID" \
  --id route-codex \
  --class skill-run \
  --locator "skills-audit route --platform codex --strategy archive --apply" \
  --owner skills-auditor-route \
  --status completed

skills-audit ledger-upsert \
  --run-id "$RUN_ID" \
  --id route-trace-codex \
  --class trace \
  --locator "$HOME/.skills-auditor/traces/<trace-id>.json" \
  --owner skills-auditor-route \
  --status preserved

skills-audit ledger-check --run-id "$RUN_ID"
skills-audit ledger-summary --run-id "$RUN_ID"
```

Use `--status handoff --handoff-target <target>` when another operator or agent must continue. Use `--status blocked --blocked-reason <reason>` when the run cannot close.
