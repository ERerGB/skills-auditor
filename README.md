# skills-auditor

One-command audit and sync for local AI skill folders (Cursor, OpenClaw, etc.).

## Why this exists

Skill folders tend to drift over time:

- broken symlinks after moving repositories,
- copied folders falling behind upstream sources,
- mixed local folders and refs with unclear ownership.

This repo provides a safe workflow:

1. **Audit** current state.
2. **Plan** sync actions in dry-run mode.
3. **Apply** only when explicitly approved.

## Features

- Audit `~/.cursor/skills` (or custom skill root)
- Detect symlink health (`ok` / `broken`)
- Detect folder mode (`symlink` / `directory` / `file`)
- Sync selected skills to canonical sources via mapping file
- Safe replacement: existing directories are archived before relinking
- Default dry-run behavior

## Quick Start

```bash
cd /Users/j.z/code/skills-auditor

# 1) Audit current status
python3 scripts/skills_audit.py audit \
  --skills-dir "$HOME/.cursor/skills"

# 2) Plan sync (dry-run)
python3 scripts/skills_audit.py sync \
  --skills-dir "$HOME/.cursor/skills" \
  --map-file config/sources.example.json

# 3) Apply sync
python3 scripts/skills_audit.py sync \
  --skills-dir "$HOME/.cursor/skills" \
  --map-file config/sources.example.json \
  --apply
```

## Mapping File

`sync` uses a JSON map from skill name to canonical source directory:

```json
{
  "arch-review": "/Users/j.z/code/dev-doc-governance/skills/arch-review",
  "auto-doc-index": "/Users/j.z/code/dev-doc-governance/skills/auto-doc-index"
}
```

Rules:

- Target path must exist.
- Target must contain `SKILL.md`.
- Missing targets are reported as errors and skipped.

## Commands

### `audit`

Inspect the current skill root and print a table + JSON summary.

```bash
python3 scripts/skills_audit.py audit --skills-dir "$HOME/.cursor/skills"
```

### `sync`

Compare current state with expected sources, then propose/apply relinking.

```bash
python3 scripts/skills_audit.py sync \
  --skills-dir "$HOME/.cursor/skills" \
  --map-file config/sources.example.json
```

Use `--apply` to execute changes.

## Safety Notes

- No destructive action in default mode.
- Existing non-symlink entries are archived as:
  - `<name>.backup-YYYYmmdd-HHMMSS`
- Existing symlinks are unlinked and recreated only in `--apply` mode.

## License

MIT
