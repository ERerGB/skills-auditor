# Managed Skill lifecycle

Use the managed lifecycle when editing a candidate must not change the bytes a
host currently uses, or when you need durable authorization and explicit crash
recovery. The legacy [`integrate/apply/verify` contract](integration-contract.md)
remains available and unchanged. Existing installations are not migrated silently.

This is a local Linux/macOS filesystem protocol, not a skill execution sandbox.
An individual pointer replacement is atomic; filesystem effects and the state
database are separate durable steps. A move between host roots is a recoverable
sequence, not a globally atomic transaction. Networked filesystem and distributed
host coordination are not supported guarantees.

## Identity and authorization

| Record | Meaning |
| --- | --- |
| Skill | Stable identity, independent of a directory or display name |
| Version | Content identity within a Skill and its first recorded origin; later adoptions keep their own exact plans |
| Installation | Stable identity, target path, selected version and lifecycle state |
| Grant | Explicit approval of an exact plan, version, target and installation generation |
| Transaction | Durable intent, individual step states, errors and recovery evidence |
| Receipt | Evidence of a completed transaction, not proof of present health |
| Verification | Point-in-time integrity and authorization observation |
| Status | Derived host-neutral projection, including observation age and next action |
| Incident | Durable failure signature, observation history, investigation notes and resolution evidence |

An authorized H1 snapshot remains active while a candidate is edited into H2.
Planning H2 does not authorize or activate it. Once a failed active verification
has invalidated a grant, restoring H1's bytes does not renew that grant. Explicit
revocation also survives later successful integrity checks. Review and approve a
new transaction to establish new authorization.

Version IDs bind Skill identity, source tree hash, normalized snapshot hash and
normalization version. Source path, plan time, operation and adoption parent are
not part of that content key. Two installations of one Skill can therefore adopt
the same content from independently reviewed plans, including different source
paths or previous versions.

The first committed version record retains its original `source`, `created_at`,
`parent_version_id` and `provenance`. A later adoption does not rewrite that
origin, including when it migrates a legacy installation. Its own exact saved
transaction plan records the current source, operation, legacy receipt if any,
and before/after selection. Read that plan for adoption history, rather than
assuming the version's origin describes every installation. Snapshot storage can
also deduplicate identical normalized bytes across different Skills without
merging their identities or lineage. Plans still bind their exact reviewed
content and all occurrence-specific operands.

## First managed installation

Prepare a candidate tree containing `SKILL.md`, and create the exact target's
parent directory. Source, installation targets and managed state must be disjoint.
Paths supplied on the command line resolve from the invoking working directory;
`--project-root` selects the state owner, not an implicit base for every argument.

```bash
skills-audit lifecycle --project-root /project plan install \
  --source /project/candidates/alpha \
  --target /project/hosts/codex/alpha \
  --plan-out /project/install-alpha.plan.json --format json
```

Review the saved file, including before/after states, source and snapshot hashes,
target paths and operation. Replace `REVIEWED_PLAN_ID` below with the exact
`plan_id` in that reviewed file. Merely saving a plan never authorizes apply.

```bash
skills-audit lifecycle --project-root /project apply /project/install-alpha.plan.json \
  --approve-plan-id REVIEWED_PLAN_ID --format json
skills-audit lifecycle --project-root /project verify INSTALLATION_ID --format json
skills-audit lifecycle --project-root /project preflight INSTALLATION_ID
```

Use the returned `installation_id` for later operations. Optional
`--transaction-id` supplies a caller's stable idempotency key. Reusing it with a
different plan is rejected; interrupted work requires explicit recovery. Retrying
a completed transaction checks current evidence and health, so a historical
success cannot silently override a subsequent update or invalidation.
An active-state failure observed by that retry uses the same durable denial
mechanism as verification; repairing bytes alone does not make the retry valid.

Plan output must stay outside candidate trees, managed state and installation
entries. Planning may initialize local state, record an active-state integrity
observation or denial, and save the requested artifact. It does not change host
pointers or candidate files, activate a version, or grant approval. Candidate
inspection failures and unrelated destination conflicts do not invalidate an
intact active version. Explicit revocation does not require content inspection.
JSON input is size bounded,
must be a regular file, and rejects duplicate keys and non-finite numbers.

