# Skill contract and API reference

The root [`SKILL.md`](../SKILL.md) is the plan-first agent entry. The CLI and the agent skill now
share one write rule: changing a skill definition or install root requires explicit apply
authorization.

## Agent entry behavior

A generic `/skills-auditor` invocation chooses integration or maintenance from the operator's
intent. A broad maintenance audit uses the following plan-first pipeline:

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

## Managed versions and recovery

For stable active bytes, durable invalidation or recovery of managed installations, use
`skills-audit lifecycle` and the [managed contract](managed-lifecycle.md). Candidate edits do
not alter an active snapshot. Each installation/version change requires the exact saved plan ID after review;
the maintenance `SKILLS_AUDITOR_APPLY` default does not substitute for that approval.

The programmatic core is `skills_auditor.lifecycle.engine.Manager(project_root)`:
`plan`, `apply`, `verify`, `recover`, `get_installation`, `list_installations`, `get_skill`
and `inspect_transaction`. Use `create=False` for readers that must not initialize state,
and close `manager.repository` when finished. Prefer CLI JSON for untrusted saved input:
its loader rejects duplicate keys, nonregular files and oversized payloads before execution.

`skills_auditor.lifecycle.status.read_status` is a pure cached read;
`preflight(..., refresh=True)` observes current integrity and returns proceed/warn/block.
Local verification records and authorization commit before derived status publication,
so failure to publish a status projection cannot erase observed denial. The host is
responsible for actually invoking preflight and respecting its result.

Additional local APIs share that manager and repository:

| Module | Entry points and boundary |
| --- | --- |
| `lifecycle.batch.BatchManager` | `plan`, `apply`, `inspect`, `recover`, `plan_compensation`; explicit parent approval, deterministic children and separately approved inverse plans |
| `lifecycle.incidents` | `list_incidents`, `get_incident`, `investigate`, `append_note`, `resolve`, `supersede`; bounded historical evidence and explicit analysis/disposition, never implicit reauthorization |
| `lifecycle.invocation` | `select`, `plan_override`, `apply_override`, `get_override`, `revoke_override`; current approved snapshot selection, bounded explicit staleness exceptions and durable audit |
| `lifecycle.retention` | `plan_retention`, `apply_retention`, `recover_retention`; separately approved evidence expiry, reference-aware quarantine/restore and explicit irreversible purge |

Observation and investigation can append evidence; they are not silent installation changes.
Operator-authored notes and dispositions must be explicitly requested, stay bounded and preserve
prior events. No API treats local actor/tool labels as authenticated identity. An invocation
decision supplies a path only when it permits use and never executes a Skill itself.

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
integration plans, receipts, managed transaction journals, route traces, or sensor logs.

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
