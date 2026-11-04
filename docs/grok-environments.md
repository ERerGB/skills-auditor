# Grok environment ownership and integration proposal

Research checked against official documentation on 2026-11-04. This document
records the proposed integration boundary; it does not enroll a cloud computer,
install Grok, publish private skills or claim native acceptance.

## Product identity is separate from the execution environment

| Surface | Auditor classification | Relevant boundary |
| --- | --- | --- |
| A Grok model selected in Cursor CLI | Existing `cursor` Skill host, with a model choice | A model change does not create a new machine, workspace or installation |
| Grok Build CLI | Proposed independent `grok` Skill host | Native `.grok/skills`, its own configuration and native discovery |
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

## Proposed implementation sequence

1. Confirm whether the requested target is Build CLI, Bot's cloud computer, or
   both. Keep the current default three-platform scope unchanged.
2. For Build, add an explicit `grok` host, declarations and native/compatibility
   root checks. Exercise `grok inspect --json` before any activation, then verify
   catalog discovery, canonical Skill/template reads and a bounded operation.
3. For Bot, enroll the verified computer through an established route and record
   Bot-to-computer association separately. Design private-library registration
   only after its actual contract and approval controls are known.
4. Reuse fixed source releases, exact-plan approval and per-environment journals.
   Keep native authentication local; plugin distribution, hooks, shared context,
   routines and telemetry synchronization remain outside this iteration.

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
