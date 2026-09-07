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