## Governed changes

Every operation below is planned first and applied with its exact plan ID.

| Operation | Effect |
| --- | --- |
| `install` | Create a new installation identity and activate an approved snapshot |
| `install-retained` | Install an explicitly selected retained version under a new installation identity and new grant |
| `update`, `edit` | Adopt a replacement candidate tree; never edit a stored snapshot in place |
| `rename` | Change the installation's display name, preserving its identity |
| `move` | Activate the destination, then remove the owned old pointer; both steps are journaled |
| `disable`, `archive` | Remove the owned pointer and retain version/history for explicit enable |
| `enable` | Verify retained bytes and issue a newly approved active installation |
| `uninstall` | Remove the owned pointer; keep a terminal historical installation record |
| `renew` | Issue a new approval for unchanged selected bytes, without rebuilding a matching pointer |
| `revoke` | Persist denial without requiring readable target content; it does not erase payloads |
| `rollback` | Select an explicit retained version and issue new approval; never resurrect an old grant |
| `migrate` | Verify a legacy receipt and convert its exact clean source/target pair explicitly |

For example, `plan update` needs `--installation-id` and `--source`; `plan move`
needs `--installation-id` and the new `--target`; `plan rollback` needs
`--installation-id` and `--version-id`. Use `lifecycle plan --help` for the complete
argument surface. An uninstalled identity is historical: a later installation
gets a new identity rather than pretending the uninstall never happened.

The `edit` operation governs promotion of an edited candidate. It does not
intercept arbitrary editor writes or modify source metadata automatically.
Destructive payload cleanup is separate from disable, archive and uninstall.

## Snapshots and storage

State lives in `.skills-auditor-local/lifecycle/` under the selected project.
SQLite stores checksummed records and append-only event history. New stores are
initialized in a private staging database before no-overwrite publication;
read-only status and inspection do not initialize missing state, and a corrupt
existing database is rejected. Creation-capable commands can initialize an absent
database, but that does not recover lost installation identities or approval
history. Missing historical identities remain unknown and cannot pass preflight.
If the database is lost, preserve the store and targets and restore the state
from a trusted backup; do not interpret a newly empty registry as recovery.

Payloads are staged, hashed and synchronized before publication under
`store/sha256/<snapshot-hash>/tree`. Regular files and directories lose write
bits while retaining read/executable semantics. Records keep both the source
hash and normalized snapshot hash; normalization does not modify the source.
Supported symlink permission bits are preserved without following their targets.
Safe relative internal links are allowed; absolute, escaping, dangling or looping
links and special files are rejected. Snapshot copies do not create hardlinks
back into candidate trees.

Read-only permissions are accidental-write protection, not a security boundary
against the same user or a privileged writer. Verify before use. Do not move,
edit, replace or manually prune the state/store directories. A project-state
directory moved or retargeted outside the protocol is not a supported migration.

## Failure and recovery

Intent is durable before a pointer effect. Each step distinguishes pending,
intent, completed and compensation progress. A completed receipt is committed
only with its installation/grant result. If the process stops between a pointer
change and that commit, the pointer can already expose complete new bytes while
the transaction still requires recovery; this is not a false completed receipt.

```bash
skills-audit lifecycle --project-root /project inspect transaction TRANSACTION_ID
skills-audit lifecycle --project-root /project recover TRANSACTION_ID --mode inspect
skills-audit lifecycle --project-root /project recover TRANSACTION_ID --mode resume \
  --approve-plan-id REVIEWED_PLAN_ID
```

Choose `--mode compensate` with the same explicit plan approval to restore owned
before-states for unfinished work. Compensation does not recreate the original
symlink inode, overwrite a foreign entry, or revive an invalidated authorization.
Completed work instead requires a newly reviewed inverse operation or rollback
plan. A partial recovery remains inspectable and may itself need another recovery.
When all owned pointers are already in their before-states, explicit compensation
can cancel the unfinished work without rebuilding them. This is cancellation,
not proof that the old payload is healthy: observed damage remains denied and a
new repair still needs approval. Actually restoring an old pointer requires a
valid retained snapshot; compensation never installs known damaged old bytes.

