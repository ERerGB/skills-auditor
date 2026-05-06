# skills-auditor

One-command audit and sync for local AI skill folders (Cursor, OpenClaw, etc.).

**Cursor / agent skill pack:** root [`SKILL.md`](SKILL.md) is the **top entry** (default full pipeline with **`--apply`** on dedup/route/sync unless the operator asks for dry-run or sets `SKILLS_AUDITOR_DRY_RUN=1`); layered sub-skills live under [`skills/`](skills/README.md). Optional env: [`config/skills-auditor.pipeline.example.env`](config/skills-auditor.pipeline.example.env).

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
- CLI default dry-run until `--apply`; Cursor top skill [`SKILL.md`](SKILL.md) defaults to apply unless dry-run

### Drift and dirty counts (`audit --with-drift`)

- **`dirty_count` (repo):** number of `git status --porcelain` lines for the **whole** repository backing the skill path (same as before).
- **`skill_dirty_count`:** same count **scoped** to the resolved skill directory via `git status --porcelain -- <path>`.
- **Monorepos:** if only *other* paths in the repo are dirty, the audit table shows `skill_clean (repo_dirty=N)` instead of looking like that skill folder was edited. When the skill tree itself has changes, both `repo_dirty` and `skill_dirty` appear in `drift(...)`.

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

After the main table, **`audit` always runs a duplicate `name:` check** (unless `--skip-duplicate-name-check`): for each top-level bundle folder under the skills root, it scans nested `SKILL.md` files and reports when the same frontmatter `name:` appears on **more than one resolved file** (symlinks to the same canonical `SKILL.md` count once — avoids false positives for DRY symlink layouts). Common real duplicates: gstack’s `.agents` / `.factory` copies vs primary trees. Use `--fail-on-duplicate-names` to exit with code **4** when any bundle has duplicates (for CI).

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

### `dedup`

Detect duplicate frontmatter `name:` across **all** nested `SKILL.md` files under an install root (the same scope Slash and similar hosts use when listing `/` skills), then replace non-canonical copies with relative symlinks to the shortest-path canonical file.

**Hash-aware dedup (v0.5.0):** `dedup` now compares file content hashes before acting:

- **Same hash → `relink`**: True duplicate, safe to replace with symlink.
- **Different hash → `skip_multi_version`**: Host-specific variant (e.g. Codex-trimmed copy), preserved intact. The output reports the inferred platform (see Convention-based inference below).

