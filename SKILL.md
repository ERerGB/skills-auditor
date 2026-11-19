---
name: skills-auditor
description: >
  Chat-native, plan-first governance for auditing and delivering AI skills to local, remote
  and cloud workspaces with native vendir locks and target-local registration. The agent chooses the integration or maintenance path from the operator's goal,
  presents a reviewable plan, resolves authorization for its exact scope, applies only that scope, and
  verifies the result.
---

# Skills Auditor

Turn the operator's maintenance request into a concrete plan, execute only its authorized scope, and verify the result. Installed directories are runtime routes; canonical repositories own maintained content.

## Before every invocation: optional Skill Trace preflight

Load `SKILLS_AUDITOR_CONFIG` if explicitly configured, then run `skills-audit skill-trace check`. This independent check works even when hooks do not run. Disabled capture is normal; report an enabled-but-unverified/stale/error gap once and continue. Missing events do not prove non-use. Never enable capture or alter hook trust as an incidental preflight. See [capture controls](docs/install.md#optional-skill-trace-plugin) when the operator requests them.

## Choose the smallest applicable path

| Request | Execute / read |
|---|---|
| Adopt canonical sources into this host | `integrate` → reviewed `apply PLAN` → `verify RECEIPT`; [integration contract](docs/integration-contract.md) |
| Inspect metadata, duplication or variants | Only the requested [maintenance cycle](#maintain-existing-install-roots) |
| Manage an existing approved installation | [Managed lifecycle](docs/managed-lifecycle.md); preserve its owner and stable installation ID |
| Shared baseline with project exceptions across hosts | [Workspace management](docs/workspace-management.md); prepare/apply the exact target-local plan |
| Deliver across filesystems or restore a target | [Locked delivery](#deliver-across-filesystems-and-restore-environments) |
| Rewrite a Skill manual and learn from execution | [Bounded handbook calibration](docs/handbook-calibration.md) |
| Inspect an explicitly enabled real-use pilot | `skills-audit handbook-pilot status`; [experimental pilot](docs/handbook-pilot.md) |

For an audit, discover the current host's actual roots. Codex may expose project/user `.agents/skills` and existing `.codex/skills` aliases. Include other hosts only when in scope. A recursive filesystem listing is not native host discovery. Inspect an existing target before choosing adoption versus managed maintenance.

## Plan, authorization and verification

1. Inspect the source identity, actual target, existing owner and current bytes. A writable copy or fork does not establish ownership. Honor the operator's maintained/third-party boundary.
2. Generate a plan for the requested path. Explain its actual filesystem, archival, network and governance effects. `route` dry-run writes trace files; drift checks can fetch remotes.
3. Match that concrete plan against authorization already given. Continue when covered; resolve only missing scope. User consent and a version-bound CLI grant are separate: inspect the exact plan and provide its current ID where required. Old receipts and environment defaults cannot approve a changed managed plan. Generic apply authorization does not authorize deletion.
4. Apply that plan, then verify automatically. Do not broaden targets or rediscover another integration plan during apply. On interrupted or uncertain effects, inspect the transaction/receipt and affected targets before any retry; managed recovery follows its explicit resume/compensation protocol.
5. Report changed/unchanged/unresolved results with source, installed and native-discovery evidence separately. Local hashes do not establish real device/cloud or semantic workflow success.

An operation is already aligned only when both filesystem and governance effects are empty. Renewal, revocation and overrides can matter without file changes. Preserve unrelated work and private runtime state.

## Integrate canonical sources into hosts

```bash
skills-audit integrate --source /absolute/canonical/skills --target codex
skills-audit apply /absolute/generated-plan.json
skills-audit verify /absolute/generated-receipt.json
```

The plan command prints its exact apply command and target paths. Use the plan's actual receipt, not these example filenames. Apply verifies the planned source-tree hashes and target snapshots. Initial stale preconditions exit 3 before effects; a later stale action can follow completed earlier actions. Inspect partial receipts before resuming.

Use `--target cursor`, `claude-code`, or `codex`; `@global` selects home scope. `--target-root NAME=PATH` selects an explicit host root. Existing managed identities must use their lifecycle rather than legacy relinking. See [integration configuration](docs/integration-contract.md) for non-default layouts and outer dependencies.

## Manage approved versions

Keep candidate bytes separate from active approved snapshots. Use fresh managed/workspace plans and exact `--approve-plan-id` grants; never edit or sync behind an active snapshot, silently migrate its owner, or reuse old approval for a new version. A restored file alone does not clear revocation or invalidation. Inspect fresh preflight plus transaction history for warnings or interruption; follow [lifecycle recovery](docs/managed-lifecycle.md).

Managed integrity/grants, host hook trust and optional capture are independent. Cross-host atomicity and native discovery require their own evidence. Read [Grok host acceptance](docs/grok-environments.md) only for an explicitly selected Grok target; Cursor acceptance means Cursor CLI, not every client.

## Deliver across filesystems and restore environments

Keep native vendir configuration and lock together. Materialize a new reviewed delivery; preserve its repository layout and actual outer dependencies. Apply a fresh plan in the receiving host rather than copying absolute links, old grants or login state. Read the [vendir delivery guide](docs/vendir-integration.md) before changing delivery or recovery.

Verify through the receiving agent: native discovery, actual entrypoint/reference reads and a bounded useful operation. Keep source hashes, runtime identity and command exits. A controller-side read or catalog entry alone does not prove acceptance. Preserve independent locked inputs and source objects for recovery; a replaced machine is not automatically restored native conversation state. Network access, credentials and paid resource creation require their applicable authorization.

## Maintain existing install roots

| Scope | Plan/read command | Important effect |
|---|---|---|
| Metadata | `metadata-repair --platform codex --skills-dir ROOT` | Apply repairs safe metadata only; unsupported cases exit 5 |
| [Discover](skills/discover/SKILL.md) | `audit --skills-dir ROOT` | `--with-drift` adds remote fetch |
| [Dedup](skills/dedup/SKILL.md) | `dedup --skills-dir ROOT` | Identical hashes only; whole root including siblings |
| [Route](skills/route/SKILL.md) | `route --platform codex --skills-dir ROOT --strategy archive` | Writes traces even without apply; deletion requires explicit authorization |
| [Trace QA](skills/traces/SKILL.md) | `audit-state-machine --trace-dir DIR` | Routing traces, not proof of semantic Skill use |
| [Sync](skills/sync/SKILL.md) | `sync --skills-dir ROOT --map-file FILE` | Explicit map remains authoritative |
| [Close](skills/close/SKILL.md) | Repeat the scoped audit | Verify applicable expectations; retain evidence |

Primitive mutation commands plan by default. Use `--apply` only within authorized scope. `SKILLS_AUDITOR_DRY_RUN=1` always suppresses application. `SKILLS_AUDITOR_APPLY=1` does not supersede managed plan/grant checks. Resolve concrete roots; never use a home or repository root as a destructive target. For a requested full legacy maintenance pipeline, order metadata → discover → dedup → route → trace QA → optional sync → close. A narrow request needs only its relevant cycle.

## Evidence and installation

For delegated or multi-step work, track resources with `ledger-create`, `ledger-upsert`, `ledger-check` and `ledger-summary`; store locators to plans/receipts/traces, not copied payloads. Keep raw prompts and credentials out of logs. See CLI `--help` for the chosen operation's actual flags and [cycle index](skills/README.md).

Use an existing compatible CLI. If missing, follow [installation](docs/install.md), or run `python3 scripts/skills_audit.py` from this repository. Editable installation is for developing Auditor, not a prerequisite for each audit.