Once the transaction has durably recorded `snapshot_ready: true`, resume uses
those approved snapshot bytes rather than adopting a subsequently edited
candidate. Before that readiness record, including a crash after snapshot
publication but before the record commits, resume still requires the exact
reviewed source. A changed or missing source must be restored by its owner or
the unfinished work explicitly compensated; it is never silently substituted.
Readable state/store, intact required retained bytes and nonconflicting target
ownership remain recovery prerequisites. Inspect before retrying after an
uncertain journal or receipt write. Preserve temporary artifacts until the
transaction and ownership are understood; an absent receipt is not proof of no
filesystem effects.

Text errors with a transaction or batch reference include a project-scoped,
read-only inspection command. If writing the failure journal also fails, the
structured error retains both failures and the recovery ID. Inspect that ID
before choosing resume or compensation; an uncertain response never authorizes
replaying effects or permanent deletion.

If the process exited before returning its generated ID, discover unfinished
work in the original project without needing an installation record:

```bash
skills-audit lifecycle --project-root '/project with spaces' list --pending \
  --limit 50 --format json
```

The result lists core `transaction`, parent/inverse `batch`, and
`retention-transaction` intents with their IDs, states, approved plan IDs and
read-only `inspection` actions. Core children include parent references; batches
include deterministic child references, which may precede a child's own WAL.
These are different records, not duplicate authorization. Completed and
compensated work is excluded. A newly rejected plan/apply request reports the
original unfinished ID rather than creating an unrelated recovery task.

Pages default to 50 entries and accept 1–100. When `has_more` is true, pass both
`continuation.after_kind` and `continuation.after_id` as `--after-kind` and
`--after-id` on the next request to the same project. Ordering is by kind/ID,
not execution time. Discovery record fetches and output pages are bounded, but
locating unfinished work and validating parent/history proofs can scan metadata
and load existing event streams. Total I/O and memory depend on that history;
this is not constant-resource discovery. Listing and its inspection actions do not read candidate payloads,
initialize absent state or choose recovery automatically. Missing/corrupt state
is an error, not an empty healthy catalog. Stages and verification fences retain
their existing diagnostics; this view is not a global filesystem audit.

Locks serialize cooperating writers across overlapping physical target parents,
including separate project stores. Lock files are persistent and must not be
deleted to bypass contention. Noncooperating filesystem writers remain outside
this coordination guarantee; detected foreign state is preserved and reported.

Within one project registry, a new mutation cannot supersede an unfinished
transaction for the same installation or overlapping managed targets. Inspect the returned recovery ID
and explicitly resume or compensate first, then generate a fresh plan. This
reservation also applies to an unfinished parent batch before its first child
starts. Explicit revocation remains available; it denies approval, never silently
resumes the old work. Subsequent compensation must preserve that revocation and
its evidence rather than restoring an old grant.
Separate project registries share live target locks, not a distributed pending
transaction catalog; after a crash, another project's foreign state must still
be preserved rather than overwritten by recovery.

## Multi-installation batches

A batch groups 1–50 saved child plans in one project registry. Children must use
distinct installation identities and nonoverlapping targets. It can coordinate
different host directories accessible on the same supported local filesystem;
it does not coordinate remote machines or merge separate project registries.
Sequential changes to the same installation require separate plans after each
preceding operation, rather than a batch that guesses future revisions.

Review both the parent order and each exact child plan. Parent approval binds the
entire saved batch. All child preconditions are checked before the first effect,
then the parent write-ahead record is committed before executing children. Each
child has a deterministic transaction identity, its own durable steps and receipt.
If a later child fails, earlier completed changes remain visible and the parent
reports recovery needed. No all-or-nothing claim applies across installations.

Shared content is allowed: two existing installations of the same Skill can
independently plan adoption of H2 and apply both plans in either reviewed child
order. The first committed origin is retained; the other child's exact adoption
plan remains unchanged. Conflicting definitions of a shared Skill or content
identity are rejected before the first child effect. During a crash after only
one child commits, H2/H1 can legitimately be visible; recovery resumes the saved
H2 rollout or follows an explicitly approved inverse, never inventing a new
approval from the partial state.

