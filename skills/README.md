# Skills Auditor — sub-skill pack

Layered skills under this folder. The **top entry** is the repository root [`../SKILL.md`](../SKILL.md).

| Sub-skill | Cycle | Role |
| --- | --- | --- |
| [discover](discover/SKILL.md) | 1 | `audit`, `drift-check`, optional `audit-discovery` |
| [dedup](dedup/SKILL.md) | 2 | Hash-aware duplicate fold; plan-first, explicit apply |
| [route](route/SKILL.md) | 3 | Select-one routing per platform + strategies |
| [traces](traces/SKILL.md) | 4 | `audit-state-machine` on routing traces |
| [sync](sync/SKILL.md) | 5 (optional) | `sync` from `--map-file` |
| [close](close/SKILL.md) | 6 | Repeat discover audit to confirm end state |

Configuration template: [`../config/skills-auditor.pipeline.example.env`](../config/skills-auditor.pipeline.example.env).

Each sub-skill follows the [capture preflight contract](../SKILL.md#before-every-invocation-optional-skill-trace-preflight),
including direct invocation. The `traces` cycle validates routing state-machine records. The
optional [Skill Trace plugin](../docs/skill-trace.md) captures host runtime events and has its own
[installation, trust, and enablement steps](../docs/install.md#optional-skill-trace-plugin).

Ledger compatibility: when an orchestrator creates `.skills-auditor-local/ledgers/<run-id>.json`, every sub-skill should record its own `skill-run` row. Sub-skills that create traces, logs, archives, deletes, or sync changes should also record `trace` or `artifact` rows that point to the existing files or external resources.
