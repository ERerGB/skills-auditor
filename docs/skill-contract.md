# Skill contract and API reference

The root [`SKILL.md`](../SKILL.md) is the agent entry point for the skill pack.

## Entry behavior

The top-level skill runs the full pipeline:

1. Discover skill folders.
2. Deduplicate names and sources.
3. Route work to the right sub-skill.
4. Sync when the operator approves apply-mode behavior.
5. Review trace output.
6. Close with a concise report.

By default, the top-level skill can use apply-mode for dedup, route, and sync. Force dry-run mode with:

```bash
export SKILLS_AUDITOR_DRY_RUN=1
```

## Sub-skills

Layered sub-skills live under [`skills/`](../skills/README.md):

- `discover`: inspect available skill roots.
- `dedup`: reason about duplicate names and canonical sources.
- `route`: select the right workflow path.
- `sync`: apply or preview synchronization.
- `traces`: inspect local skill-read sensor logs.
- `close`: summarize the run.

## CLI entry

The installed console script is:

```bash
skills-audit
```

The package module entry is:

```bash
python3 -m skills_auditor
```

The no-install script entry is:

```bash
python3 scripts/skills_audit.py
```

## Skill-run ledger contract

Ledgers are an optional compatibility layer for orchestrators that need to prove a delegated run reached a clean close. They do not replace route state-machine traces or local trigger/sensor logs.

- Schema version: `skills-auditor-ledger/v1`.
- Default path: `.skills-auditor-local/ledgers/<run-id>.json`.
- Top-level fields: `schema_version`, `run`, `resources`, `checks`.
- Resource classes: `skill-run`, `subagent-run`, `trace`, `artifact`, `external-resource`.
- Statuses: `active`, `completed`, `preserved`, `handoff`, `blocked`, `failed`.

Core commands:

```bash
skills-audit ledger-create --run-id <run-id> --source <orchestrator> --mode dry-run

skills-audit ledger-upsert --run-id <run-id> \
  --id route-trace --class trace \
  --locator ~/.skills-auditor/traces/<trace>.json \
  --owner skills-auditor-route --status preserved

skills-audit ledger-check --run-id <run-id>
skills-audit ledger-summary --run-id <run-id>
```

`ledger-check` updates the `checks` block in the ledger. Active rows are warnings. Failed rows, missing handoff targets, missing blocked reasons, invalid resource classes/statuses, and invalid schema metadata are errors.

Existing artifacts should be linked by locator instead of copied into the ledger:

- Route traces stay under `~/.skills-auditor/traces/`.
- Trigger logs stay under `.skills-auditor-local/logs/`.
- Sensor logs stay under `.skills-auditor-local/sensors/`.
- Sync, archive, delete, or report outputs should be recorded as `artifact` rows.

## Configuration examples

- [`config/sources.example.json`](../config/sources.example.json)
- [`config/discovery-profile.cursor-jz.example.json`](../config/discovery-profile.cursor-jz.example.json)
- [`config/discovery-profile.gstack-fork.example.json`](../config/discovery-profile.gstack-fork.example.json)
- [`config/discovery-profile.gstack-multiplatform.example.json`](../config/discovery-profile.gstack-multiplatform.example.json)
- [`config/skills-auditor.pipeline.example.env`](../config/skills-auditor.pipeline.example.env)