Resume requires explicit approval of the recorded parent plan and validates
completed children without replaying their effects. Compensation requires a new
inverse batch plan and its own explicit approval. It restores unfinished owned
steps conservatively and expresses inverses of completed operations as new
transactions. For example, an update can select its retained old version with a
new grant; undoing an uninstall uses `install-retained` with a new installation
identity, while the original uninstalled identity stays historical.

The inverse plan records operations that cannot be safely compensated in its
`uncompensated` list. Completing the executable inverse children does not imply
that this list is empty. The original parent is marked compensated only when all
required owned effects have verified inverses; unresolved items stay visible.
Starting an approved inverse fences forward resume of the original parent.
Never revive an old grant or overwrite foreign data merely to finish a batch.

Completion is historical. If A completes, inverse B completes, and a newly
approved inverse C later compensates B, both A and B retain their original
completion receipts/events. Their current `compensated` state does not erase
what completed earlier. History inspection and retention validate those bound
proofs without requiring historical payloads to remain on disk after legitimate
purge. Missing or contradictory proof still blocks the operation.

Compensation chains are bounded to 50 batches. An inverse that would exceed the
bound is refused with `batch_history_limit` before a new inverse intent or host
effect. Existing ancestors remain inspectable; an ordinary newly reviewed
lifecycle operation is still available. This limit is not permission to remove
historical records or silently drop a chain from reference validation.

For two already saved core plans, create and review the parent before applying:

```bash
skills-audit lifecycle --project-root /project batch plan \
  /project/alpha.plan.json /project/beta.plan.json --plan-out /project/batch.plan.json
skills-audit lifecycle --project-root /project batch apply /project/batch.plan.json \
  --approve-plan-id REVIEWED_BATCH_PLAN_ID --batch-id reviewed-batch
skills-audit lifecycle --project-root /project batch inspect reviewed-batch
```

After an interruption, use `batch resume reviewed-batch --approve-plan-id` with
the original parent plan ID. To choose compensation instead, create a new plan:

```bash
skills-audit lifecycle --project-root /project batch compensate-plan reviewed-batch \
  --plan-out /project/inverse.plan.json
```

Review its inverse operations and `uncompensated` items, then use `batch apply`
with the inverse file and its own exact plan ID. It is not approved by merely
reusing the original batch approval.

## Visible status and use-time checks

`lifecycle status ID` reads cached evidence without inspecting candidates or
initializing missing state. `[OK]`, `[WARN]` and `[BLOCK]` distinguish a fresh
valid active installation, stale evidence, and denied/unknown/inactive states.
Freshness defaults to 300 seconds and is separate from authorization.

`lifecycle preflight ID` performs a fresh verification by default. `--cached` is
an explicitly cached diagnostic. Verification starts with a durable in-progress
marker, then commits its observation and any denial before publishing derived
status. An interrupted observation or failed projection must not expose a stale
healthy cache; failure to publish status cannot undo an already observed denial.
An unresolved observation is bound to its grant, version and generation: a later
clean read cannot erase that uncertainty for the same grant. Explicitly renewed
authorization can supersede the old marker. Ordinary lock contention before an
observation begins does not itself revoke approval.

| Exit | Managed CLI meaning |
| --- | --- |
| `0` | Successful command, or proceed decision |
| `2` | Invalid arguments, input or machine contract |
| `3` | Blocked, unknown, failed verification or operation requiring investigation |
| `4` | Stale plan or stale-evidence warning; do not treat as successful apply |

Adapters may render the status as an icon or badge. No IDE integration or
continuous monitor is implied by this contract. A host that ignores preflight
can still read a pointer; Skills Auditor does not execute or sandbox the Skill.

Status, nested preflight/invocation status, investigation pages and error output
retain `project_root`. `context_verified: false` means the requested project is
known but its repository context was not established; it is not a verified owner
or healthy installation. Running a suggested command from another working
directory must still select this explicit project.

Status recommendations and pending inspection actions provide a shell-quoted
`command`, `argv` and `required_inputs`. Prefer concrete `argv` for an adapter;
do not split the display command on spaces. When source, target or another input
is missing, `argv` is null and `required_inputs` names the missing fields: the
displayed command is a template, not an executable repair. Supplying inputs or
following a diagnostic does not approve a plan.

## Investigation and resolution

