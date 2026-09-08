# Managed lifecycle: state model and invariant-driven acceptance

This is the model-first refinement of [the executable goal](lifecycle-goal.md),
not another feature roadmap. It governs Issue #19 / PR #20 and the existing
status, incident and snapshot slices. Legacy integration v1 remains unchanged.
The [operator guide](managed-lifecycle.md) describes the public commands.
The subsequent [Skill Trace alignment](lifecycle-trace-alignment.md) adds an
orthogonal advisory capture dimension after PR #21; it does not relax the
frozen core authorization or transaction policy below.

## Alignment brief and frozen scope

The existing operations, local Linux/macOS filesystem boundary and explicit
approval policy are frozen. This iteration makes their combinations coherent;
it does not introduce new host icons, a network coordinator, automatic fallback,
an execution sandbox, or interception of external editors. No PR is merged and
no issue is manually closed by this work.

The acceptance order is: describe objects and transitions; define independent
sequence assertions and demonstrate RED; implement the smallest coherent fixes;
review all consumers of each affected invariant; run complete final validation.
Passing isolated operation tests or a coverage threshold is not the acceptance
definition. This document records requirements; acceptance evidence is separate.

Existing constraints are the local [security contract](security.md), the
L01–L16 goal, shipped schemas and operator guide. There are no repository ADR or
Fulmail-specific governance scripts to import into this project. State, findings
and verification evidence belong to this project's context.

## 1. Object, identity and ownership model

There are three authority layers: immutable historical facts; mutable but
authoritative current state (installation, authorization and unfinished intent
or verification fences); and derived views. The second layer is not a disposable
cache. Rebuilding a status or incident view must not erase denial or pending work.
Immutability is an application protocol, not resistance to privileged database
rewrites.

| Object | Identity / owner | Immutable facts | Mutable projection or external observation |
| --- | --- | --- | --- |
| Project | Canonical project root; one repository database | Saved plans name their owning project | Database availability; no implicit transfer to the invoking directory |
| Skill | `skill_id`, owned by a project | Identity and original identity record | Installation display names may differ; name/path is not identity |
| Candidate | Source tree observed in a saved plan | Exact reviewed source and normalized content hashes in that plan | User may edit source; this is not an active installation |
| Version | `version_id`, within one Skill | Content identity and recorded origin/lineage | None: existing version records are not rewritten by a later adoption |
| Snapshot | Normalized tree hash; project content store | Exact normalized bytes, modes and safe link topology | Payload may be damaged externally, quarantined or explicitly purged |
| Installation | `installation_id`, project and Skill | Identity; original creation | Selected version, target, name, generation, lifecycle and authorization |
| Plan | Digest of complete saved plan | Reviewed operands, before/after states, source observation, expected revision | None: execution must not rewrite a plan to make it valid |
| Grant | `grant_id`, one installation generation | Approved plan, installation, version, target and attribution | Separate authorization projection may invalidate or revoke it |
| Core transaction | Transaction ID bound to exact plan | Approval binding, before/after intent, any completed receipt | Step progress, recovery errors and terminal outcome |
| Batch | Batch ID bound to exact ordered parent plan | Child plans, child IDs, completion receipt/event | Forward progress; later compensation relation and progress |
| Receipt / event | Unique receipt ID / append-only event sequence | What completed or was observed at that time | Never used alone as proof of present health |
| Verification | Verification ID bound to installation/grant/version | Completed point-in-time observations and check results | Latest pointer and in-progress/completed run marker |
| Status | Derived from installation and completed evidence | No independent authority | Freshness, severity, reasons and scoped next action |
| Incident | Signature bound to installation/grant/version/failure | Opening evidence and append-only observations/notes/resolution proof | Open, investigating, resolved or superseded projection |
| Invocation override | Exact approved exception binding | Plan, approval receipt and use/revocation events | Active, expired or revoked; cannot waive integrity or authorization |
| Retention transaction | Transaction ID and exact retention plan | Selected objects, identities, references and approval | Per-object move/delete progress; quarantine location and retention policy |
| Capture evidence | Explicit evidence ID, owned by the selected project | Bounded task/log context, health observation and completion event | No authority over grants; current capture may differ from this historical observation |

