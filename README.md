# skills-auditor

Audit, deduplicate, route, and sync local AI skill folders for Cursor, Claude Code, Codex, and custom agent workflows.

Use it when local skill directories have drifted across machines, repositories, or agent runtimes and you need a reversible audit before changing files.

## Who this is for

- Solo developers who copy skills between local agent tools and need to know what is stale or duplicated.
- Platform or DX engineers who maintain shared skill folders for a team.
- Agent workflow maintainers who need repeatable evidence before syncing, replacing, or routing skills.

## Quick fit check

Use skills-auditor if you need to answer:

- Which local skills are symlinks, directories, files, or broken links?
- Which skill names collide across install roots or discovery sources?
- What would change before I apply a sync?
- Can Cursor, Claude Code, Codex, or project-local roots be checked in one repeatable flow?

If you only need to copy one folder once, a shell command is probably enough. If you need ongoing inspection and safe sync planning, use this repo.

## 3-minute quickstart

```bash
git clone https://github.com/ERerGB/skills-auditor.git
cd skills-auditor
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

skills-audit audit --skills-dir "$HOME/.cursor/skills"
```

The first command to run is `audit`. It is read-only and gives you the current shape of the target skill root.

Need to run without installing?

```bash
python3 scripts/skills_audit.py audit --skills-dir "$HOME/.cursor/skills"
```

<!-- atlas-map:start -->
## Documentation Map

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

## Skill pack entry

The root [`SKILL.md`](SKILL.md) is the agent entry point. It runs the full audit pipeline by default and uses `--apply` for dedup, route, and sync unless the operator asks for dry-run or sets:

```bash
export SKILLS_AUDITOR_DRY_RUN=1
```

Layered sub-skills live under [`skills/`](skills/README.md). Optional pipeline environment examples live in [`config/skills-auditor.pipeline.example.env`](config/skills-auditor.pipeline.example.env).

## Skill-run ledgers

Use ledgers when an orchestrator, sub-skill, or delegated worker needs a structured record of execution state, side effects, artifacts, traces, and handoffs.

Ledgers use schema `skills-auditor-ledger/v1` and are stored under `.skills-auditor-local/ledgers/` by default. That directory is local and gitignored, matching the existing `.skills-auditor-local/` trigger and sensor logs. Ledgers do not replace route state-machine traces; record those trace files as `trace` resources when you need one run ledger to point at them.

```bash
skills-audit ledger-create --run-id run-1 --source orchestrator --mode apply

skills-audit ledger-upsert \
  --run-id run-1 \
  --id route-trace \
  --class trace \
  --locator "$HOME/.skills-auditor/traces/trace.json" \
  --owner skills-auditor-route \
  --status preserved \
  --metadata platform=codex

skills-audit ledger-check --run-id run-1
skills-audit ledger-summary --run-id run-1
```

Resource classes are `skill-run`, `subagent-run`, `trace`, `artifact`, and `external-resource`. Statuses are `active`, `completed`, `preserved`, `handoff`, `blocked`, and `failed`. `handoff` rows should include `--handoff-target`; `blocked` rows should include `--blocked-reason`.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## Link measurement

The `Read` links in the documentation map are direct GitHub links. The `Measured open` links route through `r.fulmail.net` first and record aggregate path-intent fields such as route id, referrer host, user-agent class, timestamp, and a short-window correlation id.

The redirect layer does not store raw IP addresses, GitHub usernames, visitor ids, session ids, full user-agent strings, or persistent cookies.

## Source and license

- Repository: [github.com/ERerGB/skills-auditor](https://github.com/ERerGB/skills-auditor)
- License: [MIT](LICENSE)
