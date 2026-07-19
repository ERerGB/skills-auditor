# Examples and recipes

Use the high-level transaction for host integration. The primitive commands below it are for
diagnosis and maintenance.

## Integrate one source into one host

```bash
skills-audit integrate --source .agents/skills --target codex
```

The output contains the saved plan path and exact apply command.

## Integrate from repository configuration

```bash
cp config/skills-auditor.integration.example.json skills-auditor.json
skills-audit integrate
```

## Target global and custom roots

```bash
skills-audit integrate \
  --source .agents/skills \
  --target cursor@global \
  --target-root acme="$HOME/.acme/skills"
```

## Machine-readable plan

```bash
skills-audit integrate \
  --source .agents/skills \
  --target codex \
  --format json \
  --plan-out .skills-auditor-local/plan.json
```

JSON mode returns `0` even when changes are planned. Inspect `summary.changes` instead of treating
ordinary drift as a process failure.

## Inspect one install root

```bash
skills-audit audit --skills-dir "$HOME/.cursor/skills"
```

## Inspect multiple install roots

```bash
skills-audit audit \
  --skills-dir "$HOME/.cursor/skills" \
  --skills-dir "$HOME/.claude/skills" \
  --fail-on-duplicate-names
```

## Repair metadata

```bash
skills-audit metadata-repair \
  --platform codex \
  --skills-dir "$HOME/.codex/skills"
```

Review the plan, then repeat with `--apply` only when that direct source edit is intended.

## Audit discovery policy

```bash
skills-audit audit-discovery \
  --profile-file config/discovery-profile.gstack-multiplatform.example.json \
  --fail-on-conflict \
  --fail-on-hash-conflict
```

## Legacy mapped sync

```bash
skills-audit sync \
  --skills-dir "$HOME/.cursor/skills" \
  --map-file config/sources.example.json
```

Mapped sync remains useful for an existing curated map. New integrations should prefer
`integrate` so apply consumes a versioned plan.

## Record a delegated run ledger

```bash
skills-audit ledger-create --run-id run-1 --source orchestrator --mode dry-run

skills-audit ledger-upsert \
  --run-id run-1 \
  --id integration-plan \
  --class artifact \
  --locator .skills-auditor-local/plans/<plan-id>.json \
  --owner skills-auditor \
  --status preserved

skills-audit ledger-check --run-id run-1
skills-audit ledger-summary --run-id run-1
```
