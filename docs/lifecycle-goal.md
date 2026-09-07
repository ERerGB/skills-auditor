# Skill lifecycle and transaction management — executable goal

Status: active design and implementation goal for Issue #19 and follow-ups
#16–#18; not a claim of shipped capability.

## Goal prompt

Complete the identified Skill lifecycle and transactional-governance gaps in the
Skills Auditor project. Reuse Issue #14 / PR #15 and Issues #16, #17 and #18. Keep
the small legacy verification change reviewable; deliver the new managed
lifecycle in explicitly linked, dependency-ordered work without merging any PR
or manually closing any issue. Deduplicate missing transaction-foundation work
before opening another issue. Keep implementation, test evidence and continuation
memory in this repository's context.

Success means a human or Agent can identify a Skill, review a candidate version,
explicitly authorize a bounded change, know which exact approved bytes an
installation exposes, and inspect/recover every interrupted mutation without
mistaking a partial operation for success. A warning must offer both an immediate
next action and durable investigation context. Editing a candidate must not
silently alter the active approved version.

Implement all requirements below with tests first, independent worker/reviewer
closure, shipped machine contracts and installed-CLI coverage. Do not merely
write a roadmap and mark this goal complete.

## Architecture and policy decisions

1. Legacy `integration/v1` keeps its current stateless observation semantics:
   H1 -> H2 -> H1 may verify valid again with the old receipt. Document that this
   is not a durable revocation log. Do not change a historical schema's meaning
   to smuggle in the new state machine.
2. New managed lifecycle records separate identity, candidate, active version,
   installation state, authorization, integrity observation and freshness.
   Candidate H2 awaiting approval does not revoke an intact, authorized active
   H1 snapshot. An observed failure of the active snapshot/target invalidates its
   grant durably; restored bytes alone never clear invalidation or revocation.
   A new explicitly approved, completed and verified transaction is required.
3. One installation's old/new snapshot pointer changes atomically. A batch or
   cross-host move is a recoverable, compensatable sequence, not global ACID.
   Logs describe partial completion and recovery-needed states honestly.
4. Local files are not signatures. Local actor/tool labels are audit attribution,
   not authenticated identity. No semantic-safety, sandbox, network registry,
   privileged-tamper prevention or continuous enforcement claim is allowed.
5. Core returns a host-neutral status/preflight contract and prominent CLI text.
   Icons are rendered only by adapters that actually integrate that contract.
   A host that bypasses preflight is not claimed to be blocked.

## Acceptance matrix

| ID | Required outcome | Automated proof |
| --- | --- | --- |
| L01 | Stable Skill and installation IDs survive rename/move; versions have content identity, parent lineage and provenance. | Rename/move/restart retains identity; duplicate name is not duplicate identity; invalid transitions rejected. |
| L02 | Distinct candidate, active, disabled, archived and uninstalled state; explicit durable grant, invalidation and revocation. | H1 active/H2 candidate; H1 corruption then byte restoration still requires renewal; revoke cannot be cleared by verify or retry. |
| L03 | All managed mutations use saved plan, review, explicit approval, execution and verification. | Install/update/edit/move/disable/enable/archive/uninstall and rollback; no approval, mismatched approval, changed source/target/revision and noop paths. |
| L04 | Immutable content-addressed snapshots contain only approved bytes; source and normalized snapshot hashes recorded separately where permissions differ. | Source mutation leaves target bytes H1; exact copy validation before activation; duplicate content reuse; chmod semantics; relative links work, absolute/escaping/looping links and special files rejected. |
| L05 | Atomic pointer activation and conservative legacy migration. | Process death before/after replace leaves old/new complete snapshot; clean legacy receipt migrates explicitly; invalid legacy state does not bless current bytes. |
| L06 | Write-ahead, durable transaction and per-step evidence exist before effects. | Crash at every durable boundary, restart inspection, failed journal/receipt writes, uncertain post-write outcome, partial completion and failed compensation preserve evidence. |
| L07 | Recovery is explicit, idempotent and preserves concurrent third-party changes. | Resume/compensate from each phase; reject foreign target occupancy; repeated recovery and conflicting plans; no false completed receipt. |
| L08 | Cooperating writers serialize across overlapping targets, even from separate project stores; approvals/retries bind exact plan and expected revision. | Two subprocesses, same/different stores, same target, duplicate transaction IDs, changed payload under same ID; lock release after process death. |
| L09 | Durable status/v1 separates approval, freshness and severity; unknown or unreadable state never silently valid. | Fresh/stale/future/unknown/corrupt states; atomic publish failures; prominent human output and deterministic proceed/warn/block contract. |
| L10 | Durable incident/events supply an Agent investigation entry with expected/actual evidence, references, timestamps and append-only notes. | Deduplicated repeated signature, distinct signature, restart, corrupt write, bounded packet, private fields excluded, actor/tool attribution. |
| L11 | Incident resolution is evidence-backed or an explicit non-remediation disposition. | Reapproval plus clean completed verification resolves by reference; source restoration alone does not; history survives. |
| L12 | Invocation policy selects only explicitly authorized snapshots; override is explicit, bounded and auditable. | Last-known-good, strict fail-closed, missing/corrupt active content, expired/wrong-version override; no fallback to candidate. |
| L13 | Retention is reference-aware and plan-first; evidence expiration is separate from object deletion. | Active, retained, receipt/incident referenced and in-flight snapshots protected; dry-run/recheck; no broad recursive deletes. |
| L14 | Legacy primitives cannot unintentionally mutate recognized managed snapshots or entries. | Guard legacy sync/repair/route/dedup writes; nonmanaged legacy behavior stays compatible; explicit migration guidance. |
| L15 | New schemas/readers, help, package contents and compatibility guidance ship together. | Historical v1 fixtures remain accepted; malformed new records fail closed; schema examples, clean-wheel and CLI E2E. |
| L16 | PR15 documents and tests partial completion without claiming WAL or sticky invalidation. | Earlier action succeeds then later I/O/stale_plan failure; exact targets, concurrent data, failed receipt and remaining untouched targets asserted. |

