# Troubleshooting

## `skills-audit` is not found

Activate the virtual environment or run from the checkout:

```bash
source .venv/bin/activate
python3 scripts/skills_audit.py --help
```

## Integrate reports `missing_sources` or `missing_targets`

Pass both sides explicitly:

```bash
skills-audit integrate --source .agents/skills --target codex
```

Or add `sources` and `targets` to a repository `skills-auditor.json`.

## Integrate reports `target_inside_source`

The high-level transaction rejects a target contained by its canonical source root. Use a precise
source such as `.agents/skills`, not the entire repository.

`source_inside_target` is the inverse: a canonical source sits inside an install root and could be
mistaken for an entry to replace. Move the canonical tree outside that namespace. If
`overlapping_targets` is reported, choose target roots that do not contain each other.

## Apply reports `stale_plan`

A reviewed source-tree hash, affected target entry, or reserved archive destination changed. If the
initial preflight rejected the plan, no apply actions started. The same error can also arise during
a later per-action check, after earlier actions completed. Preserve any failed receipt and audit
the plan's target roots and archive paths before generating and reviewing a new plan:

```bash
skills-audit integrate --config skills-auditor.json
```

Do not edit `plan_id` to bypass the check.

## Apply reports a failed receipt

Some earlier actions may have completed before an I/O failure or a later `stale_plan` check. They
are not automatically undone. Preserve the failed receipt and original plan, inspect `results`,
and audit every target root and reserved archive in the plan before preparing a new plan.

`results` contains only actions that completed and passed their post-operation checks. It is not a
write-ahead journal: an empty list does not prove that no filesystem changes occurred, and an
unlisted action may have changed its target before failing verification. Receipt-write failures
and process interruption may leave no new receipt at all. Do not blindly retry or remove a
concurrent user's entry; establish the actual source, link, and archive state first.

## Verify fails

Read failed checks from JSON:

```bash
skills-audit verify <receipt.json> --format json
```

`target_link` means the installed entry moved or was replaced. `source_tree` means the canonical
skill tree changed after apply. Generate a new plan instead of editing the receipt.

## Metadata validation fails

Preview supported repairs:

```bash
skills-audit metadata-repair --platform codex --skills-dir .agents/skills
```

Unsupported malformed frontmatter requires manual correction.

## A symlink is broken

Check whether the canonical repository moved, then rerun audit:

```bash
skills-audit audit --skills-dir "$HOME/.cursor/skills"
```

## Repository dirty count looks surprising

`dirty_count` is repository-wide. `skill_dirty_count` is scoped to the resolved skill directory,
so unrelated monorepo changes can leave the skill itself clean.

## Exit codes

For `integrate`, `apply`, and `verify`, `0` means command success, `2` means invalid input, and `3`
means a contract failure. Legacy primitives retain their documented command-specific gates.

## A managed installation is warning, blocked or unknown

Use the owning project and stable installation ID, not a guessed current name:

```bash
skills-audit lifecycle --project-root /project status INSTALLATION_ID --format json
skills-audit lifecycle --project-root /project preflight INSTALLATION_ID --format json
```

Status reads cached evidence; preflight verifies afresh by default. A warning about stale
evidence is not a new approval. Missing state is reported as unknown, never silently initialized
as a healthy installation. Managed exit `4` means stale plan or stale-evidence warning; `3`
means blocked or failed. These codes do not replace legacy command-specific exits.

| Evidence | Next investigation |
| --- | --- |
| `snapshot_tree` | Compare the expected/actual hash and read error; do not edit the stored snapshot in place |
| `target_link` | Inspect expected versus actual target state; preserve another writer's entry |
| `receipt_record`, `transaction_record`, `grant_binding` | Inspect the linked historical records and current authorization; do not fabricate a replacement receipt |
| `verification_incomplete`, `status_invalid`, `never_verified` | Inspect prior verification/transaction evidence, then obtain a fresh completed verification |
| `lock_contended` | Identify the cooperating writer; do not delete its lock file to force a retry |

Restoring a managed target's bytes alone does not restore approval. Renew or roll back only
through a newly reviewed, explicitly approved plan. If the target is foreign, resolve its
ownership before planning a change; automatic overwriting is intentionally refused.

## A managed transaction stopped partway through

```bash
skills-audit lifecycle --project-root /project inspect transaction TRANSACTION_ID
skills-audit lifecycle --project-root /project recover TRANSACTION_ID --mode inspect
```

Inspect individual steps and current paths before choosing an explicitly approved resume or
compensation. Earlier pointer changes may already have occurred without a completed receipt.
An old successful receipt is historical evidence, not a shortcut around a later invalidation
or changed installation generation. A completed transaction requires a new inverse plan rather
than unfinished-transaction compensation. See [managed recovery](managed-lifecycle.md#failure-and-recovery).
