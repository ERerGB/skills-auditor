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

- Audit `~/.cursor/skills`, `~/.claude/skills`, or any custom skill root
- Repeat `--skills-dir` to audit/sync **Cursor + Claude Code** in one run
- Detect symlink health (`ok` / `broken`)
- Detect folder mode (`symlink` / `directory` / `file`)
- Audit discovery-layer collisions across multiple sources
- **Platform-tagged discovery profiles** (`cursor`, `claude-code`, or `*`): see below
- Build canonical injection preview with source priority (includes per-source platforms)
- Sync selected skills to canonical sources via mapping file
- **Optional `--target-platform` + `--discovery-profile`** on `sync` to skip skills whose canonical path lives under a source that does not allow that platform
- Safe replacement: existing directories are archived before relinking
- Default dry-run behavior

## Install

From a clone of this repo, use a virtualenv (recommended on macOS / PEP 668):

```bash
cd /path/to/skills-auditor
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .
```

This installs the **`skills-audit`** console script and enables **`python -m skills_auditor`**.

**Global CLI (optional):** `pipx install /path/to/skills-auditor` (or `pipx install .` from the repo).

**Without any install:** run **`python3 scripts/skills_audit.py`** from the repo root (it prepends the repo to `sys.path`).

## Tests

```bash
cd /path/to/skills-auditor
python3 -m unittest discover -s tests -v
```

## Quick Start

```bash
cd /path/to/skills-auditor

# 1) Audit current status
skills-audit audit \
  --skills-dir "$HOME/.cursor/skills"

# 2) Plan sync (dry-run)
skills-audit sync \
  --skills-dir "$HOME/.cursor/skills" \
  --map-file config/sources.example.json

# 2b) Same mapping, both Cursor + Claude Code global roots (dry-run)
skills-audit sync \
  --skills-dir "$HOME/.cursor/skills" \
  --skills-dir "$HOME/.claude/skills" \
  --map-file config/sources.example.json

# 3) Discovery-layer audit (multi-source, dedupe preview)
skills-audit audit-discovery \
  --source "$HOME/.cursor/skills" \
  --source "$HOME/.cursor/skills-cursor" \
  --source "/path/to/your/project/.cursor/skills"

# 4) Apply sync
skills-audit sync \
  --skills-dir "$HOME/.cursor/skills" \
  --map-file config/sources.example.json \
  --apply
```

## gstack fork (`plan-ux-review`)

If you use a [gstack](https://github.com/garrytan/gstack) fork that adds `plan-ux-review/` (for example `https://github.com/ERerGB/gstack`):

- **Discovery:** use `config/discovery-profile.gstack-fork.example.json`, or `config/discovery-profile.cursor-jz.example.json`.
- **Sync dry-run (Claude only, skip Cursor-only sources in the profile):**
  `skills-audit sync --skills-dir ~/.claude/skills --map-file config/sources.gstack-fork.example.json --discovery-profile config/discovery-profile.cursor-jz.example.json --target-platform claude-code`

## Discovery profile format

`audit-discovery --profile-file` and `sync --discovery-profile` accept JSON with:

- `sources`: ordered list. Each entry is either a **string** (path, treated as platform `["*"]` — sync/apply to all) or an **object** `{ "path": "~/.cursor/skills-cursor", "platform": ["cursor"] }`.
- `exclude_sources`: path strings (unchanged).
- `collapse_identical`: boolean (unchanged).

Known platform labels (convention): `cursor`, `claude-code`. Use `"*"` inside `platform` to mean “all targets.”

`audit-discovery` without `--profile-file` uses default roots and **infers** platforms (e.g. `~/.claude/skills` → `claude-code`, `skills-cursor` → `cursor`, shared `~/.cursor/skills` → both).

Design note: platform metadata lives in the **profile** (management layer), not in each `SKILL.md`. See [issue #1](https://github.com/ERerGB/skills-auditor/issues/1).

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

After the main table, **`audit` always runs a duplicate `name:` check** (unless `--skip-duplicate-name-check`): for each top-level bundle folder under the skills root, it scans nested `SKILL.md` files and reports when the same frontmatter `name:` appears more than once (common with gstack’s `.agents` / `.factory` copies — can surface as multiple `/gstack` in an IDE). Use `--fail-on-duplicate-names` to exit with code **4** when any bundle has duplicates (for CI).

```bash
skills-audit audit --skills-dir "$HOME/.cursor/skills"
skills-audit audit --skills-dir "$HOME/.claude/skills" --fail-on-duplicate-names
```

### `sync`

Compare current state with expected sources, then propose/apply relinking.

```bash
skills-audit sync \
  --skills-dir "$HOME/.cursor/skills" \
  --map-file config/sources.example.json
```

Use `--apply` to execute changes.

**Platform-aware sync:** when you use the same map against both Cursor and Claude Code trees, pass the same discovery profile and `--target-platform cursor` or `claude-code`. Entries whose map target path falls under a profile source that does not list that platform are reported as `skip_platform` (no symlink changes).

```bash
skills-audit sync \
  --skills-dir "$HOME/.claude/skills" \
  --map-file config/sources.example.json \
  --discovery-profile config/discovery-profile.cursor-jz.example.json \
  --target-platform claude-code
```

### `audit-discovery`

Inspect discovery-layer behavior across multiple skill sources and output:

- all discovered candidates
- same-name collision groups
- canonical selection by source priority
- final injection preview

```bash
# default sources: ./.cursor/skills, ~/.cursor/skills, ~/.cursor/skills-cursor,
#   ./.claude/skills, ~/.claude/skills
skills-audit audit-discovery

# explicit priority (first source wins conflicts)
skills-audit audit-discovery \
  --source "$HOME/.cursor/skills" \
  --source "$HOME/.cursor/skills-cursor" \
  --source "/path/to/your/project/.cursor/skills"

# profile-driven discovery (recommended)
skills-audit audit-discovery \
  --profile-file config/discovery-profile.cursor-jz.example.json

# CI-friendly summary
skills-audit audit-discovery \
  --profile-file config/discovery-profile.cursor-jz.example.json \
  --summary-only

# Fail when unresolved conflicts remain
skills-audit audit-discovery \
  --profile-file config/discovery-profile.cursor-jz.example.json \
  --fail-on-conflict \
  --fail-on-hash-conflict

# exclude noisy paths and keep same-hash folding on
skills-audit audit-discovery \
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