This prevents blindly symlinking files that intentionally differ for different platforms (e.g. gstack's `.agents/skills/` copies are trimmed for Codex).

This is the **recommended best practice** for skill packs that ship mirror directories (e.g. gstack's `.agents/skills/` and `.factory/skills/` alongside primary trees). Instead of maintaining multiple independent copies that drift apart, `dedup --apply` collapses identical copies into symlinks so:

- IDE skill lists show **one entry** per logical skill (not N copies) for true duplicates
- Content stays DRY — edit the canonical file, all mirrors follow
- Host-specific variants (different content) are **never** overwritten — reported as `skip_multi_version` with inferred platform
- `audit` duplicate-name check reports **zero findings** post-dedup (for identical copies)
- Future `./setup` runs that recreate copies can be re-deduped in one command

```bash
# Dry-run: see what would be relinked vs skipped
skills-audit dedup --skills-dir "$HOME/.claude/skills"

# Apply: replace identical duplicates with symlinks (multi-version variants untouched)
skills-audit dedup --skills-dir "$HOME/.claude/skills" --apply

# Verify: audit should show zero duplicate names for truly identical copies
skills-audit audit --skills-dir "$HOME/.claude/skills" --fail-on-duplicate-names
```

**Canonical selection heuristic:** shortest path under the **install root** wins. Examples:

- `browse/SKILL.md` (canonical) vs `gstack/browse/SKILL.md` → relink the nested copy when hashes match
- `gstack/SKILL.md` (canonical — shortest inside the pack)
- `gstack/.agents/skills/gstack/SKILL.md` → symlink (if identical) or preserved (if different)
- `gstack/.factory/skills/gstack/SKILL.md` → symlink (if identical) or preserved (if different)

**Best practice workflow:**

1. Install/update a skill pack (`./setup`, `git pull`, etc.)
2. Run `skills-audit dedup --skills-dir <root> --apply`
3. Run `skills-audit audit --skills-dir <root>` to verify
4. For multi-version variants: use platform-aware discovery profiles (see below)
5. Repeat after each pack update

## Convention-based platform inference

`dedup` automatically infers target platforms from well-known directory conventions inside a bundle:

| Sub-directory | Inferred platform |
|---|---|
| `.agents/` | `codex` |
| `.codex/` | `codex` |
| `.factory/` | `factory` |

When a `skip_multi_version` action is reported, the output includes the inferred platform so you know which host the variant targets. This information feeds into the discovery profile routing below.

## Intra-bundle routing with exclude patterns

Discovery profiles now support **`exclude`** patterns per source entry. This enables routing different sub-directories of the same bundle to different platforms:

```json
{
  "sources": [
    {
      "path": "~/.claude/skills/gstack",
      "platform": ["cursor", "claude-code"],
      "exclude": [".agents/*", ".factory/*"]
    },
    {
      "path": "~/.claude/skills/gstack/.agents/skills",
      "platform": ["codex"]
    },
    {
      "path": "~/.claude/skills/gstack/.factory/skills",
      "platform": ["factory"]
    }
  ]
}
```

This means:
- **Cursor / Claude Code** see only the primary skill files (`.agents/` and `.factory/` excluded)
- **Codex** sees only the trimmed `.agents/skills/` variants
- **Factory** sees only the `.factory/skills/` variants

See `config/discovery-profile.gstack-multiplatform.example.json` for a ready-to-use template.

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

## Select-One Routing (v0.6.0)

For multi-platform skill packs (e.g. gstack shipping primary + `.agents/` + `.factory/` variants), `route` replaces `dedup` with a four-phase state-machine pipeline:

```
DISCOVERED → CLASSIFIED → ROUTED → RESOLVED
```

- **Phase 1 (Discover)**: hash all variants of each skill identity
- **Phase 2 (Classify)**: infer platform via path convention (`.agents/` → codex, `.factory/` → factory) or fallback to wildcard
- **Phase 3 (Route)**: exact platform match beats wildcard; select one variant per identity
- **Phase 4 (Resolve)**: terminal state per variant — `ACTIVE` / `ARCHIVED` / `DELETED` / `KEPT_HIDDEN` / `FLAGGED`

Every transition is recorded in a JSON trace file under `~/.skills-auditor/traces/`.

```bash
# Dry-run: see what route would do for Cursor
skills-audit route --platform cursor --skills-dir "$HOME/.cursor/skills"

# Apply with archive strategy (superseded variants renamed, recoverable)
skills-audit route --platform cursor --skills-dir "$HOME/.cursor/skills" --strategy archive --apply

# Route for Codex — selects .agents/ variants, archives the rest
skills-audit route --platform codex --skills-dir "$HOME/.claude/skills" --apply
```

### Resolve strategies

| Strategy | Superseded variants become | Use when |
|----------|---------------------------|----------|
| `archive` (default) | Renamed to `SKILL.md.archived-<timestamp>` | Safe — can undo |
| `delete` | Removed from disk | Confident cleanup |
| `keep` | Left in place (no filesystem change) | Audit-only, no side effects |

### State machine audit

Validate accumulated traces against transition rules:

```bash
# Audit all traces in default dir (~/.skills-auditor/traces/)
skills-audit audit-state-machine

# Audit traces from a specific directory
skills-audit audit-state-machine --trace-dir ./my-traces
```

Checks performed: illegal transitions, terminal state coverage, dead/unused states, signal coverage gaps, UNROUTABLE frequency, cross-run consistency (same skill selecting different variants across runs).

## Isolation Harness Recommendations

Each `route` or `audit-state-machine` run should produce **reproducible, context-free results**. When an AI agent runs multiple audits in one session, prior decisions and cached impressions can bias later runs (context pollution). The fix: run the fact-collection phase in an **isolated subagent** with zero prior context.

### Minimum viable isolation

Use your IDE's native subagent mechanism with `readonly: true`:

- **Cursor**: `Task` tool with `subagent_type: "shell"`, `readonly: true`
- **Claude Code**: scope with `--allowedTools`

### Dedicated harnesses

| Harness | Stars | Isolation model | Best for |
|---------|-------|----------------|----------|
| [subagent-harness](https://github.com/ERerGB/subagent-harness) | — | SSOT compile → per-runtime artifact; each invocation is stateless by design | Cursor / Claude Code / Codex ecosystems — compile the audit SKILL.md into an isolated subagent artifact, run it, collect structured trace |
| [dmux](https://github.com/standardagents/dmux) | 1.3k | git worktree per agent; parallel runs, smart merge | Running N audit configs in parallel (e.g. `--platform cursor` + `--platform codex` simultaneously), then merging trace results — similar to a `/batch` dispatch pattern |
| [CrewAI](https://github.com/crewAIInc/crewAI) | 48k | Role-based delegation with hierarchical process | Teams already in the CrewAI ecosystem needing audit as a delegated worker task |

### Cursor Task invocation template

```
Task(
  subagent_type: "shell"
  model: "fast"
  description: "Audit skills for <platform>"
  prompt: |
    Run the following commands and return their full stdout.
    Do NOT interpret or summarize — return raw output only.

    cd /path/to/skills-auditor && python -m skills_auditor.cli route \
      --platform <platform> \
      --skills-dir ~/.cursor/skills \
      --strategy archive

    Then run:
    python -m skills_auditor.cli audit-state-machine

    Return both outputs verbatim.
  readonly: true
)
```

## Safety Notes

- No destructive action in default mode.
- Existing non-symlink entries are archived as:
  - `<name>.backup-YYYYmmdd-HHMMSS`
- Existing symlinks are unlinked and recreated only in `--apply` mode.

## License

MIT
