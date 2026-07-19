# Skill contract and API reference

The root [`SKILL.md`](../SKILL.md) is the plan-first agent entry. The CLI and the agent skill now
share one write rule: changing a skill definition or install root requires explicit apply
authorization.

## Agent entry behavior

A generic `/skills-auditor` invocation runs the maintenance pipeline in plan mode:

1. Metadata repair preview.
2. Install-root audit.
3. Hash-aware dedup preview.
4. Platform route preview and trace capture.
5. Trace QA.
6. Optional sync preview.
7. Closing audit.

The skill passes `--apply` only when the operator explicitly asks to apply or sets:

```bash
export SKILLS_AUDITOR_APPLY=1
```

`SKILLS_AUDITOR_DRY_RUN=1` remains a compatibility override and always suppresses apply. Delete
strategy always requires explicit operator wording even when apply is enabled.

## Host integration

For canonical-source promotion into Cursor, Claude Code, Codex, or a custom root, the agent uses:

```bash
skills-audit integrate --source <root> --target <environment>
skills-audit apply <reviewed-plan.json>
skills-audit verify <receipt.json>
```

The apply step is never inferred from a generic integration request. See
[integration-contract.md](integration-contract.md) for schemas, preconditions, and exit codes.

## Sub-skills

Layered maintenance instructions live under [`skills/`](../skills/README.md):

- `discover`: inspect roots and optional Git drift.
- `dedup`: fold identical hashes.
- `route`: select platform variants and preserve traces.
- `traces`: validate route state transitions.
- `sync`: maintain legacy map/discovery workflows.
- `close`: confirm the end state.

## CLI entries

```bash
skills-audit --help
python3 -m skills_auditor --help
python3 scripts/skills_audit.py --help
```

## Execution ledger

Ledgers are an optional compatibility layer for delegated maintenance. They do not replace
integration plans, receipts, route traces, or sensor logs.

- Schema: `skills-auditor-ledger/v1`.
- Default path: `.skills-auditor-local/ledgers/<run-id>.json`.
- Resource classes: `skill-run`, `subagent-run`, `trace`, `artifact`, `external-resource`.
- Statuses: `active`, `completed`, `preserved`, `handoff`, `blocked`, `failed`.

Record plan and receipt files as `artifact` resources by locator. Record route state-machine files
as `trace` resources. `ledger-check` updates the ledger's `checks` block.

## Configuration references

- [`config/skills-auditor.integration.example.json`](../config/skills-auditor.integration.example.json)
- [`config/skills-auditor.pipeline.example.env`](../config/skills-auditor.pipeline.example.env)
- [`config/discovery-profile.multisource.example.json`](../config/discovery-profile.multisource.example.json)
- [`config/discovery-profile.gstack-multiplatform.example.json`](../config/discovery-profile.gstack-multiplatform.example.json)
- [`config/sources.example.json`](../config/sources.example.json)