A failed verification records an incident after committing its core evidence and
denial. Status supplies incident IDs and an investigation entry. Failure to write
this derived incident does not undo the denial. If publication fails, preserve
the verification ID and inspect/retry the evidence publication; do not interpret
an absent incident as a healthy result.

Repeated observations of the same installation, grant, version and failure
signature share an incident. Different expected/actual evidence opens a distinct
incident. Timestamps, random verification IDs and incidental error wording do not
create a new signature. A clean read while the old grant remains invalidated does
not invent another content fault or close the existing incident.

Investigation packets contain allowlisted identity, expected/actual checks,
receipt/transaction/verification references, timestamps, local actor/tool labels
and bounded pages of events. The default page has at most 50 events; a cursor
continues the oldest-to-newest history without loading the entire stream.
Append-only notes can link existing records for an Agent's next investigation.
They do not fetch arbitrary files or collect prompts, credentials or arbitrary
environment variables. Operator-written notes can still contain sensitive text: review them
before export and protect the project state directory.

Remediation requires a new explicitly approved grant and its completed receipt,
transaction and clean verification for this installation. Byte restoration alone
is insufficient. Historical resolution retains its proof even after a later
legitimate update; it is not a statement that the installation remains healthy
forever. An explicit non-remediation disposition or superseding incident needs an
explanation and never changes authorization.

Use the IDs from failed verification/status to enter the investigation:

```bash
skills-audit lifecycle --project-root /project incidents --installation-id INSTALLATION_ID
skills-audit lifecycle --project-root /project investigate INCIDENT_ID --limit 50
skills-audit lifecycle --project-root /project append-note INCIDENT_ID \
  --text "Checked target ownership; waiting for explicit renewal review." \
  --evidence-ref verification:VERIFICATION_ID --actor local-reviewer --tool investigation
```

When a packet has `has_more`, pass its `continuation.after_sequence` to the next
`investigate --after-sequence` call. After an explicitly approved repair and
new clean verification, `resolve INCIDENT_ID --verification-id NEW_VERIFICATION_ID`
records that proof. Alternatively, an explicit `--disposition` with
`--explanation` records a non-remediation decision. `supersede INCIDENT_ID
REPLACEMENT_ID --explanation ...` links a related existing incident, not a repair.

### Optional capture evidence

Skill Trace capture health is a separate, advisory dimension. Disabled, stale,
unverified or erroneous capture does not invalidate a Skill grant; healthy
capture cannot authorize a blocked Skill, prove semantic Skill use, or attest
that the host trusts its hooks. Managed commands retain warning-only capture
preflight: stderr may warn, but the command's JSON stdout and exit status retain
their existing meaning. No lifecycle command changes capture preferences or
host hook permissions.

Recording a snapshot and associating it with an incident are explicit steps:

```bash
skills-audit lifecycle --project-root /project capture-evidence \
  --evidence-id CAPTURE_ID --log-dir ./task-logs --actor local-reviewer --tool investigation
skills-audit lifecycle --project-root /project inspect capture-evidence CAPTURE_ID
skills-audit lifecycle --project-root /project append-note INCIDENT_ID \
  --text "Recorded optional task-local capture diagnostics; no remediation claimed." \
  --evidence-ref capture-evidence:CAPTURE_ID --actor local-reviewer --tool investigation
```

The task working directory, managed project and sensor log root remain distinct.
Here `./task-logs` is relative to the invoking task's working directory, not
`/project`. A task B observation stored in project A does not prove that a Skill
in A was read. The immutable snapshot keeps these contexts, the observation
time/session, preference source, allowlisted health state and hook timestamps;
host trust remains `unverified`. It copies neither raw log lines nor tool data,
prompts, arbitrary error prose or referenced files. Paths and IDs can still be
private. An identical scoped retry with the same evidence ID returns the original
observation without resampling; changed requests conflict.