The dependency graph is not a list of independent features:

```text
project -> Skill -> version -> content-addressed snapshot
       -> installation -> selected version + target + generation
                       -> grant -> exact approved plan
                       -> receipt -> completed core transaction
batch -> ordered child plans / transactions -> receipt + completion event
      -> later inverse batch (does not erase original completion)
verification -> installation + grant + version + receipt + transaction
             -> durable denial / latest observation -> status -> invocation
             -> incident -> investigation and evidence-backed resolution
retention -> installation / receipt / incident / pending intent / override roots
task cwd + session + log root -> optional capture health (not hook trust)
explicit capture evidence -> incident note / investigation (not authorization)
```

### Version identity is not an adoption occurrence

The content key consists of Skill identity, source tree hash, normalized snapshot
hash and normalization version. Two installations may adopt that same content
from independently saved plans. Plan creation times must not turn those plans
into mutually incompatible definitions of the same content key.

The existing version origin fields describe the recorded origin, not every
subsequent adoption. Each exact transaction plan retains its own operation,
source and before-version evidence. Implementations must preserve that history,
validate content identity independently of occurrence metadata, and leave an
already recorded version unchanged. They must not silently edit approved child
plans, overwrite origin records, or disable immutable-field validation globally.
The exact plan/transaction already records each adoption; a new parallel history
database or another adoption-record API is not needed for this correction.

A batch is admissible only if its shared identity definitions can coexist. The
ordinary same-Skill multi-installation rollout must succeed. Truly conflicting
definitions must be rejected before any child effect; recovery of existing
approved intents must not depend on pretending a partial effect never happened.

### Completion is historical; compensation is a later action

For a completed batch A with inverse B, A's receipt/event remains a completion
fact even if A's current projection is `compensated`. If B later has inverse C,
B's completion receipt/event also remains a fact. History readers validate the
acyclic chain and each immutable completion proof, rather than requiring every
inverse's current state to remain `completed` forever. A missing or mismatched
proof still fails closed. This rule also governs incident resolutions, override
revocations, retention completion and completed-operation retries.

### Historical references are not permanent payload roots

Logical snapshot content identity differs from the identity of a physical object
at a path. A quarantined object and a newly materialized tree of identical content
are not the same inode/instance. Retention checks both content and exact physical
ownership; it must not restore or delete a foreign replacement because its name
or hash looks familiar.

| Root | Payload retained until |
| --- | --- |
| Active, disabled or archived installation | A legitimate later selection or uninstall releases that installation's references |
| Explicit pin or recent-version policy | A newly approved policy changes the root |
| Unexpired receipt | Independently approved expiry completes with its receipt/event proof |
| Unresolved or unexpired incident and its notes/resolution references | Valid disposition plus explicit expiry permits release |
| Active invocation override | Valid revocation proof or actual expiry releases the override root |
| Unfinished core intent | Proven completion or compensation; both before and planned after content stay protected |
| Unfinished parent or inverse batch, including zero-child WAL | Proven terminal disposition of all represented work, including uncompensated items |
| Active/unknown stage | Ownership, released lease, no references and explicit selection permit collection |

Releasing one root does not release other installations or evidence referring to
the same bytes. Completed historical metadata remains readable after legitimate
payload purge; following every historical lineage edge as a permanent payload
root would incorrectly prevent all collection.

## 2. State transitions and commit boundaries

### Orthogonal state dimensions

- Installation: absent -> active -> disabled / archived -> active, or any
  supported installed state -> uninstalled. Uninstalled identity is historical;
  reinstall gets a new installation identity.
- Authorization: unknown -> valid only through exact approved completion;
  observed active failure -> invalidated; explicit revoke -> revoked. Restoring
  bytes, rereading status, retrying an old receipt or compensating an unfinished
  pointer operation does not resurrect the old grant.
