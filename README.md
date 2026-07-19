# skills-auditor

> **Make every installed AI skill traceable to the definition a host will discover.**

AI coding hosts intentionally use different discovery roots. Once teams keep the same logical
skill across repository, global, and host-specific trees, a directory listing no longer explains
which definition is active, whether two copies are identical, or what a sync would replace.

`skills-auditor` models those trees as an install graph. It inventories links and directories,
validates `SKILL.md` contracts, detects identity collisions, selects platform variants, and plans
or applies host-native symlinks for Cursor, Claude Code, Codex, and registered environments.

> **Invariant:** plan install-root changes before applying them. CLI commands that change skill
> definitions or install roots require `--apply`; the bundled agent skill supplies `--apply` by
> default unless the operator explicitly requests dry-run.

**Boundary:** this project validates definitions, paths, metadata, hashes, and lifecycle evidence.
It does not execute a skill, translate its semantics, or prove equivalent behavior across hosts.

**Fast paths:** [Understand](#understand) · [Run the proof](#quickstart) ·
[Choose commands](#command-surface) · [Extend a host](#extend-the-host-model) ·
[Browse docs](#documentation)

```text
SKILL.md source roots
        │
        ▼
discover · validate · hash
        │
        ▼
select candidate + route platform
        │
        ▼
dry-run action plan
  ├── table / JSON
  ├── route traces
  └── optional run ledger
        │ --apply
        ▼
host install roots
  ├── .cursor/skills
  ├── .claude/skills
  └── .codex/skills
```

<details>
<summary><strong>README map</strong> — choose the shortest path</summary>

- **Evaluate:** [The missing layer](#the-missing-layer), [System model](#system-model),
  [Scope and compatibility](#scope-and-compatibility)
- **Adopt:** [Quickstart](#quickstart), [Use real roots](#use-real-roots),
  [CLI or agent skill](#cli-or-agent-skill)
- **Extend:** [Host model](#extend-the-host-model), [Python building blocks](#embed-the-audit-primitives),
  [Discovery policy](#control-source-selection)
- **Operate:** [Command surface](#command-surface), [Quality gates](#quality-gates),
  [Evidence](#evidence-and-local-state), [Safety](#safety-model)
- **Reference:** [Source and license](#source-and-license), [Documentation](#documentation)

</details>

<br>

## Understand

### The missing layer

Cursor, Claude Code, Codex, and other agents discover skills from native folders. That diversity is
useful; the engineering gap is a shared inspection and promotion layer between authored
`SKILL.md` trees and those host-visible install roots.

Without that layer, one logical `name:` can resolve to copied folders, healthy or broken symlinks,
or different platform variants. The filesystem contains the truth, but does not expose the
selection policy. `skills-auditor` makes that policy inspectable before changing the tree.

### System model

The tool does not create a new portable skill format. It keeps `SKILL.md` as the definition and
factors host path conventions, source selection, validation, and installation into explicit
layers.

| Layer | Representation | What is established |
| --- | --- | --- |
| Canonical aggregation | Repeated `--source`, `--skills-dir`, map files, or discovery profiles | Which trees participate and in what priority order |
| Portable identity | Frontmatter `name:` plus the `SKILL.md` content hash | Whether same-name candidates are identical or divergent |
| Target compatibility | `NativeEnvironment`, platform tags, path conventions, and symlink plans | Where a host-native discovery entry should point |
| Contract validation | Metadata findings, duplicate checks, route state transitions, and ledgers | What can be checked before handoff or consumption |

The installed artifact remains a normal host entry: usually a symlink to the selected source.
When sync encounters a native directory at that name, apply mode renames it to a timestamped
archive before linking. A second run against the same source produces `noop` actions.

### Scope and compatibility

`skills-auditor` can establish three increasingly strong facts:

- **Syntax and metadata conformance:** a visible `SKILL.md` has the frontmatter required by the
  selected profile; Codex requires non-empty `name` and `description`.
- **Definition provenance:** a host entry resolves to the selected source, and identical
  candidates share a hash.
- **Lifecycle conformance:** a route trace or execution ledger reaches an allowed terminal state.

It cannot establish semantic skill quality, model/tool behavior, credential compatibility, or
behavioral parity across runtimes. Sensor logs expose host-observable facts such as file access;
they do not by themselves prove that an agent semantically used the skill.

### Built-in environments

| Environment | Project root | Global root | Support |
| --- | --- | --- | --- |
| Cursor | `.cursor/skills` | `~/.cursor/skills` | Built in |
| Claude Code | `.claude/skills` | `~/.claude/skills` | Built in |
| Codex | `.codex/skills` | `~/.codex/skills` | Built in |
| Another host | Supplied by `NativeEnvironment` | Supplied by `NativeEnvironment` | Programmatic registration |

When `sync-discover` is called without `--source`, it scans `.agents/skills` and the built-in
project roots under the current directory. Add `--include-global-sources` only when global roots
should participate too.

<br>

## Adopt

### Quickstart

This proof installs the CLI, previews six links from the repository's own sub-skill pack, applies
them only inside a temporary directory, and audits the result.

```bash
git clone https://github.com/ERerGB/skills-auditor.git
cd skills-auditor
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .

SA_DEMO_ROOT="$(mktemp -d)"
SA_PREVIEW_STATUS=0
skills-audit sync-discover \
  --source "$PWD/skills" \
  --skills-dir "$SA_DEMO_ROOT/install" || SA_PREVIEW_STATUS=$?
if [ "$SA_PREVIEW_STATUS" -ne 0 ] && [ "$SA_PREVIEW_STATUS" -ne 1 ]; then
  exit "$SA_PREVIEW_STATUS"
fi

skills-audit sync-discover \
  --source "$PWD/skills" \
  --skills-dir "$SA_DEMO_ROOT/install" \
  --apply

skills-audit audit --skills-dir "$SA_DEMO_ROOT/install"
```

The preview exits `1` when `sync-discover` finds unapplied work; that is an expected plan result,
not a failed discovery. The apply run creates host-style symlinks, and the final audit reports each
entry as `symlink`, `ok`, and `has_skill_md=true`, followed by clean duplicate-name and metadata
checks.

```text
discovered: 6 sync entries
mode: DRY-RUN
skills-auditor-close    create_link    entry missing    .../skills/close

mode: APPLY
...
Applied actions. Re-run audit to verify final state.

name     entry_type    link_status    has_skill_md
skills-auditor-close    symlink       ok             true
...
status: ok (no duplicate names under this install root)
status: ok (no frontmatter metadata problems under this install root)
```

Repeat the apply command and the plan becomes `noop`. To run without installing, replace
`skills-audit` with `python3 scripts/skills_audit.py`; see the full [install guide](docs/install.md).

### Use real roots

Start by auditing the target namespace. Then preview the exact source-to-target reconciliation,
review every `create_link`, `replace_link`, `archive_and_link`, `noop`, or `skip_*` action, and only
then repeat it with `--apply`.

```bash
SA_SOURCE_ROOT="/path/to/canonical/skills"
SA_TARGET_ROOT="$HOME/.codex/skills"

skills-audit audit --skills-dir "$SA_TARGET_ROOT"

skills-audit sync-discover \
  --source "$SA_SOURCE_ROOT" \
  --skills-dir "$SA_TARGET_ROOT"

skills-audit sync-discover \
  --source "$SA_SOURCE_ROOT" \
  --skills-dir "$SA_TARGET_ROOT" \
  --apply

skills-audit audit --skills-dir "$SA_TARGET_ROOT"
```

Use `sync --map-file <file>` when canonical targets are curated explicitly. Use
`audit-discovery --profile-file <file>` when multiple source roots need priority, exclusion, hash
collapse, or platform policy before sync.

### CLI or agent skill

The repository exposes two execution surfaces with deliberately different defaults.

| Surface | Default for install-root or definition changes | Other side effects |
| --- | --- | --- |
| Direct CLI | `metadata-repair`, `dedup`, `route`, `sync`, and `sync-discover` preview until `--apply` | `route` writes a trace even in dry-run; `--with-drift` fetches Git remotes |
| Root [`SKILL.md`](SKILL.md) | The full pipeline passes `--apply` to metadata repair, dedup, route, and sync | Set `SKILLS_AUDITOR_DRY_RUN=1` or explicitly ask for dry-run to suppress apply |
| `route --strategy delete` | Never implied by the default archive strategy | Removes superseded paths only when paired with explicit `--apply` |

The agent pipeline is:

```text
metadata-repair → audit → dedup → route → trace QA → optional sync → closing audit
```

Narrow it with `SKILLS_AUDITOR_MODE`, configure roots and platforms with
[`config/skills-auditor.pipeline.example.env`](config/skills-auditor.pipeline.example.env), and
inspect the progressive sub-skill pack in [`skills/README.md`](skills/README.md).

<br>

## Extend

### Extend the host model

`NativeEnvironment` separates host path conventions from the discovery-entry lifecycle. A custom
embedding can add another environment and reuse the same discover, plan, archive, link, and verify
functions without adding host branches to the sync algorithm.

```python
from pathlib import Path

from skills_auditor.cli import discover_sync_mapping, plan_sync
from skills_auditor.environments import (
    BUILTIN_ENVIRONMENTS,
    NativeEnvironment,
    NativeEnvironmentRegistry,
)

registry = NativeEnvironmentRegistry(BUILTIN_ENVIRONMENTS.all())
registry.register(
    NativeEnvironment("acme-agent", (".acme/skills",), (".acme/skills",))
)

project = Path.cwd()
target = registry.get("acme-agent").primary_project_root(project)
mapping = discover_sync_mapping(
    [project / ".agents" / "skills"],
    exclude_target_root=target,
)

actions = plan_sync(target, mapping)
for action in actions:
    print(action.name, action.action, action.expected_target)
```

This registers path semantics in the embedding; it does not certify that the new host understands
the linked skill or automatically add that host to a separately installed CLI process. After
reviewing the plan, an embedding can call `apply_actions(target, actions)` explicitly.

### Embed the audit primitives

The same module-level functions can support a dashboard, migration tool, or CI adapter without
shelling out.

```python
from pathlib import Path

from skills_auditor.cli import collect_metadata_findings, scan_skills

root = Path.home() / ".codex" / "skills"
inventory = scan_skills(root)
metadata_findings = collect_metadata_findings(root, platform="codex")

broken = [item.name for item in inventory if item.link_status == "broken"]
if broken or metadata_findings:
    raise SystemExit({"broken_links": broken, "metadata": metadata_findings})
```

These Python building blocks are used by the repository's lifecycle tests, but they are not a
separately versioned SDK contract. Pin the package version when embedding them; use the CLI and
documented JSON schemas when a process boundary is preferable.

### Control source selection

Discovery profiles turn source order and platform eligibility into data. Each source can declare
allowed platforms and exclusion globs; identical same-name candidates can collapse by hash while
divergent candidates remain visible as conflicts.

Start with
[`config/discovery-profile.gstack-multiplatform.example.json`](config/discovery-profile.gstack-multiplatform.example.json)
and the [discovery recipes](docs/examples.md). `sync --target-platform` requires a discovery profile
so the target decision can be traced back to source platform tags.

<br>

## Operate

### Command surface

| Intent | Commands | Default behavior |
| --- | --- | --- |
| Inspect an install graph | `audit`, `metadata`, `audit-discovery` | Read-only; `audit` validates Codex metadata by default |
| Inspect Git provenance | `drift-check`, `audit --with-drift` | Fetches the backing repository remote for healthy symlinks |
| Repair metadata | `metadata-repair` | Dry-run; `--apply` performs supported idempotent frontmatter repairs |
| Fold exact duplicates | `dedup` | Dry-run; only same-hash `SKILL.md` files are relinked on apply |
| Select a host variant | `route`, `audit-state-machine` | Superseded variants default to archive; route writes traces and trace QA is read-only |
| Reconcile install roots | `sync`, `sync-discover` | Dry-run; apply creates/replaces links and archives native entries |
| Record observations | `record-trigger-log`, `record-sensor-event`, `audit-trigger-logs`, `audit-sensor-logs`, `aggregate-sensor-claims`, `log-stats` | Writes or reads gitignored local JSONL evidence as requested |
| Coordinate a run | `ledger-create`, `ledger-upsert`, `ledger-check`, `ledger-summary` | Creates or updates `skills-auditor-ledger/v1` JSON records |

Run `skills-audit --help` or `skills-audit <command> --help` for the authoritative flags. The
[examples guide](docs/examples.md) covers mapped sync, discovery sync, metadata repair, profiles,
and delegated ledgers.

### Quality gates

Use CI as a read-only conformance layer. Keep scheduled apply jobs out of the critical path until
the team owns the source policy and archive behavior.

```bash
python3 -m unittest discover -s tests -v

skills-audit audit \
  --skills-dir "$HOME/.codex/skills" \
  --fail-on-duplicate-names \
  --metadata-platform codex

skills-audit audit-discovery \
  --profile-file config/discovery-profile.gstack-multiplatform.example.json \
  --fail-on-conflict \
  --fail-on-hash-conflict
```

`audit` exits `5` for invalid metadata unless `--allow-invalid-metadata` is supplied, and exits `4`
for duplicate names when `--fail-on-duplicate-names` is set. `metadata-repair` and
`sync-discover` use exit `1` to signal pending dry-run work. See [CI and automation](docs/ci.md)
for gate composition.

### Evidence and local state

- Audit, sync, metadata, discovery, and ledger commands emit structured JSON alongside their
  human-readable reports where applicable.
- Route runs write state-machine traces under `~/.skills-auditor/traces/`, including dry-runs.
- Trigger logs, sensor logs, and execution ledgers live under `.skills-auditor-local/` by default;
  the repository ignores that directory.
- Ledgers point to existing traces and artifacts by locator. They do not replace route traces or
  copy external resources into the ledger.
- `skills-audit log-stats` summarizes local event, sensor, and trace storage before retention or
  automation decisions.

For a delegated run, create a ledger, upsert each `skill-run`, `trace`, `artifact`, or handoff, then
close with `ledger-check` and `ledger-summary`. Active rows warn; failed rows and incomplete handoff
or blocked metadata fail validation.

### Safety model

- **Preview first:** CLI content and install-root changes require `--apply`.
- **Preserve native entries:** sync renames an existing directory or file to
  `<name>.archived-<timestamp>` before linking.
- **Do not merge divergence:** dedup relinks identical hashes and leaves different-content variants
  for `route` or manual classification.
- **Prefer archive:** route defaults to `--strategy archive`; `delete` is an explicit destructive
  choice.
- **Repair only supported metadata:** malformed or ambiguous frontmatter is skipped for manual
  review and returns exit `5`.
- **Know the inspection side effects:** route writes traces, ledger checks update their `checks`
  block, and Git drift inspection fetches remotes.

Security posture and adoption checks are collected in [docs/security.md](docs/security.md).

<br>

## Reference

### Source and license

- Repository: [github.com/ERerGB/skills-auditor](https://github.com/ERerGB/skills-auditor)
- Package metadata: [`pyproject.toml`](pyproject.toml)
- License: MIT, as declared in [`pyproject.toml`](pyproject.toml)
- Supported Python: 3.9+

### Link measurement

The `Read` links in the documentation map are direct repository links. `Measured open` links route
through `r.fulmail.net` and record aggregate path-intent fields such as route id, referrer host,
user-agent class, timestamp, and a short-window correlation id.

The redirect layer does not store raw IP addresses, GitHub usernames, visitor ids, session ids,
full user-agent strings, or persistent cookies.

### Documentation

<!-- atlas-map:start -->
| Need | Read | Measured open |
| --- | --- | --- |
| Understand if this fits | [docs/getting-started.md](docs/getting-started.md) | [purpose](https://r.fulmail.net/r/oss/skills-auditor/readme_top/primary/purpose/v1) |
| Run the first safe audit | [docs/getting-started.md#quickstart](docs/getting-started.md#quickstart) | [quickstart](https://r.fulmail.net/r/oss/skills-auditor/readme_top/primary/quickstart/v1) |
| Install safely | [docs/install.md](docs/install.md) | [install](https://r.fulmail.net/r/oss/skills-auditor/install_block/primary/install/v1) |
| Find command recipes | [docs/examples.md](docs/examples.md) | [examples](https://r.fulmail.net/r/oss/skills-auditor/readme_top/secondary/examples/v1) |
| Use it in CI | [docs/ci.md](docs/ci.md) | [ci](https://r.fulmail.net/r/oss/skills-auditor/readme_top/secondary/ci/v1) |
| Debug a failed install or audit | [docs/troubleshooting.md](docs/troubleshooting.md) | [troubleshoot](https://r.fulmail.net/r/oss/skills-auditor/install_block/inline/troubleshoot/v1) |
| Inspect the skill contract | [docs/skill-contract.md](docs/skill-contract.md) | [api_reference](https://r.fulmail.net/r/oss/skills-auditor/docs/primary/api_reference/v1) |
| Evaluate security and dependencies | [docs/security.md](docs/security.md) | [security_policy](https://r.fulmail.net/r/oss/skills-auditor/trust/primary/security_policy/v1) |
| Compare alternatives | [docs/alternatives.md](docs/alternatives.md) | [alternatives](https://r.fulmail.net/r/oss/skills-auditor/footer/secondary/alternatives/v1) |

Full searchable map: [atlas.fulmail.net/oss/skills-auditor](https://atlas.fulmail.net/oss/skills-auditor)
<!-- atlas-map:end -->

Implementation references: [sub-skill index](skills/README.md) ·
[sensor architecture](doc/sensor-plugin-architecture.md) ·
[trigger regression contract](doc/trigger-observability-regression.md)
