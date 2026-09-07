# Security and dependency review

skills-auditor is a local filesystem tool. Its main risk is applying a stale or misunderstood plan
to a valuable install root.

## Default posture

- `integrate` writes a local plan but does not change source or target entries.
- `apply` accepts only a versioned plan and verifies its content checksum, full source-tree hashes, and
  affected target-entry snapshots before writing.
- `verify` checks receipt-scoped links and source-tree hashes, then reports whether the associated
  approval remains valid or requires re-approval.
- Primitive repair, dedup, route, and sync commands remain plan-first.
- The agent skill is plan-first; explicit apply authorization is required.
- Route defaults to archive. Delete requires explicit strategy and apply authorization.

## Legacy live-link boundary

Native target entries are timestamp-archived before integration links replace them. Replacing an
incorrect symlink does not archive its former target because the target content is not owned by the
install entry.

Apply checks all preconditions before the first write and again before each action. Filesystem
operations across several target roots are not globally atomic. A mid-run failure produces a
failed receipt when possible; preserve it for repair and handoff.

Installed entries are live symlinks, not immutable copies. A source edit after apply is immediately
visible to the host; `verify` detects that drift but cannot prevent consumption between checks.
Pin or protect canonical checkouts when runtime immutability matters.

Integration approval is version-bound and renewable, not permanent trust. Only a completed receipt
whose target links and source-tree hashes still match retains a valid approval state. A failed
receipt or any verification drift invalidates that approval; generate and review a new plan for the
current state before explicitly approving another apply. This status does not prove that the Skill's
behavior is safe or benign.

Plan IDs and receipt IDs are content checksums, not signatures. Protect plan files with the same
access controls as the target roots they authorize.

Legacy verification is stateless: restoring the original source and target can make an old receipt
valid again. It is not a sticky revocation journal. The managed lifecycle uses a distinct contract.

## Managed lifecycle boundary

[Managed installations](managed-lifecycle.md) separate candidate trees from normalized,
content-addressed snapshots. Source edits do not flow through the installed pointer. Filesystem
read-only bits reduce accidental writes but do not prevent the same user or a privileged process
from tampering; use fresh verification before consumption.

Managed authorization is durable and bound to a version, target and installation generation.
An observed failure invalidates it; restored bytes alone cannot renew it. A verification-run marker
prevents an interrupted observation from being presented as a fresh healthy cache. Derived status
publication cannot roll back an already committed denial.

An individual symlink replacement is atomic on the supported local POSIX filesystem. SQLite
state and pointer effects are separate durable operations: write-ahead step records make their
interruption inspectable and explicitly recoverable, not globally ACID. Power-loss guarantees
depend on the filesystem honoring synchronization operations. Persistent advisory locks protect
cooperating writers, not arbitrary external programs; do not remove lock files to force progress.

Disable, archive and uninstall remove owned pointers, not their payload history. Recovery preserves
foreign state and does not resurrect revoked grants. No operation proves semantic safety or
identical runtime behavior. Status icons and blocking work only in hosts that actually integrate
the preflight contract; there is no automatic enforcement in unrelated agents or editors.

Use-time overrides waive only a reviewed stale-evidence interval, never failed integrity,
revocation, unknown state or a different version. They require explicit approval, bounded expiry
and a durable use audit. A selection is not a file lease or a guarantee about the next read.

Retention expiry releases historical payload references, not the records themselves. Quarantine
is recoverable storage relocation; permanent purge requires a separate reviewed plan and explicit
irreversible-deletion gate. Active references, incomplete transactions and live staging leases
prevent collection. Do not manually remove the store or its synchronization files.

Legacy managed-data guards recognize only bounded existing project/registry context. They do not
scan every project on the machine and cannot recognize every unrelated drifted ordinary file.
They are accidental-cross-boundary protection, not an operating-system sandbox.

## Network and local data

The integration transaction does not require network access. `drift-check` and `audit --with-drift`
fetch Git remotes. Trigger, sensor, trace, plan, receipt, and ledger data remain local by default.

`.skills-auditor-local/` is gitignored. Route traces default to `~/.skills-auditor/traces/`.
Sensor facts such as file access do not prove semantic skill use.

Managed incident packets allowlist bounded evidence and paginated events. They do not collect
prompt bodies, environment dumps or arbitrary referenced file contents. User-authored notes and
local paths can still contain private information; inspect exports before sharing. Actor/tool
labels are local attribution, not authenticated identity, and checksums are not signatures.

## Dependencies and license

The runtime uses the Python standard library. Build tooling is declared in
[`pyproject.toml`](../pyproject.toml). The project is distributed under the
[`MIT License`](../LICENSE).

## Adoption checklist

- Keep canonical source roots and target install roots disjoint; target roots must not overlap.
- Keep each skill payload self-contained; integration rejects source symlinks that escape its tree.
- Review the saved plan rather than terminal text alone.
- Apply the reviewed plan path; do not rebuild it implicitly.
- Preserve receipts in controlled automation.
- Verify after apply and before handoff.
- Keep delete out of unattended workflows.

Report security or trust concerns through
[GitHub issues](https://github.com/ERerGB/skills-auditor/issues).
