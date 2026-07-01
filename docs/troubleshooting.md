# Troubleshooting

Most first-run issues are environment activation, command path, metadata validation, or sync-plan review issues.

## `skills-audit` is not found

Activate the virtualenv:

```bash
source .venv/bin/activate
```

Or run the script directly from the repo root:

```bash
python3 scripts/skills_audit.py audit --skills-dir "$HOME/.cursor/skills"
```

## Metadata validation fails

Run metadata repair without `--apply` first:

```bash
skills-audit metadata-repair \
  --platform codex \
  --skills-dir "$HOME/.codex/skills"
```

Read the proposed changes before writing them.

## Sync would replace a directory

Stop and inspect the dry-run output. skills-auditor archives existing directories before relinking, but this is still a filesystem change that should be reviewed.

## A symlink is reported as broken

Check whether the canonical source repository moved or whether the target root was copied between machines.

```bash
ls -la "$HOME/.cursor/skills"
```

Then audit the root again after fixing the source path.

## Repository dirty count looks surprising

`dirty_count` is repository-wide. `skill_dirty_count` is scoped to the resolved skill directory. In monorepos, unrelated dirty files can make the repo dirty while the skill itself remains clean.

## Need a new recipe

Open a use-case issue:

https://github.com/ERerGB/skills-auditor/issues/new?title=%5BUse%20case%5D%20
