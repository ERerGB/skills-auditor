# Managed lifecycle and optional Skill Trace alignment

This is the bounded compatibility increment for Issue #19 / PR #20 after
main incorporated PR #21 (`14e6641`). It supplements the
[lifecycle state model](lifecycle-state-model.md), not its authorization rules.

## Independent authorities

| Dimension | Authority | Effect on managed use |
| --- | --- | --- |
| Version integrity and Skill grant | Managed verification and explicit approved plans | Existing proceed / warn / block policy remains authoritative |
| Permission to execute plugin hooks | Host's reviewed hook definitions | External and unverified here; never inferred from capture health |
| Optional capture preference | User setting or explicit process override | Enables this plugin's observation only; never grants Skill permission |
| Capture health | Recent matching task-local sensor metadata | Advisory observation; missing or failed capture does not revoke a Skill grant |

Healthy capture is neither semantic Skill-use proof nor current hook-trust
attestation. A blocked Skill stays blocked even with healthy capture. Disabled,
stale, unverified or erroneous capture never authorizes or invalidates a Skill.
No command in this increment changes capture settings, host hook trust or a
host-wide hook feature flag.

## Ownership and entry points

The task working directory, explicit managed project root, and sensor log root
are separate identities. Relative sensor paths are interpreted from task cwd,
as in PR #21; acting on project A from task B does not relocate B's logs to A.
Diagnostic output records both contexts and does not claim that task B's hook
events prove a Skill in project A was read.

Lifecycle commands get the same warning-only capture preflight as ordinary
commands, including the early console dispatch. It executes once, leaves
stdout machine-readable and preserves the operation's exit status. Invalid
arguments and help retain their parser behavior. Standalone health checks stay
read-only and never initialize a managed repository.

## Explicit diagnostic evidence

An explicit `lifecycle capture-evidence` command records a bounded, allowlisted
health snapshot in the selected managed repository. It does not copy raw sensor
lines, tool inputs/outputs, prompts, arbitrary error prose or referenced files.
The snapshot records observation time, task cwd/session, managed project context,
log root, preference source, health state and observed hook timestamps. Host
trust is always marked unverified. Paths and identifiers can be private.

The record is immutable through this API. A caller-supplied evidence ID is a
retry key: the same scoped request returns the original observation without
resampling; a changed request conflicts. Record and completion event commit in
one SQLite transaction. Failed writes cannot create successful evidence. Readers
validate shape, project ownership, revision and matching append-only event;
checksums are not signatures or protection against a malicious database owner.

Existing `append-note --evidence-ref capture-evidence:ID` explicitly associates
the record with an incident. Investigation exposes only validated local
diagnostic records, not paths to follow. Such evidence cannot resolve an incident
as remediated, grant authorization, or substitute for verification. It holds no
version/payload references of its own; an incident's existing retention policy
still governs its Skill payload roots. Retention must accept and validate this
new reference kind without promoting it into an authorization or payload root.

## Acceptance and exclusions

Tests precede implementation. Cover disabled / healthy / unverified / stale /
error capture alongside valid and denied managed states; all console aliases;
task B acting on project A; warning-only stderr and unchanged JSON/exit status;
no preference/trust/log writes; explicit persistence, exact retries and changed
requests; rollback on event/record failure; malformed/missing/mismatched durable
evidence; incident investigation and retention compatibility. Add isolated
installed-wheel E2E, not only in-process mocks.

Review the complete combined tree, then run source tests, statement and branch
coverage with the unchanged floor, Markdown checks, wheel/sdist content and byte
checks, clean-wheel smoke and installed CLI E2E. Publish evidence for the exact
remote SHA and terminal Actions runs on the existing PR. No PR merge or manual
issue close.

Excluded: UI icons, automatic permission repair, automatic capture enablement,
continuous enforcement, semantic safety proofs, a new mandatory hook gate, raw
log ingestion/search, automatic version rollback or a new background service.