`investigate` adds optional `capture_evidence` snapshots for references in the
returned event page, deduplicated by evidence ID. Each note accepts at most 20
references; a page has at most 50 events and 1000 distinct capture records.
The runtime limits each canonical UTF-8 evidence record to 32 KiB, so the record
bodies total at most 32,768,000 bytes per page (plus list separators and the
incident/event wrapper); pretty-printed output can be larger. JSON Schema
constrains fields and lengths, while the serialized byte ceiling is a runtime
check rather than a JSON Schema keyword.
`limits.evidence_refs_per_event` and `limits.capture_evidence_max_records` expose
these ceilings. Readers validate local ownership, immutable revision and durable
completion evidence without following the stored paths. Missing or inconsistent
records fail closed rather than appearing as healthy diagnostics. These are
output bounds, not a constant-I/O guarantee for historical proof validation.
Older investigation documents without the additive fields remain schema-valid.

Capture evidence holds no version or snapshot payload reference. Its attachment
does not resolve the incident or change the incident's existing retention roots;
explicit incident resolution/expiry remains necessary to release those roots.
Diagnostic metadata remains readable after a legitimately expired payload is
collected or purged.

## Invocation selection and temporary exceptions

The invocation adapter returns a decision and, only when permitted, the current
approved snapshot path. It does not execute the Skill. Both `strict` and
`last-known-good` keep intact active H1 while H2 remains an unapproved candidate.
Neither automatically chooses an older historical version after active content
fails. Such a version change needs a new reviewed rollback plan and new grant.

Fresh selection verifies by default. With cached evidence, `strict` blocks stale
observations and `last-known-good` returns a warning without a usable path. A
separate explicitly approved override can waive only this stale-evidence gate.
It binds the installation, version, grant, target, generation and reviewed age
limit, requires a reason, and expires within 1–3600 seconds of its plan's creation.
Approval does not restart the expiry clock. Revocation and every actual use have
durable audit evidence; a retry cannot reactivate a revoked exception.

Even a cached override performs a live integrity check. A clean check does not
refresh the old observation's age. Failure or interruption uses the same durable
denial/fence as verification. Unknown, inactive, revoked, damaged or differently
bound state cannot be waived. A successful decision also requires a durable audit
record. There is no lease preventing a noncooperating writer from changing bytes
after selection; the host remains responsible for integrating this gate.

Prefer a fresh use-time decision:

```bash
skills-audit lifecycle --project-root /project invocation select INSTALLATION_ID --format json
```

Only if an operator deliberately chooses a bounded stale-cache exception:

```bash
skills-audit lifecycle --project-root /project invocation override-plan INSTALLATION_ID \
  --reason "Keep the reviewed cached observation for this bounded adapter use." \
  --ttl-seconds 300 --plan-out /project/override.plan.json
skills-audit lifecycle --project-root /project invocation override-apply /project/override.plan.json \
  --approve-plan-id REVIEWED_OVERRIDE_PLAN_ID
skills-audit lifecycle --project-root /project invocation select INSTALLATION_ID \
  --cached --override-id OVERRIDE_ID --format json
skills-audit lifecycle --project-root /project invocation override-revoke OVERRIDE_ID \
  --reason "The temporary exception has ended."
```

An override plan is rejected when cached evidence is not stale. If you review a
nondefault `--max-age-seconds`, use that same limit for selection. A successful
selection reports whether the override was actually used; it does not relabel
stale evidence as fresh or make unreadable content available.

## Payload retention and cleanup

Uninstall is not deletion. Receipts, transactions, incidents, notes and version
lineage remain historical evidence. A retention policy keeps the three most recent
versions per Skill by default; explicit version pins add retained payloads.
Active, disabled and archived installations, current approval evidence, unresolved
incidents, unexpired receipt/incident references, live overrides and unfinished
transactions protect their referenced snapshots. Unfinished parent and inverse
batches protect their before/after references even before any child transaction
exists. Releasing one root does not release another installation's references to
the same content. Completed metadata remains readable after approved payload
expiry and purge; it does not pin every historical payload forever.

Cleanup is a series of separate reviewed plans:

1. `policy` changes recent-version retention or explicit pins. Omitting a field
   preserves its setting; an explicitly empty pin list clears pins.
2. `expire` releases selected historical receipt/incident **payload-retention**
   references. It does not erase those records or revoke a current installation.
   Current installation receipts and unresolved incidents cannot be expired.
3. `collect` rechecks references and exact object identity, then moves selected
   unreferenced objects into same-filesystem quarantine. This does not free disk
   space. Staging directories require explicit selection and a nonblocking lease
   check that proves no live materializer owns them.