## Implementation contract

- Use a separate `skills_auditor.lifecycle` namespace rather than repurposing
  the short-lived routing `VariantState` machine or growing legacy integration
  into a second incompatible API.
- Prefer Python standard-library durable storage and advisory locking on
  supported Linux/macOS filesystems. Explain filesystem and lock assumptions;
  fail clearly on unsupported backends instead of claiming universal safety.
- Store mutation intent, planned before/after states and unique transaction ID
  durably before an external filesystem effect. A completed receipt is a
  postcondition artifact, never the only execution journal.
- Revalidate expected source/target/registry generation before effects and before
  final publication. Recovery compares planned before/after/foreign states.
  Never overwrite a foreign state to make rollback appear successful.
- New active data is staged, hashed, synchronized and published before pointer
  replacement; old snapshot remains retained. Local read-only protection is
  best effort and does not replace integrity verification.
- Reject managed source/target/store overlap, traversal, unsupported file types,
  and unsafe symlink topology. Preserve executable/read bits intentionally.
- `edit` governs a replacement candidate tree; it does not promise transactional
  interception of arbitrary external editors. `move` preserves installation
  identity and records destination activation and source removal separately.
- Disable/archive/uninstall remove only owned installation pointers and retain
  recovery/history records. Deleting payload/evidence is a separate reviewed GC
  plan. Re-enable and rollback require explicit approval and valid retained bytes.
- Preserve failed/partial transactions even if best-effort compensation succeeds.
  Crash recovery must not silently grant a new authorization.
- Status and incident readers must not turn parse/read errors into empty healthy
  state. Persist only bounded, necessary evidence; never collect prompt bodies,
  environment dumps or credentials by default.

## Delivery ordering

1. Finish the bounded #14/#15 partial-completion and v1 semantics review, run the
   full validation and update the same PR.
2. Introduce a deduplicated foundation issue for the new identity, authorization,
   transaction/recovery and managed-operation contracts. Record the canonical
   scope matrix there, referring to #16 status, #17 incidents and #18 snapshots.
3. Implement the foundation and snapshot activation, then status and incident
   consumers, completing the full end-to-end lifecycle and legacy guards.
   Keep dependency branches/PRs explicit while #15 awaits human review.
4. Run independent review and resolve findings; run all full validation gates
   again on each final published head. Update Issue/PR evidence and resource
   ledger. Leave PRs open for normal review and merge.

## Verification and safety boundaries

Run source tests with measured statement AND branch coverage (fresh baseline,
whole project and key modules), Markdown checks, wheel/sdist build and content
checks, dependency checks, clean-wheel smoke and installed CLI E2E. Include real
subprocess death and concurrency tests in addition to controlled exceptions.
Do not lower the existing coverage floor, remove assertions or exclude business
code to make numbers pass. Record significant remaining untested risks.

All product mutation exercises use temporary fixture directories, never the
user's installed Skills. Protect `doc/plugin-ecosystem-survey.md`: do not read,
copy, overwrite, delete, stage or commit it. Keep runtime ledgers, build outputs
and investigation data ignored. Do not bypass hooks or force-push. Never merge
PRs or manually close issues. Do not invent commits if review finds no change.

Before completion verify exact local/remote SHA equality, each PR's open state
and terminal Actions conclusion for that SHA, and publish reproducible evidence.
Record which tests were added, real defects fixed, coverage fractions, remaining
risks, related PRs and the safe review/merge handoff. Only then complete the goal.