- Integrity: unobserved / passed / failed observation, separate from authorization.
  Candidate H2 failure does not invalidate intact active H1.
- Freshness: unknown / fresh / stale / future-invalid, derived from a completed
  observation and current policy. A clean cached probe does not refresh its age.
- Core transaction: prepared -> applying -> completed, or recovery_needed;
  explicit resume continues owned intent; explicit compensation ends unfinished
  work as compensated. A completed operation needs a newly approved inverse.
- Batch: prepared -> applying -> completed or recovery_needed; an approved
  inverse claims the original as compensating, then compensated only when no
  uncompensated work remains. Later inverses do not erase earlier receipts.
- Retention: prepared (with per-object progress) -> completed or recovery_needed. Only unfinished
  collection supports compensation in the frozen API. Restore, policy and expiry
  resume their exact intent; a completed collection can have a new restore plan.
  Purge has irreversible per-entry progress and can only resume with the separate
  deletion authorization.

### Core operation matrix

All rows require the exact saved plan approval, project ownership, applicable
revision/content/target checks and conflict-free intent admission. Active-state
observations use the shared durable denial fence. Target changes never overwrite
unmanaged or foreign entries. `C` denotes the atomic database completion described
below; filesystem effects occur before C and may require recovery.

| Operation | Required state / input | Installation and pointer result | Authorization / identity | Completion and recovery |
| --- | --- | --- | --- | --- |
| install | New identity, source, absent target | Active at selected snapshot | New Skill if requested; new installation/grant; register or reuse content version | Stage -> pointer -> C; resume or remove owned unfinished pointer |
| install-retained | Retained intact version, absent target | Active at retained snapshot | Same Skill/version; new installation/grant | Pointer -> C; compensate new pointer, not historical identity |
| migrate | New identity, exact clean legacy receipt/source/target | Active snapshot replaces exact legacy link | New managed identity/grant; legacy receipt preserved | Stage -> replace -> C; unfinished compensation restores owned legacy link |
| update | Active, replacement candidate | Active at candidate snapshot | Same installation; selected version and new grant | Stage -> replace -> C; resume staged bytes or compensate verified old pointer |
| edit | Active, replacement candidate | Same as update; source is never edited | Same identity; new version selection/grant | Same protocol as update |
| rollback | Active, explicit intact retained version | Active at selected historical bytes | Same installation; NEW grant | Replace -> C; never revive a historical grant |
| renew | Active, intact selected snapshot | Active; matching pointer stays untouched | Same version; NEW grant | Noop pointer still checks approval/preconditions -> C |
| move | Active, absent destination | Destination active, owned origin absent | Same installation/version; NEW target-bound grant | Two journaled steps -> C; partial destination visibility is explicit |
| rename | Non-uninstalled identity, intact selected snapshot | Name changes; lifecycle and target preserved | Same installation/version; new grant under current contract | Matching pointer is noop -> C; inactive installation remains inactive |
| disable | Active | Disabled; owned pointer removed | Retain version/history and authorization state | Unlink -> C; actual compensation requires valid retained old bytes |
| archive | Non-uninstalled identity | Archived; owned pointer absent | Retain identity/version/history | Owned removal or noop -> C; prior state dictates inverse |
| enable | Disabled or archived, intact retained snapshot | Active; owned absent target linked | Same installation/version; NEW grant | Link -> C; unfinished compensation restores absence |
| uninstall | Non-uninstalled identity | Uninstalled; owned pointer absent | Terminal identity; retain historical records | Removal/noop -> C; later inverse is new install-retained identity |
| revoke | Non-uninstalled identity; no readable-content requirement | No pointer/content changes | Explicit persistent denial; no new grant | Metadata C; pending work stays pending; later recovery preserves denial |

At C, the installation generation, new grant if applicable, authorization
projection, completed receipt, transaction state and completion event commit
together. A post-commit response failure does not rewrite them as failure.
Read/parse/hash failure never substitutes empty or valid data. No-effect
compensation may cancel pending work without claiming damaged retained bytes are
healthy; actual restoration still requires intact retained bytes.

