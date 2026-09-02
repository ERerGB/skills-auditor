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

## Filesystem boundary

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

## Network and local data

The integration transaction does not require network access. `drift-check` and `audit --with-drift`
fetch Git remotes. Trigger, sensor, trace, plan, receipt, and ledger data remain local by default.

`.skills-auditor-local/` is gitignored. Route traces default to `~/.skills-auditor/traces/`.
Sensor facts such as file access do not prove semantic skill use.

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
