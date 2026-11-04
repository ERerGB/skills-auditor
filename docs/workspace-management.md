# Skills across workspaces and environments

`skills-audit workspace` manages a reviewed collection across local projects or
existing SSH connections. It is independent of Potology. Codex, Cursor CLI and
Claude Code use their native project Skill directories. Existing integration and
lifecycle v1 commands remain available; nothing adopts an old installation silently.

## Identity and storage

| Identity | Binding |
| --- | --- |
| Environment | Operator ID, explicit instance generation and target-local runtime fingerprint |
| Workspace | Environment, workspace ID, concrete path and directory device/inode |
| Agent platform | `codex`, `cursor`, or `claude-code` |
| Installation | Workspace/platform/Skill mapping to a lifecycle installation ID |
| Release | Git commit or explicitly marked local snapshot, content/permission hashes and relative entrypoint |

The same path in two environments is never one installation. Fingerprints detect
accidental runtime replacement; they are not remote cryptographic attestation.
Replacing an instance requires explicit enrollment with a new state directory and
generation. Old grants are not transported to the replacement.

Commit a [workspace declaration](../config/skills-auditor.workspace.example.json)
to each project. Its `sources` records contain source kind, pinned commit,
credential-free repository URL, package selection, required files, relative Skill
entrypoints and supported platforms. `baseline` selects source IDs; `overrides`
selects an explicit replacement entry or `null` to exclude one Skill. Different
versions should use different source IDs. Two baselines defining the same alias
block unless an override resolves it.

Source `root`, SSH alias, interpreter path, Potology tool/state paths, environment
bindings and workspace paths live in the private controller registry. Source
declarations take precedence over corresponding source-binding metadata. Older
binding-only configurations are supported, but should move portable source
metadata into the repository. No credential material belongs in either shared
declarations or release provenance.

Controller defaults to `$XDG_STATE_HOME/skills-auditor/workspaces` (otherwise
`~/.local/state/skills-auditor/workspaces`). A target has its own dedicated state
directory, disjoint from workspace and source trees. Controller plans, retained
releases and per-target results remain private. Target candidates, immutable
snapshots, local lifecycle grants, journals and registrations remain target-local.
Identical content is reused within that environment, across installations.

## Prepare local bindings

These explicit setup commands save private bindings and prepare empty native
directories. They do not activate a Skill. Example paths must be replaced with
existing directories; use a dedicated persistent state location.

```bash
skills-audit workspace environment add laptop --provider local \
  --generation laptop-v1 --state-dir /private/state/skills-auditor-node
skills-audit workspace source add baseline --root /sources/skills \
  --kind git --commit FULL_40_CHARACTER_COMMIT \
  --include skills --include scripts --include templates \
  --require-file scripts/check.py --require-file templates/report.md \
  --entry review=skills/review
skills-audit workspace register project-a --environment laptop \
  --path /work/project-a --declaration /work/project-a/skills-auditor.workspace.json
skills-audit workspace status
```

Use `--kind local` without `--commit` for an uncommitted snapshot. The plan labels
it `uncommitted_snapshot: true` and fixes its content hash. Editing the original
does not edit the active snapshot. A Git source is read directly from the pinned
object; dirty working-tree files, checkout hooks and filters do not enter it.
Git objects must already be available locally; this command does not fetch or
provision connectivity. Git submodules must be materialized and explicitly selected.

Select the complete dependency layout. For an entry `skills/review`, the whole
selected package is retained and the native link points inside its snapshot.
`../../scripts/check.py` therefore keeps its meaning. Declare external helper and
template files with `required_files`/`--require-file`; each must be present in the
selected fixed package. This is explicit dependency acceptance, not automatic
analysis of arbitrary code dependencies. Escaping, absolute, dangling and cyclic
links, malformed metadata and plugin packages block capture. Executable bits are
part of the content contract. Packets have a 64 MiB decoded payload bound and the
wire request is bounded separately; split large collections into smaller sources
and workspace plans.

## Review, apply and verify

```bash
skills-audit workspace plan project-a project-b --plan-out reviewed.json
skills-audit workspace apply reviewed.json --approve-plan-id REVIEWED_PLAN_ID
skills-audit workspace verify project-a project-b
```

The saved plan contains each target's environment/workspace generation, effective
selection, project exceptions, source hashes, before/after lifecycle versions,
migration scope and retained previous version. Planning may stage candidates and
persist verification evidence, but does not grant approval or change active links.
Any blocked target prevents applying that aggregate plan. Select an independent
healthy subset explicitly if it needs a separate review.

Apply rechecks controller bindings, declarations, source content, target directory
identity, environment generation, unchanged installations, lifecycle preconditions
and native discovery conflicts. It executes the saved child plans without
rediscovering the selection. Each target checks free space before candidate
staging and again before activation, reserving space for snapshots and evidence.
Capacity failure stops before installation changes on that target. Other targets
may already have completed; no cross-environment atomicity is promised.

User-level, project-level, parent repository and compatibility Skill roots are
checked for same-name entries, including nested packs and broken shadows. A
separate pointer to equal content is still an independently addressed installation
and requires an explicit migration decision. Native precedence never silently
chooses between different copies. Unsupported source/platform combinations block.
`status` includes unmanaged native entries as `legacy_installations`, explicitly
marked unverified and requiring migration review; inventory never adopts them.

