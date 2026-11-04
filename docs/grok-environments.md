# Grok Build Skill host

Research checked against official documentation on 2026-11-04. This document
records the implemented Grok Build CLI integration. Grok Bot cloud enrollment,
plugin distribution and native login/trust remain outside this iteration.

## Product identity is separate from the execution environment

| Surface | Auditor classification | Relevant boundary |
| --- | --- | --- |
| A Grok model selected in Cursor CLI | Existing `cursor` Skill host, with a model choice | A model change does not create a new machine, workspace or installation |
| Grok Build CLI | Independent `grok` Skill host | Native `.grok/skills`, its own configuration and native discovery |
| Grok Bot | Remote execution environment plus Bot identity | The account's cloud computer owns filesystem state; several Bots share it |

The [Cursor Grok page](https://cursor.com/grok) confirms that Grok models are
available through Cursor's CLI and other surfaces. That does not establish that
every Grok product uses Cursor's native skill directories.

[Grok Build](https://docs.x.ai/build/overview) provides its own TUI, headless CLI
and ACP interface. Its [skill contract](https://docs.x.ai/build/features/skills-plugins-marketplaces)
uses project `.grok/skills` up to the repository root, user `.grok/skills` and
configured extra paths. It also consumes Claude Code compatibility content and
user `.agents/skills`. `GROK_HOME` can change its user configuration location.
An implementation must inspect those loading surfaces and same-name conflicts;
aliasing `grok` directly to Auditor's `cursor` descriptor would miss them.

## Grok Bot's cloud computer

The [official computer model](https://docs.x.ai/grok-bot/computer-and-apps) says
the persistent computer belongs to the user account. Bots share files, browser
sessions and command-line credentials; separate screens do not isolate storage.
The [Bot overview](https://docs.x.ai/grok-bot/overview) identifies Cursor's cloud
as the execution infrastructure.

Therefore model one verified computer/instance generation as one environment.
Register each actual project directory as a workspace. Keep Bot identity separate;
do not invent one independent machine or security boundary per Bot. New computer
generations need fresh enrollment and local approval, as with other providers.

Private Bot skills are a shared library with per-Bot availability controls under
Connect apps. [Skills and routines](https://docs.x.ai/grok-bot/skills-routines-and-automations)
describes this application-level workflow. It does not establish a writable
`SKILL.md` directory contract or a controller-accessible SSH/API transport for
that library. A cloud filesystem, private skill library and enabled app are
distinct registration surfaces. A symlink check cannot prove Bot visibility.

Before implementing a Bot adapter, verify the specific account/computer, its
instance generation, approved execution/access route and native skill-library
contract. The existing target-local workflow can run inside a verified cloud
terminal; controller-driven delivery requires an actual supported route. Do not
add a nominal `grok-bot` provider that cannot execute or verify operations.

## Enable Grok Build explicitly

`grok` is an additive host choice. Existing workspace and source defaults remain
`codex`, `cursor`, `claude-code`; they do not automatically opt into Grok.
Both the selected source's `platforms` and the workspace's `platforms` must include
`grok`. For a Grok-only workspace declaration:

```json
{
  "schema_version": "skills-auditor-workspace-declaration/v1",
  "baseline": ["grok-baseline"],
  "platforms": ["grok"]
}
```

Bind a dependency-complete source, then register, review and apply:

```bash
skills-audit workspace source add grok-baseline --kind local --root /sources/package \
  --include skills --include templates --require-file templates/report.md \
  --entry review=skills/review --platform grok
skills-audit workspace register grok-project --environment laptop \
  --path /work/project --declaration /work/project/skills-auditor.workspace.json
skills-audit workspace plan grok-project --plan-out grok-plan.json
skills-audit workspace apply grok-plan.json --approve-plan-id REVIEWED_PLAN_ID
skills-audit workspace verify grok-project --native grok
```

Portable source definitions can be committed in the declaration's `sources`, as
described in [workspace management](workspace-management.md). Git pins, local
content snapshots, exact-plan approval, dependency layout, inverse plans and
replacement recovery use the same lifecycle as the other hosts. Legacy
`integrate --target grok` and `--target grok@global` are also supported.

For an existing registered workspace, explicitly add `grok` to its platform list
and register the same workspace ID/path again before planning. Registration can
add platforms while preserving existing installation identities. It cannot
remove roots, rebind the directory or silently reuse a different instance.

## Discovery and configuration

The primary project entry is `.grok/skills/NAME`. User registration uses
`$GROK_HOME/skills` when `GROK_HOME` is set, otherwise `~/.grok/skills`.
`GROK_HOME` must be absolute for managed discovery. User, project and parent
repository roots include Grok plus Agent/Cursor/Claude compatibility roots.

Auditor reads explicit `[skills].paths` from owner-local Grok user, project and
managed configuration layers, without starting hooks, MCPs or plugins. Extra
paths must be absolute. Conflicting aliases, broken links and malformed configs
block planning. File paths and configuration hashes stay in private target
plans; config content is not copied into releases. Changes to roots or config
hashes after review stale that plan before activation. Python 3.11+ uses
`tomllib`; Python 3.9/3.10 install the declared `tomli` compatibility dependency.

## Native verification and remaining acceptance

`workspace verify --native grok` invokes the explicit `grok` executable with
`--no-auto-update ... inspect --json`. It checks the returned Grok contract and
compares discovered Skill identities and canonical paths with active managed
entries, then actually reads those files. Unavailable, ambiguous or shadowed
results stay unverified. No raw native configuration or authentication output is
returned. A native discovery gap does not revoke otherwise healthy filesystem
approval; the explicitly requested combined verification still fails.

The result level is `catalog_and_reads`. It does not claim a model followed the
Skill, read a required template or completed a useful operation. A fresh native
session must perform those bounded checks separately. Project trust also matters:
the tested Grok Build 1.0.70 omits project Skills from an untrusted directory's
catalog. Auditor never changes native folder trust, hook trust, login or plugin
settings to make verification pass. An explicitly configured user Skill path can
be discovered independently; the report retains `project_trusted: false`.

The official 1.0.70 binary used for isolated discovery is retained privately for
acceptance. It does not replace global executables or borrow another client's
credentials. No successful authenticated Grok model operation is claimed until
native authentication and any required project trust are completed by their owner.

## Executable identity and client updates

Official installers can route the generic `agent` command differently. The
research copy of Cursor's installer contains Grok-host switch handling, while
Grok's installer creates `grok` and `agent` links and honors Cursor switch records.
An executable named `agent` is consequently insufficient evidence that Cursor
is installed. Auditor status now requires the explicit `cursor-agent` command.
Command presence still indicates availability only; native acceptance must
confirm which host actually runs and what it discovers.

The current Cursor CLI was already downloaded into the private isolated
acceptance directory during the previous iteration. This proposal does not
replace global executables, rewrite generic aliases, transfer account tokens or
perform native login. A global upgrade must preserve old version recovery and
the actual current owner of `agent`/`grok` aliases.