4. `restore` moves an exact quarantined object back only if the original location
   is unoccupied. It does not install the version or issue approval.
5. `purge` permanently deletes exact quarantined inventory after the reviewed
   grace period (default seven days), requiring both exact plan approval and the
   explicit permanent-delete gate. Purge is irreversible and never compensatable.

Expiration and policy projections are checked against their completed approval
evidence before they can release references. Collection/purge recheck references
at the effect boundary, not only when the plan is written. Foreign entries,
uncertain ownership, changed content or unreadable state stop the operation.

Interrupted collection can be resumed or explicitly compensated. A completed
collection instead needs a new restore plan. Interrupted restore, policy and
expiry resume their exact approved intent; they do not offer compensation.
Interrupted purge retains per-entry
intent and an honest partial-deletion tombstone for explicit resume; no successful
receipt is published for partial deletion. Do not manually remove quarantine,
staging directories, locks or evidence to bypass these checks.

For example, inspect a collection plan for an exact unreferenced digest:

```bash
skills-audit lifecycle --project-root /project retention plan collect \
  --object-id SNAPSHOT_SHA256 --plan-out /project/collect.plan.json
skills-audit lifecycle --project-root /project retention apply /project/collect.plan.json \
  --approve-plan-id REVIEWED_COLLECTION_PLAN_ID
skills-audit lifecycle --project-root /project retention recover RETENTION_TRANSACTION_ID
```

`retention recover` inspects by default. Resuming requires `--mode resume` and the
recorded exact plan ID; compensating an unfinished collection uses `--mode
compensate`. A later `retention plan restore --object-id QUARANTINE_ID` makes a
separate reviewable restore plan. Purge uses that quarantine ID, waits for the
reviewed grace period, and `retention apply` additionally requires
`--permanent-delete`; ordinary plan approval alone cannot delete payloads.

## Legacy mutation guards

Legacy integration, metadata repair, sync, route and dedup writers reject
recognized managed store paths and installation entries before their first
effect. This includes their plan/receipt outputs and whole-plan batches. Use
managed plans for managed changes; do not retry a denied operation through a
lower-level legacy command.

Recognition is deliberately bounded to existing registry context, supplied paths
and their project ancestors. It is not a global filesystem scanner or access
control boundary. A drifted ordinary file belonging to an unrelated project may
be unrecognizable when that project's registry is not in scope. Nonmanaged legacy
behavior remains compatible. Keep the correct project context when invoking any
writer, and never use filesystem aliases to bypass a managed denial.

## Machine contracts and compatibility

The shipped [core schema bundle](../skills_auditor/schemas/lifecycle-core-v1.schema.json)
contains named `$defs` for plans, steps, versions, installations, grants,
transactions, receipts, verification, events and snapshots. The
[status contract](../skills_auditor/schemas/lifecycle-status-v1.schema.json)
allows additive reader fields. Resolve schema references from the installed
package: schema IDs are identifiers, not a requirement to fetch GitHub at runtime.

Separate shipped contracts cover [batches](../skills_auditor/schemas/lifecycle-batch-v1.schema.json),
[incidents](../skills_auditor/schemas/lifecycle-incident-v1.schema.json),
[incident lists](../skills_auditor/schemas/lifecycle-incident-list-v1.schema.json),
[investigation packets](../skills_auditor/schemas/lifecycle-investigation-v1.schema.json),
[invocation](../skills_auditor/schemas/lifecycle-invocation-v1.schema.json), and
retention [plans](../skills_auditor/schemas/lifecycle-retention-plan-v1.schema.json)
and [receipts](../skills_auditor/schemas/lifecycle-retention-receipt-v1.schema.json).

JSON Schema validates shape; runtime additionally checks checksums, immutable
record identity, cross-record binding, expected revisions, actual filesystem
state and explicit plan approval. Checksums and local actor/tool labels are not
signatures or authenticated identity. Standard-library runtime dependencies do
not remove these trust limitations.

Legacy receipt verification remains stateless: H1 → H2 → H1 can make the same
legacy receipt valid again. Managed verification remembers invalidation. To
migrate, supply the complete legacy receipt with `plan migrate --legacy-receipt`;
an invalid legacy receipt cannot be used to bless current bytes.