`verify` checks managed hashes, authorization, the effective installation view,
native-root conflicts and actual file readability. Its `native_discovery` field
remains `unverified`: filesystem checks alone do not establish that a running
client reloaded its catalog. Complete acceptance with a fresh target CLI session:

1. Discover the selected Skill through the native catalog or slash invocation.
2. Have the client actually read its `SKILL.md` and selected dependency file.
   Resolve the entrypoint's physical path before interpreting outer relative
   package references: native clients may display the logical installation link.
3. Execute the declared bounded, read-only operation and retain successful output.

Record the client version and environment with the result. Cursor acceptance is
for Cursor CLI, not the desktop GUI. Missing or older clients stay unaccepted;
they are never automatically installed, upgraded or logged in by workspace apply.
Existing hooks, plugin trust and authorization remain under their native owner.

## Existing SSH and optional Potology

Install the same reviewed Skills Auditor package on the target, or configure
`--python-path` to a complete pinned copy. No bootstrap, credentials or VPN setup
is hidden in registration:

```bash
skills-audit workspace environment add buildbox --provider ssh \
  --connection existing-ssh-alias --generation buildbox-v1 \
  --state-dir /home/coder/.local/state/skills-auditor-node \
  --python python3 --python-path /opt/reviewed/skills-auditor
skills-audit workspace register remote-project --environment buildbox \
  --path /workspace/project --declaration /work/declarations/remote.workspace.json
```

SSH uses the native alias and its owner-local credential references, strict host
checking, batch mode and no agent/port forwarding. A bounded JSON worker accepts
only workspace protocol operations, not arbitrary shell instructions. Authentication
prompts or missing dependencies are reported, not replaced with another identity.

`--provider potology` additionally requires `--potology-tools`, `--potology-state`,
`--potology-target` and `--pairing`. The adapter asks the existing `potology-env.py`
for status, SSH routes and a validated connection vector. The target generation
must match; the selected pairing must belong to an owner-executable direct route
to that target. Delegated routes require execution by their owner and do not become
implicit ProxyJump routes. No core module imports Potology or reads its credentials.

## Failure, rollback and replacement recovery

```bash
skills-audit workspace recover reviewed.json
skills-audit workspace recover reviewed.json --mode resume \
  --approve-plan-id REVIEWED_PLAN_ID
skills-audit workspace rollback reviewed.json --plan-out inverse.json
skills-audit workspace apply inverse.json --approve-plan-id INVERSE_PLAN_ID
```

Controller and targets journal intent before effects. A lost response is uncertain,
not a clean failure or an automatic retry. `recover` inspects target journals and
stable child transaction IDs before explicit resume. Completed children are checked
without repeating pointer changes. Missing target journal plus existing child
effects requires investigation. Resuming a partial operation is an explicit action;
ordinary `apply` refuses it. Offline targets remain unknown while completed target
results remain recorded. Partial compensation can use the existing per-installation
lifecycle recovery; aggregate rollback requires completed deployment evidence.

Rollback generates a fresh inverse plan selecting retained versions or disabling
new installations. It requires fresh approval. It does not delete payloads or
restore an old grant, and a superseded old apply cannot overwrite current state.
Re-enabling an excluded Skill whose version also changed is deliberately blocked:
review re-enable of the retained version first, then plan the update.

For rebuild recovery, retain the selected source bytes separately:

```bash
skills-audit workspace export-release reviewed.json --out portable.release.json
skills-audit workspace plan replacement-project --release portable.release.json \
  --plan-out replacement.json
skills-audit workspace apply replacement.json --approve-plan-id NEW_PLAN_ID
```

Re-enroll the replacement environment/workspace first. Exported release envelopes
contain fixed package files and provenance, without machine paths, target links or
grants. They work after the original source path disappears. Fresh target planning
validates hashes and dependencies and issues new local approvals only on apply.
Repeat planning should report zero changes after successful activation.

## Legacy migration and deferred plugins

Keep readable legacy installations until the new entry passes acceptance. An
occupied unmanaged entry blocks normal deployment. A clean, same-target legacy
integration receipt can be supplied with
`plan --legacy-receipt ENVIRONMENT=CONTROLLER_RECEIPT_FILE` for explicit lifecycle
migration. It must still verify against the actual target and exact whole-package
layout; it is evidence, never a transported authorization. Mismatched old layouts
need a separately reviewed migration rather than copying a lone `SKILL.md`.

Tracked links, including the existing Magpie repairs, require a repository migration
commit/PR that identifies affected paths and records native acceptance before old
entries retire. Workspace apply does not commit, edit unrelated roots or remove
foreign shadow entries. Keep the previous delivery and its rollback evidence until
that migration is reviewed. Capacity or connectivity blockers do not authorize
cleanup of unrelated container data.

Plugin conversion, cross-environment installation/activation, native login/hook
trust, context synchronization and telemetry aggregation are excluded. The future
design is tracked in [issue #23](https://github.com/ERerGB/skills-auditor/issues/23).
Skill Trace access evidence, reuse counts and outcome assessment remain distinct;
this feature does not centrally collect any of them.
