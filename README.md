# skills-auditor

> **Turn canonical AI skill trees and named host targets into one reviewable filesystem transaction.**

Cursor, Claude Code, Codex, and other agents intentionally discover skills from different native
folders. The missing layer is not another skill format; it is a small integration contract that
resolves those folders, exposes every mutation, and makes the installed state checkable.

`skills-auditor` compiles source trees plus target names into a versioned plan, applies only that
plan, records a receipt, and verifies the links and source payload afterward.

> **Invariant:** plan the whole mutation; apply never rediscovers.

**Boundary:** the tool validates filesystem topology, metadata, source-tree hashes, and lifecycle
evidence. It does not translate skill semantics, execute a skill, or prove behavioral parity across
models, tools, credentials, and hosts.

**Fast paths:** [Understand the contract](#the-missing-contract) · [Run the proof](#quickstart) ·
[Adopt in a repository](#repository-adoption) · [Embed the planner](#python-building-block) ·
[Operate safely](#safety-model) · [Browse docs](#documentation)

```text
canonical skill trees + host names
               ↓
plan.json → apply(plan.json)
               ↓
receipt.json → verify
```

<details>
<summary><strong>README map</strong> — choose the shortest path</summary>

- **Evaluate:** [missing contract](#the-missing-contract), [data model](#data-model),
  [ground truth](#tiny-ground-truth), [boundary](#compatibility-boundary)
- **Adopt:** [quickstart](#quickstart), [repository config](#repository-adoption),
  [CLI and agent behavior](#cli-and-agent-behavior), [machine interface](#machine-interface)
- **Extend:** [custom targets](#custom-targets), [Python API](#python-building-block),
  [maintenance primitives](#maintenance-primitives)
- **Operate:** [safety](#safety-model), [quality gates](#quality-gates),
  [failure semantics](#failure-semantics), [local evidence](#local-evidence)
- **Reference:** [source and license](#source-and-license), [documentation](#documentation)

</details>

<br>

## Understand

### The missing contract

A host-visible folder answers “what can this host discover now?” It does not answer where a skill
came from, whether its scripts changed after review, which native entry will be archived, or
whether an apply command is still acting on the plan an operator approved.

The high-level interface makes those facts data:

```text
IntegrationSpec → Plan → Apply → Receipt → Verify
```

The older `sync` and `sync-discover` commands remain useful maintenance primitives. New host
integrations use the transaction above so preview and apply cannot silently recompute different
actions.

### Data model

| Object | Contains | Contract |
| --- | --- | --- |
| `skills-auditor-integration/v1` | Canonical source roots, named targets, metadata profile | One repository-owned integration intent |
| `skills-auditor-plan/v1` | Resolved roots, full tree hashes, target snapshots, exact actions and archive paths | Reviewable input to apply |
| `skills-auditor-receipt/v1` | Completed actions, installed snapshots, applied hashes, failure detail | Evidence of what apply reached |
| `skills-auditor-verification/v1` | Receipt-scoped link and source-tree checks | Current conformance to that receipt |

`plan_id` and `receipt_id` are content checksums, not signatures. A plan freezes regular-file
bytes, permission bits, directory structure, and internal symlink targets for each skill tree.
Source symlinks that escape their skill tree are rejected.

### Tiny ground truth

Given one canonical definition and a Codex project target:

```text
source     /repo/.agents/skills/review/SKILL.md
target     codex → /repo/.codex/skills
plan       create_link  /repo/.codex/skills/review
installed  /repo/.codex/skills/review → /repo/.agents/skills/review
```

No vendor artifact is generated and no definition is copied. The installed entry is a normal
host-native symlink; rerunning `integrate` against that state produces `noop`.

### Compatibility boundary

| Claim | What skills-auditor can establish |
| --- | --- |
| Metadata conformance | A discovered `SKILL.md` satisfies the selected frontmatter profile |
| Definition provenance | A target link resolves to the reviewed canonical tree |
| Payload integrity | The complete in-tree payload still matches its reviewed SHA-256 snapshot |
| Lifecycle evidence | Plans, receipts, route traces, and ledgers satisfy their structural contracts |
| Host path contract | Installed-wheel E2E covers Cursor, Claude Code, and Codex project/global roots on Linux and macOS |
| Behavioral parity | **Not established**; requires downstream host and model integration tests |

Built-in target names resolve to native roots without repeating path conventions:

| Target | Project root | Global selector |
| --- | --- | --- |
| `cursor` | `.cursor/skills` | `cursor@global` → `~/.cursor/skills` |
| `claude-code` | `.claude/skills` | `claude-code@global` → `~/.claude/skills` |
| `codex` | `.codex/skills` | `codex@global` → `~/.codex/skills` |

Use an explicit root for another host; registration does not certify that host's runtime behavior.

<br>

## Adopt

### Quickstart

This proof installs the package, plans six links from the repository's own sub-skill pack, shows
the saved plan, applies that exact file only inside a temporary directory, and verifies its receipt.

```bash
git clone https://github.com/ERerGB/skills-auditor.git
cd skills-auditor
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .

SA_DEMO_ROOT="$(mktemp -d)"
skills-audit integrate \
  --source "$PWD/skills" \
  --target-root demo="$SA_DEMO_ROOT/install" \
  --plan-out "$SA_DEMO_ROOT/plan.json"

python -m json.tool "$SA_DEMO_ROOT/plan.json"

skills-audit apply "$SA_DEMO_ROOT/plan.json" \
  --receipt-out "$SA_DEMO_ROOT/receipt.json"

skills-audit verify "$SA_DEMO_ROOT/receipt.json"
```

The observable result is `skills: 6`, `changes: 6`, a `completed` receipt, and `12` passing checks:
one target-link check and one source-tree check per skill. Repeat `integrate` against the same
source and target and `summary.changes` becomes `0`.

`integrate` writes a plan but never changes a host root. The explicit `apply` command is the first
step that can mutate one.

### Repository adoption

Keep the integration intent in `skills-auditor.json` at the repository root:

```json
{
  "schema_version": "skills-auditor-integration/v1",
  "project_root": ".",
  "sources": [".agents/skills"],
  "targets": ["cursor", "claude-code", "codex"],
  "metadata_platform": "codex"
}
```

Then the operating loop is deliberately boring:

```bash
skills-audit integrate
python -m json.tool .skills-auditor-local/plans/<plan-id>.json
skills-audit apply .skills-auditor-local/plans/<plan-id>.json
skills-audit verify .skills-auditor-local/receipts/<receipt-id>.json
```

Paths in the config resolve from `project_root`. Repeated CLI `--source` and `--target` values
replace their config lists. Start from the
[integration template](config/skills-auditor.integration.example.json) and read the
[full contract](docs/integration-contract.md) before automating apply.

### CLI and agent behavior

There is one write rule across both execution surfaces: host or definition changes need explicit
apply authorization.

| Surface | Default | Mutation gate |
| --- | --- | --- |
| `skills-audit integrate` | Resolve and save a plan | Never mutates target entries |
| `skills-audit apply PLAN` | Execute one reviewed plan | The command itself is explicit authorization |
| Root [`SKILL.md`](SKILL.md) | Plan-first maintenance or integration | Explicit operator wording or `SKILLS_AUDITOR_APPLY=1` |
| Primitive repair, dedup, route, sync | Preview | Their existing `--apply` flag |

`SKILLS_AUDITOR_DRY_RUN=1` always suppresses agent-driven apply. Delete is never inferred from a
generic apply request and still requires explicit operator wording.

### Machine interface

`integrate`, `apply`, and `verify` accept `--format json`. After argument parsing, stdout is exactly
one versioned JSON object; execution diagnostics are part of that object.

```bash
skills-audit integrate \
  --source .agents/skills \
  --target codex \
  --format json \
  --plan-out .skills-auditor-local/ci-plan.json
```

| Exit | Meaning |
| --- | --- |
| `0` | Command completed; an integration plan may contain changes or only `noop` |
| `2` | Invalid arguments, config, schema, path, or target name |
| `3` | Contract failure: conflict, stale plan, failed apply, or failed verification |

Inspect `summary.changes` instead of treating an ordinary plan as process failure. Parser usage
errors retain standard `argparse` stderr behavior.

<br>

## Extend

### Custom targets

An unregistered host needs only an explicit install root:

```bash
skills-audit integrate \
  --source .agents/skills \
  --target-root acme=.acme/skills
```

For reusable host conventions, `NativeEnvironment` separates path registration from discovery and
link lifecycle logic. See the [API and skill contract](docs/skill-contract.md); adding a path model
does not imply runtime certification.

### Python building block

The planner can be embedded without shelling out. This example stops after saving the reviewable
artifact; apply remains a separate decision.

```python
from pathlib import Path

from skills_auditor.integration import (
    IntegrationSpec,
    IntegrationTarget,
    build_integration_plan,
    save_plan,
)

project = Path.cwd().resolve()
spec = IntegrationSpec(
    project_root=project,
    sources=((project / ".agents" / "skills").resolve(),),
    targets=(IntegrationTarget("acme", root=(project / ".acme" / "skills").resolve()),),
)

plan = build_integration_plan(spec)
plan_path = save_plan(plan)
print(plan["summary"], plan_path)
```

These module functions are tested building blocks, not a separately versioned SDK promise. Pin the
package when embedding them; prefer the CLI plus versioned JSON schemas across process boundaries.

### Maintenance primitives

The transaction handles adoption. Existing commands remain available for diagnosis, repair,
variant routing, observability, and delegated-run evidence.

<details>
<summary><strong>Advanced command surface</strong></summary>

| Intent | Commands | Default |
| --- | --- | --- |
| Inspect roots and metadata | `audit`, `metadata`, `audit-discovery` | Read-only |
| Inspect Git provenance | `drift-check`, `audit --with-drift` | Fetches backing remotes |
| Repair safe metadata cases | `metadata-repair` | Preview until `--apply` |
| Fold exact duplicates | `dedup` | Preview; apply relinks identical definitions only |
| Select platform variants | `route`, `audit-state-machine` | Archive strategy; route writes traces |
| Reconcile an existing map | `sync`, `sync-discover` | Preview until `--apply` |
| Record host observations | `record-*`, `audit-*-logs`, `aggregate-sensor-claims`, `log-stats` | Local evidence files |
| Coordinate delegated work | `ledger-*` | Versioned local ledger records |

</details>

Run `skills-audit --help` for the authoritative list and use the
[examples](docs/examples.md) for recipes. The root agent skill composes maintenance primitives as
`metadata-repair → audit → dedup → route → trace QA → optional sync → closing audit`.

<br>

## Operate

### Safety model

- **All-roots preflight:** source trees, target entries, and reserved archive paths are checked
  before the first write, then checked again immediately before each action.
- **Exact apply:** `apply` consumes recorded actions; it does not scan for new sources or rebuild a
  plan.
- **Archive first:** a native file or directory moves to the exact missing archive path recorded in
  the plan before its symlink is created.
- **Local rollback:** replace-link and archive-link actions attempt to restore their entry when link
  creation fails.
- **Disjoint roots:** install aliases are one path segment; source and target roots cannot contain
  each other; target roots cannot overlap; source symlinks cannot escape their skill tree.
- **Live-source truth:** installed entries are symlinks, not immutable copies. Protect or pin the
  canonical checkout; a later source edit is visible to the host and will make receipt verification
  fail.

Several filesystem operations across several roots cannot be globally atomic. Preserve failed
receipts and audit the listed roots before replanning.

### Quality gates

```bash
python3 -m coverage run -m unittest discover -s tests -v
python3 -m coverage report

python3 -m build
python3 scripts/run_artifact_tests.py smoke dist
python3 scripts/run_artifact_tests.py e2e dist

skills-audit audit \
  --skills-dir .agents/skills \
  --fail-on-duplicate-names \
  --metadata-platform codex

skills-audit verify .skills-auditor-local/receipts/<receipt-id>.json \
  --format json
```

The repository [CI workflow](.github/workflows/ci.yml) enforces at least 90% branch-aware coverage on
Python 3.9–3.14, installs the built wheel into clean environments for Smoke tests, and runs the real
console-script E2E lifecycle on Linux and macOS. A separate distribution job checks version metadata,
the console entry point, license, schemas, integration contract, packaged tests, and examples.

### Failure semantics

| Failure | Write boundary | Recovery |
| --- | --- | --- |
| Invalid metadata or conflicting sources | Plan is not created | Repair or select canonical sources |
| Stale plan during all-roots preflight | No target write starts | Generate and review a new plan |
| Target or source changes during apply | Apply stops; earlier actions may exist | Preserve failed receipt, audit roots, replan |
| Receipt cannot be stored | Apply reports the missing evidence explicitly | Audit every target before retrying |
| Receipt verification fails | Read-only check | Inspect `target_link` or `source_tree`, then replan |

See [troubleshooting](docs/troubleshooting.md) for concrete diagnostics and
[security](docs/security.md) for the trust boundary.

### Local evidence

- Plans and receipts default to `.skills-auditor-local/`; the repository ignores that directory.
- Route traces default to `~/.skills-auditor/traces/`, including route dry-runs.
- Trigger and sensor logs describe host-observable events such as file access. They do not prove
  semantic skill use.
- Optional execution ledgers reference plans, receipts, traces, and external artifacts by locator;
  they do not replace those records.

`drift-check` and `audit --with-drift` fetch Git remotes. The high-level integration transaction
itself needs no network access.

<br>

## Reference

### Source and license

- Repository: [github.com/ERerGB/skills-auditor](https://github.com/ERerGB/skills-auditor)
- Package metadata: [`pyproject.toml`](pyproject.toml)
- Version source: [`skills_auditor/_version.py`](skills_auditor/_version.py)
- License: [MIT](LICENSE)
- Runtime: Python 3.9+ and the standard library
- Release procedure: [`docs/releasing.md`](docs/releasing.md)

### Link measurement

The `Read` links below are direct repository links. `Measured open` links route through
`r.fulmail.net` and record aggregate path-intent fields such as route id, referrer host, user-agent
class, timestamp, and a short-window correlation id.

The redirect layer does not store raw IP addresses, GitHub usernames, visitor ids, session ids,
full user-agent strings, or persistent cookies.

### Documentation

<!-- atlas-map:start -->
| Need | Read | Measured open |
| --- | --- | --- |
| Understand if this fits | [docs/getting-started.md](docs/getting-started.md) | [purpose](https://r.fulmail.net/r/oss/skills-auditor/readme_top/primary/purpose/v1) |
| Inspect the integration contract | [docs/integration-contract.md](docs/integration-contract.md) | — |
| Run the first safe proof | [docs/getting-started.md#quickstart](docs/getting-started.md#quickstart) | [quickstart](https://r.fulmail.net/r/oss/skills-auditor/readme_top/primary/quickstart/v1) |
| Install safely | [docs/install.md](docs/install.md) | [install](https://r.fulmail.net/r/oss/skills-auditor/install_block/primary/install/v1) |
| Find command recipes | [docs/examples.md](docs/examples.md) | [examples](https://r.fulmail.net/r/oss/skills-auditor/readme_top/secondary/examples/v1) |
| Use it in CI | [docs/ci.md](docs/ci.md) | [ci](https://r.fulmail.net/r/oss/skills-auditor/readme_top/secondary/ci/v1) |
| Debug a failed plan, apply, or verify | [docs/troubleshooting.md](docs/troubleshooting.md) | [troubleshoot](https://r.fulmail.net/r/oss/skills-auditor/install_block/inline/troubleshoot/v1) |
| Inspect the agent and API contract | [docs/skill-contract.md](docs/skill-contract.md) | [api_reference](https://r.fulmail.net/r/oss/skills-auditor/docs/primary/api_reference/v1) |
| Evaluate security and dependencies | [docs/security.md](docs/security.md) | [security_policy](https://r.fulmail.net/r/oss/skills-auditor/trust/primary/security_policy/v1) |
| Cut a release | [docs/releasing.md](docs/releasing.md) | — |
| Compare alternatives | [docs/alternatives.md](docs/alternatives.md) | [alternatives](https://r.fulmail.net/r/oss/skills-auditor/footer/secondary/alternatives/v1) |

Full searchable map: [atlas.fulmail.net/oss/skills-auditor](https://atlas.fulmail.net/oss/skills-auditor)
<!-- atlas-map:end -->

Implementation references: [sub-skill index](skills/README.md) ·
[sensor architecture](doc/sensor-plugin-architecture.md) ·
[trigger regression contract](doc/trigger-observability-regression.md)
