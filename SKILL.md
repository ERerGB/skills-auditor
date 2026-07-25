---
name: skills-auditor
description: >
  Chat-native, plan-first governance for auditing and bringing the same AI skills to local
  workspaces. The agent chooses the integration or maintenance path from the operator's goal,
  presents a reviewable plan, waits for explicit approval, applies only the reviewed scope, and
  verifies the result.
---

# Skills Auditor

> Bring the same AI skills to every workspace.

Ask once. Review the plan. Approve the change. Get a verified result.

## Interaction contract

Treat `/skills-auditor` as the product entry point, not as a request for the
operator to select CLI primitives. Translate the operator's goal into the
smallest safe workflow:

| Operator goal | Preferred path |
| --- | --- |
| “Bring these skills to this workspace” | Integrate canonical sources into the current host |
| “Audit the skills in this workspace” | Inspect active roots and plan the relevant maintenance |
| “Fix metadata, duplicates, or variants” | Run only the matching maintenance cycle |
| “Sync this authoritative mapping” | Run mapped sync against the named map |

Do not lead with cycle names, flags, plan IDs, or schemas. First explain the
observed state and intended result in the operator's language; show commands and
artifacts as review evidence.

Use one visible lifecycle:

1. **Inspect** — resolve the current workspace, host, canonical sources, and
   host-visible skill roots. State material assumptions.
2. **Plan** — run the appropriate plan-only command and summarize what is
   already aligned, what would change, and what would remain untouched.
3. **Approve** — pause before mutation. Make the target scope, archival behavior,
   network side effects, and any deletion explicit. A generic apply request never
   authorizes deletion.
4. **Apply** — after explicit approval, execute the reviewed plan or exact
   maintenance scope. Do not broaden the target or rediscover a high-level
   integration plan.
5. **Verify** — verify automatically after apply and return the receipt,
   resulting topology, unresolved findings, and a concise outcome.

If the plan contains no actions, report the zero-change plan as already aligned
and stop. Do not manufacture apply, receipt, or verification steps.

## Default behavior

When the operator invokes `/skills-auditor` without a narrower request:

1. Load `SKILLS_AUDITOR_CONFIG` when it points to an environment file.
2. Infer integration or maintenance intent from the request and current
   workspace. Prefer the high-level `integrate` transaction for adoption.
3. Use the requested cycle, or the full maintenance pipeline only for a broad
   audit request when `SKILLS_AUDITOR_MODE` is unset or `full`.
4. Keep definition and install-root changes in plan mode by default.
5. Pass `--apply` only when the operator explicitly asks to apply changes or
   `SKILLS_AUDITOR_APPLY=1`.
6. `SKILLS_AUDITOR_DRY_RUN=1` is a compatibility override that always suppresses apply.

`route` dry-runs still write trace evidence. `audit --with-drift` still fetches Git remotes.
Disclose those side effects before running them. Neither changes a skill
definition or install root.

## Choose one path

### Integrate canonical sources into hosts

Prefer the high-level transaction for adoption and host registration. Infer the
current project host when it is unambiguous; otherwise present the resolved
choices in user-facing names before asking for a target.

```bash
skills-audit integrate \
  --source .agents/skills \
  --target codex
```

The command exits `0`, writes a `skills-auditor-plan/v1` file under
`.skills-auditor-local/plans/`, and prints the exact apply command. It does not change the target.

Present the plan as a short operator summary before showing the artifact:

- source skills being adopted;
- destination workspace and host;
- entries that will link, archive, or remain unchanged;
- explicit exclusions and safety constraints;
- exact approval phrase or apply command.

Only after an explicit apply request:

```bash
skills-audit apply .skills-auditor-local/plans/<plan-id>.json
skills-audit verify .skills-auditor-local/receipts/<receipt-id>.json
```

`apply` validates full source-tree hashes and target-entry snapshots from the reviewed plan. It never
rediscovers sources. A stale plan exits `3` without starting the apply.

Run `verify` as part of the approved interaction. Do not require a second
operator request to verify the outcome.

Use `--target cursor`, `--target claude-code`, or `--target codex` for project roots. Append
`@global` for the corresponding home root. Use `--target-root NAME=PATH` for an unregistered host.

An optional repository `skills-auditor.json` can replace repeated source and target flags. Its
contract is documented in [`docs/integration-contract.md`](docs/integration-contract.md).

### Maintain existing install roots

Use the primitive cycles when the request is specifically about metadata, duplicate identities,
platform variants, traces, or an existing source map. These are implementation
tools behind the chat interaction, not prerequisites the operator must learn.

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

For the operator-facing response, summarize evidence in this order:

1. outcome;
2. changed, unchanged, and unresolved counts;
3. safety-relevant archives or skips;
4. receipt and verification locations;
5. the next action only when one remains.

Do not make raw ledger or receipt payloads the primary explanation.

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
