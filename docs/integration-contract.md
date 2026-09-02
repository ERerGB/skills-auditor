# Integration contract

The high-level interface has one state transition:

```text
Integration Spec → immutable Plan → Apply → Receipt → Verify
```

It exists so an operator can review the exact filesystem actions that will run. The advanced
`sync` and `sync-discover` commands remain available as lower-level primitives.

## Core invariant

`skills-audit apply` never discovers sources or rebuilds actions. It accepts one
`skills-auditor-plan/v1` file and verifies:

- the plan content still matches its `plan_id`;
- every recorded source tree still has the reviewed SHA-256 hash;
- every affected target entry still has the reviewed missing, file, directory, or symlink state;
- every reserved archive destination is still missing;
- every action points to the source record with the same alias and tree hash.

If any precondition changed, apply exits `3` before starting filesystem writes. Generate and review
a new plan.

## Integration spec

Repository configuration is optional. When `skills-auditor.json` exists in the current directory,
`integrate` loads it automatically.

```json
{
  "schema_version": "skills-auditor-integration/v1",
  "project_root": ".",
  "sources": [".agents/skills"],
  "targets": ["cursor", "claude-code", "codex"],
  "metadata_platform": "codex"
}
```

Paths in the config resolve relative to `project_root`, which itself resolves relative to the
config file. CLI `--source` and `--target` lists replace the corresponding config lists.

Targets accept these forms:

| Form | Meaning |
| --- | --- |
| `codex` | Built-in project root `.codex/skills` |
| `codex@global` | Built-in global root `~/.codex/skills` |
| `--target-root acme=.acme/skills` | Explicit custom environment root |
| `{"environment":"acme","root":".acme/skills"}` | Custom root in the JSON spec |

Built-in environment names are `cursor`, `claude-code`, and `codex`. High-level integration requires
source and target roots to be disjoint and rejects target roots that contain each other. Use a
precise canonical source tree rather than asking the tool to install within a source or source from
within an install namespace.

Template: [`../config/skills-auditor.integration.example.json`](../config/skills-auditor.integration.example.json).

## Plan

Create a plan directly:

```bash
skills-audit integrate --source .agents/skills --target codex
```

Or use repository configuration:

```bash
skills-audit integrate
```

The command exits `0` whether the plan contains changes or only `noop` actions. The plan is written
atomically to `.skills-auditor-local/plans/<plan-id>.json` unless `--plan-out` is supplied.

`plan_id` is a content checksum over the entire plan except `plan_id` itself and the output-only
`plan_path`. A source snapshot covers directory structure, regular-file bytes and permission bits,
and literal symlink targets. Source symlinks that resolve outside their skill tree are rejected,
so the hash never claims to cover unbounded external content. The checksum detects accidental or
stale edits; it is not a signature or authorization token.

## Apply and receipt

Apply the exact path printed by `integrate`:

```bash
skills-audit apply .skills-auditor-local/plans/<plan-id>.json
```

On success, apply writes `skills-auditor-receipt/v1` atomically under
`.skills-auditor-local/receipts/`. For an existing native entry, the plan records one exact,
currently missing `<name>.archived-<timestamp>` destination before apply renames and links it.

All preconditions across all roots are checked before the first write and again immediately before
each action. Replacement and archive/link actions attempt a local rollback when their link creation
fails. Separate filesystem operations cannot be globally atomic across roots. If an I/O failure
occurs mid-run, apply writes a failed receipt containing completed actions and the error when the
filesystem still permits it. If receipt storage itself fails, the command reports that explicitly;
audit the listed target roots before retrying.

## Verify

Verify the receipt after apply or in CI:

```bash
skills-audit verify .skills-auditor-local/receipts/<receipt-id>.json
```

Verification checks every receipt entry still resolves to the expected source and every complete
source tree still has the applied hash. It does not reject unrelated extra entries in a target
root. The output also includes an `approval` object. A clean, completed receipt reports `valid`
with `requires_reapproval: false`; a failed receipt, target-link drift, or source-tree drift reports
`invalidated`, sets `requires_reapproval: true`, and lists stable, deduplicated failed-check codes.

Approval is bound to the exact reviewed version and target state, not permanently to a Skill name.
After invalidation, generate and review a new plan for the current state, then explicitly approve
its apply. Verification does not prove runtime behavior or semantic skill safety. Installed entries
remain live symlinks, so verification can detect a source change but cannot prevent a host from
consuming it between verification runs.

## Machine interface

Each high-level command accepts `--format json`. After successful argument parsing, JSON mode writes
exactly one JSON object to stdout and keeps execution diagnostics inside that object; human
diagnostics use stderr. Parser usage errors retain standard `argparse` stderr output and exit `2`.

| Exit | Meaning for `integrate`, `apply`, and `verify` |
| --- | --- |
| `0` | Command completed; plan may contain changes or `noop` actions |
| `2` | Invalid arguments, config, schema, path, or environment name |
| `3` | Contract failure: conflict, stale plan, failed apply verification, or failed receipt verification |

Versioned JSON Schemas ship with the package:

New verification output always includes `approval`. The v1 verification schema keeps that property
optional so verification documents generated by earlier v1 releases remain valid.

- [`integration-spec-v1.schema.json`](../skills_auditor/schemas/integration-spec-v1.schema.json)
- [`integration-plan-v1.schema.json`](../skills_auditor/schemas/integration-plan-v1.schema.json)
- [`integration-receipt-v1.schema.json`](../skills_auditor/schemas/integration-receipt-v1.schema.json)
- [`integration-verification-v1.schema.json`](../skills_auditor/schemas/integration-verification-v1.schema.json)
- [`error-v1.schema.json`](../skills_auditor/schemas/error-v1.schema.json)