### Consumer and retention transitions

| Entry | Preconditions / effects | Durable boundary / interrupted outcome | Recovery or follow-up |
| --- | --- | --- | --- |
| verify / planning observation / retry health | Exact current binding; marker before fallible active inspection | Marker -> observation and denial commit -> derived incident/status publication | Incomplete marker blocks cached use; restored bytes do not clear denial |
| status / cached preflight | Read existing repository/evidence; no initialization | Read-only derived result | Every diagnostic command preserves project; unknown stays blocked |
| fresh preflight / invocation | Verify by default; active + authorized + intact + acceptable freshness | Verification fence; successful selection requires audit event | Never select candidate or revive historical grant; not an execution lease |
| override plan/apply/revoke | Exact bounded stale-only exception; expiry and binding rechecked | Approval/revocation facts committed before use | Retry cannot extend expiry or reverse revocation |
| incident observe/note | Bounded failure signature or allowlisted references | Append-only event and validated projection | Repeat signature deduplicates; investigation is project-scoped and paged |
| resolve / supersede | New-grant remediation proof or explicit disposition/replacement | Durable resolution/supersession proof | Later lifecycle changes do not erase historical proof or grant authority |
| retention policy / expire | Exact current references and reviewed metadata selection | WAL -> policy/expiry plus receipt/event commit | Resume incomplete work; expiry is not payload deletion |
| collect | Unreferenced exact object, inactive stage lease if relevant | Intent -> same-filesystem quarantine rename -> completion | Resume/compensate owned locations; foreign occupants are preserved |
| restore | Exact quarantined object, original location absent | Intent -> rename back -> completion | Resume exact unfinished move; completed restore is historical |
| purge | Exact quarantined inventory, grace elapsed, extra deletion approval | Per-entry delete intent -> effect -> cursor; final receipt only after all | Irreversible; resume exact remaining work; no fabricated compensation |

### Global invariants and affected surfaces

| ID | Must hold after every model action and durable boundary | Consumers to audit |
| --- | --- | --- |
| I01 identity | Stable Skill/installation IDs; one content key has one coherent immutable definition; adoption occurrences do not conflict | Core plan/apply/retry/recover, batch admission/children, version and schema readers |
| I02 exact approval | Every effect has prior durable exact approval; plan bytes and IDs never change; noop is not an exemption | All core/batch/retention operations and invocation overrides |
| I03 authorized bytes | Usable selection names only active, intact, authorized selected bytes; H2 candidate cannot change H1 | Snapshot, verify, status, preflight, invocation, rollback and enable |
| I04 durable denial | Observed active failure/revoke persists for its grant; only explicit new grant can renew | Planning, apply admission, verify, retry, compensation, cached/fresh selection |
| I05 honest outcome | Completed receipt and bound metadata commit together; uncertain/partial work is not success | Core, batch, retention WAL and post-commit error handlers |
| I06 immutable history | Prior version/grant/receipt/verification/event facts survive legitimate later operations and inverses | Inspect, nested compensation, incident proof, override proof, retention root traversal |
| I07 recoverability | Every pending intent is discoverable with kind, owning project and ID; inspect is read-only; approved recovery terminates or identifies an external prerequisite/conflict | New plan/apply rejection, same/different/generated IDs, core/batch/retention recovery |
| I08 scoped context | Human and machine next actions retain the same state owner, including from another cwd and for unknown state | Status, preflight, invocation, incidents/investigation and error rendering |
| I09 ownership/isolation | Cooperating writers serialize; pending reservations prevent supersession; recovery preserves foreign targets | Same/different stores, parent-only batch WAL, move, stages and legacy guards |
| I10 reference safety | Active/retained/in-flight/evidence roots cannot be purged; expiration is explicit and separate | Shared versions, nested inverse batches, incidents, overrides, collect/restore/purge |
| I11 independent evidence | Missing, malformed, contradictory or interrupted evidence cannot imply usable approval; freshness is not silently extended | Core evidence readers, status and invocation; schema/runtime agreement |
| I12 compatibility | Legacy v1 keeps its meaning; new contracts, commands and packages remain aligned | Historical fixtures, machine schemas, docs, source/wheel/sdist and installed E2E |
| I13 separate capture authority | Capture preference, host hook trust, sensor health and Skill authorization cannot substitute for each other; warnings preserve command stdout/exit policy | Every lifecycle CLI entry, status, invocation, capture records and incident consumers |
| I14 observation ownership | Capture evidence binds task cwd/session/log root separately from the managed project; explicit persistence is atomic and idempotent, no arbitrary file ingestion or payload roots | Capture reader/writer, incident references, investigation schema and retention |

