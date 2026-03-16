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
- Audit discovery-layer collisions across multiple sources
- Build canonical injection preview with source priority
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

# 3) Discovery-layer audit (multi-source, dedupe preview)
python3 scripts/skills_audit.py audit-discovery \
  --source "$HOME/.cursor/skills" \
  --source "$HOME/.cursor/skills-cursor" \
  --source "/Users/j.z/code/fulmail/.cursor/skills"

# 4) Apply sync
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

### `audit-discovery`

Inspect discovery-layer behavior across multiple skill sources and output:

- all discovered candidates
- same-name collision groups
- canonical selection by source priority
- final injection preview

```bash
# default sources: ./.cursor/skills, ~/.cursor/skills, ~/.cursor/skills-cursor
python3 scripts/skills_audit.py audit-discovery

# explicit priority (first source wins conflicts)
python3 scripts/skills_audit.py audit-discovery \
  --source "$HOME/.cursor/skills" \
  --source "$HOME/.cursor/skills-cursor" \
  --source "/Users/j.z/code/fulmail/.cursor/skills"

# profile-driven discovery (recommended)
python3 scripts/skills_audit.py audit-discovery \
  --profile-file config/discovery-profile.cursor-jz.example.json

# CI-friendly summary
python3 scripts/skills_audit.py audit-discovery \
  --profile-file config/discovery-profile.cursor-jz.example.json \
  --summary-only

# Fail when unresolved conflicts remain
python3 scripts/skills_audit.py audit-discovery \
  --profile-file config/discovery-profile.cursor-jz.example.json \
  --fail-on-conflict \
  --fail-on-hash-conflict

# exclude noisy paths and keep same-hash folding on
python3 scripts/skills_audit.py audit-discovery \
  --source "$HOME/.cursor/plugins" \
  --exclude-source "$HOME/.cursor/plugins/cache"
```

Discovery report includes:

- `total_candidates`: all same-name hits
- `effective_candidates`: after same-hash folding
- `collapsed_identical`: number of folded duplicates
- `hash_conflict`: same-name but different content hash (high risk)

CI flags:

- `--summary-only`: print compact counters only
- `--fail-on-conflict`: non-zero exit if duplicates remain
- `--fail-on-hash-conflict`: non-zero exit if same-name hash conflicts exist

## Recommended Profile

See `config/discovery-profile.cursor-jz.example.json`:

- Includes user, built-in, project, and plugin roots
- Excludes plugin cache to reduce duplicate noise
- Keeps `collapse_identical=true` for stable canonical preview

## Safety Notes

- No destructive action in default mode.
- Existing non-symlink entries are archived as:
  - `<name>.backup-YYYYmmdd-HHMMSS`
- Existing symlinks are unlinked and recreated only in `--apply` mode.

## License

MIT
