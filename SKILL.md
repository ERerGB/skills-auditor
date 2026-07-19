---
name: skills-auditor
description: >
  Plan-first entry for auditing and integrating local AI skill roots. Generic invocations never
  change skill definitions or install roots unless the operator explicitly requests apply or sets
  SKILLS_AUDITOR_APPLY=1. Uses versioned plans and receipts for host integration.
---

# Skills Auditor

> Inspect first. Apply only the state the operator reviewed.

## Default behavior

When the operator invokes `/skills-auditor` without a narrower request:

1. Load `SKILLS_AUDITOR_CONFIG` when it points to an environment file.
2. Run the requested cycle, or the full maintenance pipeline when
   `SKILLS_AUDITOR_MODE` is unset or `full`.
3. Keep definition and install-root changes in plan mode by default.
4. Pass `--apply` only when the operator explicitly asks to apply changes or
   `SKILLS_AUDITOR_APPLY=1`.
5. `SKILLS_AUDITOR_DRY_RUN=1` is a compatibility override that always suppresses apply.

`route` dry-runs still write trace evidence. `audit --with-drift` still fetches Git remotes.
Neither side effect changes a skill definition or install root.

## Choose one path

### Integrate canonical sources into hosts

Prefer the high-level transaction for adoption and host registration:

```bash
skills-audit integrate \
  --source .agents/skills \
  --target codex
```

The command exits `0`, writes a `skills-auditor-plan/v1` file under
`.skills-auditor-local/plans/`, and prints the exact apply command. It does not change the target.

Only after an explicit apply request:

```bash
skills-audit apply .skills-auditor-local/plans/<plan-id>.json
skills-audit verify .skills-auditor-local/receipts/<receipt-id>.json
```

`apply` validates full source-tree hashes and target-entry snapshots from the reviewed plan. It never
rediscovers sources. A stale plan exits `3` without starting the apply.

Use `--target cursor`, `--target claude-code`, or `--target codex` for project roots. Append
`@global` for the corresponding home root. Use `--target-root NAME=PATH` for an unregistered host.

An optional repository `skills-auditor.json` can replace repeated source and target flags. Its
contract is documented in [`docs/integration-contract.md`](docs/integration-contract.md).

### Maintain existing install roots

Use the primitive cycles when the request is specifically about metadata, duplicate identities,
platform variants, traces, or an existing source map.

| Cycle | Plan command | Apply behavior |
| --- | --- | --- |
| Metadata repair | `skills-audit metadata-repair --platform codex --skills-dir <root>` | Add `--apply` only when explicitly authorized |
| Discover | `skills-audit audit --skills-dir <root>` | Read-only; add `--with-drift` only when remote fetch is intended |
| Dedup | `skills-audit dedup --skills-dir <root>` | Apply relinks only identical hashes |
| Route | `skills-audit route --platform <host> --skills-dir <root> --strategy archive` | Apply archives superseded variants by default |
| Trace QA | `skills-audit audit-state-machine` | Read-only over route traces |
| Mapped sync | `skills-audit sync --skills-dir <root> --map-file <file>` | Apply the reviewed mapping plan |
| Close | repeat `audit` | Confirm final topology and metadata |

The full maintenance order is:

```text
metadata-repair → audit → dedup → route → trace QA → optional sync → closing audit
```

Do not use `--strategy delete` unless the operator explicitly requested deletion. An apply request
does not imply permission to delete.

## Scope rules

- Each `--skills-dir` is one host-visible install root. Duplicate-name checks recurse through the
  whole root, including sibling packs.
- Codex metadata requires fenced frontmatter with non-empty `name` and `description`.
- `metadata-repair` handles only safe, idempotent repairs. Unsupported malformed frontmatter is
  skipped and exits `5`.
- `dedup` relinks same-hash definitions. Different hashes remain separate and move to `route`.
- Host integration creates symlinks. An existing native entry is timestamp-archived before
  linking; a matching link becomes `noop`.
- Syntax, provenance, and lifecycle evidence do not prove semantic skill use or runtime behavior.

## Configuration

Template: [`config/skills-auditor.pipeline.example.env`](config/skills-auditor.pipeline.example.env).

| Variable | Purpose |
| --- | --- |
| `SKILLS_AUDITOR_ROOTS` | Space-separated maintenance roots |
| `SKILLS_AUDITOR_EXTRA_ROOTS` | Additional maintenance roots |
| `SKILLS_AUDITOR_MODE` | `full`, `metadata-repair`, `discover`, `dedup`, `route`, `traces`, `sync`, or `close` |
| `SKILLS_AUDITOR_APPLY` | `1` permits apply for the requested maintenance run |
| `SKILLS_AUDITOR_DRY_RUN` | `1` forces plan-only behavior; compatibility override |
| `SKILLS_AUDITOR_WITH_DRIFT` | `1` enables remote-backed drift checks |
| `SKILLS_AUDITOR_ROUTE_PLATFORMS` | Comma-separated route platforms |
| `SKILLS_AUDITOR_ROUTE_STRATEGY` | `archive` or `keep`; use `delete` only with explicit operator wording |
| `SKILLS_AUDITOR_SYNC_MAP_FILE` | Optional authoritative map for the sync cycle |
| `SKILLS_AUDITOR_CONFIG` | Environment file to source before resolving the above |

Default maintenance roots, when no configuration is supplied, are `~/.cursor/skills` and
`~/.claude/skills`.

## Sub-skill system

| Cycle | Instructions |
| --- | --- |
| Discover | [`skills/discover/SKILL.md`](skills/discover/SKILL.md) |
| Dedup | [`skills/dedup/SKILL.md`](skills/dedup/SKILL.md) |
| Route | [`skills/route/SKILL.md`](skills/route/SKILL.md) |
| Trace QA | [`skills/traces/SKILL.md`](skills/traces/SKILL.md) |
| Sync | [`skills/sync/SKILL.md`](skills/sync/SKILL.md) |
| Close | [`skills/close/SKILL.md`](skills/close/SKILL.md) |

Index: [`skills/README.md`](skills/README.md).

## Evidence and handoff

Use execution ledgers for multi-step or delegated maintenance runs:

```bash
skills-audit ledger-create --run-id <run-id> --source <orchestrator> --mode dry-run
skills-audit ledger-upsert --run-id <run-id> \
  --id <resource-id> --class skill-run --locator <command-or-path> \
  --owner <owner> --status completed
skills-audit ledger-check --run-id <run-id>
skills-audit ledger-summary --run-id <run-id>
```

Keep route traces as `trace` resources and plan/receipt files as `artifact` resources. Do not copy
their payloads into the ledger.

## Isolation and safety

- Plan-only work can run in read-only isolation, except that route may write traces and integrate
  writes a plan under `.skills-auditor-local/`.
- Apply work needs write access to the exact source or target roots in scope.
- Resolve every target before applying. Never use a home directory, repository root, or unresolved
  variable as a destructive target.
- A failed high-level apply writes a failed receipt with completed actions and the error when the
  filesystem permits it.

## Install

From the repository:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
skills-audit --version
```

Use `python -m pip install -e .` only when developing skills-auditor itself. The no-install entry
is `python3 scripts/skills_audit.py`.