### Discovery and action context contract

`project_root` selects the owner and is preserved through status, nested
preflight/invocation status, investigation pages and error recommendations.
Commands with missing candidate/target inputs are templates, not executable
shell commands. Render concrete actions from argument lists with correct quoting;
do not alter historical record payloads merely to add navigation metadata.
Unknown state preserves the requested project but never invents a verified owner
or initializes a database through a diagnostic command.
An opened API manager does not acquire a new owner when its project path is
retargeted. Diagnostic and selection consumers check the bound state directory
and canonical project path; a mismatch fails closed without an executable next
action. This is a point-in-time check, not a lease preventing later external
filesystem changes or a project-migration protocol.

Validation is phase-specific. Reading historical intent must validate its exact
metadata without resolving today's candidate aliases. Retrying completed work
checks the current installation boundary, not a candidate that is no longer its
content source. Before durable snapshot readiness a new or resumed adoption
still requires its reviewed source; after readiness it uses the recorded intact
snapshot. A metadata-only reader is not a new-effect admission shortcut.

The existing installation list alone cannot discover an auto-ID install that
died before its first installation record. Add a bounded, read-only pending view
to the existing `lifecycle list` entry for core, batch and retention intents, with
kind, ID, state, plan ID, owning project and a read-only inspection action. It
must work after restart without prior knowledge of the generated transaction ID,
distinguish parent/child references, and avoid guessing or automatically executing
recovery. Missing/corrupt storage remains an error, not an empty healthy catalog.
Bounds apply to discovery record fetches and output pages. Historical proof
traversal may scan all metadata, and existing batch proof readers may load an
entire event stream; total I/O and memory are not globally bounded by page size.
This is the discovery half of the existing recovery contract, not a new daemon,
global registry, stage-scanning service or automatic remediation feature.

External recovery prerequisites include a readable store, intact selected
retained payload and, before durable snapshot readiness, the exact reviewed
source. A changed/missing source or foreign target must be reported with the
recorded recovery reference, not adopted silently. An internal metadata conflict
created by the protocol's own legitimate earlier steps is not an acceptable
external prerequisite. Pending-view inspection does not silently resume,
compensate, or offer unsupported recovery modes.

A saved plan is not mutation admission. Core or batch planning may still produce
a review artifact while earlier intent is unfinished; applying that artifact
must not supersede the pending work. Wherever planning/apply rejects a conflicting
attempt, it returns the original recovery reference. Retention additionally
rejects new planning while its earlier retention transaction is unfinished.

## 3. Model-driven sequence and crash testing

The test model owns expected transitions independently of production constants
or validators. Adapters call real APIs/CLI, while a shared oracle checks:

1. Expected stable identity, installation state, selected version and approval.
2. Actual target entry and payload bytes, or the exact allowed before/after
   alternatives during a journaled partial operation; candidate bytes unchanged.
3. Previously captured immutable records unchanged and events preserved in order.
4. Receipt/transaction/approval relationships and absence of false completion.
5. Every pending record has a usable project-scoped discovery/inspection path;
   chosen explicit recovery reaches the model result or preserves a modeled
   foreign conflict. Merely asserting that an exception occurred is insufficient.

