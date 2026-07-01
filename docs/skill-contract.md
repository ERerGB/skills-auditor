# Skill contract and API reference

The root [`SKILL.md`](../SKILL.md) is the agent entry point for the skill pack.

## Entry behavior

The top-level skill runs the full pipeline:

1. Discover skill folders.
2. Deduplicate names and sources.
3. Route work to the right sub-skill.
4. Sync when the operator approves apply-mode behavior.
5. Review trace output.
6. Close with a concise report.

By default, the top-level skill can use apply-mode for dedup, route, and sync. Force dry-run mode with:

```bash
export SKILLS_AUDITOR_DRY_RUN=1
```

## Sub-skills

Layered sub-skills live under [`skills/`](../skills/README.md):

- `discover`: inspect available skill roots.
- `dedup`: reason about duplicate names and canonical sources.
- `route`: select the right workflow path.
- `sync`: apply or preview synchronization.
- `traces`: inspect local skill-read sensor logs.
- `close`: summarize the run.

## CLI entry

The installed console script is:

```bash
skills-audit
```

The package module entry is:

```bash
python3 -m skills_auditor
```

The no-install script entry is:

```bash
python3 scripts/skills_audit.py
```

## Configuration examples

- [`config/sources.example.json`](../config/sources.example.json)
- [`config/discovery-profile.cursor-jz.example.json`](../config/discovery-profile.cursor-jz.example.json)
- [`config/discovery-profile.gstack-fork.example.json`](../config/discovery-profile.gstack-fork.example.json)
- [`config/discovery-profile.gstack-multiplatform.example.json`](../config/discovery-profile.gstack-multiplatform.example.json)
- [`config/skills-auditor.pipeline.example.env`](../config/skills-auditor.pipeline.example.env)