Observing a partial active pointer can legitimately invalidate the old grant.
Tests must model that observation explicitly; the oracle must not accidentally
refresh status, grant approval, or mutate evidence while reading assertions.

| Family | Required sequence variations | Invariants |
| --- | --- | --- |
| S01 lifecycle | Install -> edit/update -> renew -> rename -> move -> disable -> enable -> archive -> enable -> rollback -> revoke -> renew -> uninstall -> install-retained; explicit clean legacy migration | I01–I06, I12 |
| S02 shared content | Same Skill/two installations -> independently saved same-H2 update/edit plans -> batch; reverse child order, same content from another source, interrupted first/second child, restart/resume and explicit compensation | I01, I02, I05–I07, I09–I10 |
| S03 inverses | Forward -> inverse -> inverse-of-inverse -> later legitimate action; inspect every ancestor and traverse retention roots at each step; interrupted inverse and missing-proof negative controls | I05–I07, I10–I11 |
| S04 denial | Candidate-only drift vs active damage -> observe -> restore bytes -> cached/fresh check -> explicit renewal; revoke while pending -> compensate; stale historical retry | I02–I04, I06, I09, I11 |
| S05 context | Outside cwd, project path with spaces, text and JSON status/preflight/invocation/investigation, unknown installation or missing repository; execute safe emitted commands and assert no wrong-project DB | I07–I08, I12 |
| S06 lost response | Real process exit after generated core/batch/retention WAL; new planning attempts and default/different-ID apply cannot supersede old work and rejected conflicts expose its reference; inspect then exact approved recovery | I02, I05, I07–I08 |
| S07 retention | Update/uninstall -> explicit evidence expiry/policy -> collect -> restore -> collect -> purge; active, shared, pending, unresolved incident and override roots; foreign occupancy and partial deletion | I05–I07, I09–I11 |
| S08 negative admission | No/wrong approval, altered content, conflicting shared identity, overlapping target, stale revision, unknown ID, malformed proof, reused ID/different plan | I01–I02, I05, I09, I11–I12 |
| S09 optional capture | Disabled / healthy / stale / unverified / error alongside valid / denied Skill state; outside task cwd; explicit evidence -> exact retry -> incident note -> investigation -> retention; malformed or incomplete evidence negative controls | I02–I05, I08, I10–I14 |

Crash points include core prepared/staged, step intent/staged/effect/completed,
before-commit/committed; batch prepared, child-started, child durable points,
child-recorded, before-commit/committed; verification started/observed; retention
prepared, object intent/effect/completed and purge delete intent/effect. Use real
subprocess exit at selected durable points, then reopen the same fixture store.
Controlled I/O injection complements, but does not replace, process-death tests.
The executable test inventory must state the exact covered families/boundaries,
not imply every possible scheduler interleaving or physical power loss is tested.
Exercise a declared, bounded set of legal state/operation transitions and their
negative counterparts; use deterministic traces with the action prefix and
invariant ID in assertion failures. Do not derive the reference transitions from
production operation tables or call production validation as the oracle. A model
that accepts perpetual blocking without checking the next legal action is not a
recoverability test.

## Completion gate

Each S-family must have a concrete automated entry and I-invariant assertions;
missing or indirect evidence remains incomplete. The four review findings must
have RED-before-fix evidence and independent closure, including sibling paths
identified by the same invariants. Public changes need installed CLI E2E, not
only in-process assertions. Run source tests, statement/branch coverage with the
unchanged floor, Markdown, wheel/sdist build/content, clean-wheel smoke, installed
E2E and final-head CI. Record remaining risks without claiming universal ACID,
continuous enforcement, authenticated actors or privileged-tamper resistance.

The [executable evidence ledger](lifecycle-model-verification.md) maps these
families to tests and records review gaps and final-gate status separately from
the requirements above.

Protected user research is outside this work and every fixture uses temporary
directories, not real installed Skills. Final evidence and continuation memory
stay in this project; the existing PR and dependency chain are reused.
