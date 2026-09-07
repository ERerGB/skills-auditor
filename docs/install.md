# Install skills-auditor

Python 3.9 or newer is required. The package has no runtime dependencies outside the standard
library.

## Isolated install from GitHub

Use `pipx` for a global CLI without coupling it to a project environment:

```bash
pipx install git+https://github.com/ERerGB/skills-auditor.git
skills-audit --version
```

## Virtual environment

```bash
git clone https://github.com/ERerGB/skills-auditor.git
cd skills-auditor
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
skills-audit --version
```

Use `python -m pip install -e .` only when developing skills-auditor itself.

## Register the agent skill

Installing the CLI alone is enough for direct terminal use. The chat workflow also requires the
repository's root [`SKILL.md`](../SKILL.md) to be visible in the host's skill directory.

Keep the repository checkout intact and link the whole directory as one skill. For example, to
register it globally in Codex:

```bash
mkdir -p ~/.codex/skills
ln -s /absolute/path/to/skills-auditor ~/.codex/skills/skills-auditor
```

Choose the root that matches the host and desired scope:

| Host | Project scope | Global scope |
| --- | --- | --- |
| Cursor | `.cursor/skills` | `~/.cursor/skills` |
| Claude Code | `.claude/skills` | `~/.claude/skills` |
| Codex | `.codex/skills` | `~/.codex/skills` |

The destination `skills-auditor` entry must not already exist. Inspect an existing entry before
replacing or relinking it. Once registered, start a new chat session if the host only discovers
skills at session startup, then invoke `/skills-auditor` or ask for the equivalent workflow in
natural language.

## No-install entry

From a repository checkout:

```bash
python3 scripts/skills_audit.py --help
```

The module entry is equivalent:

```bash
python3 -m skills_auditor --help
```

## Optional Skill Trace plugin

Use Skills Auditor CLI `0.8.0` or later with Skill Trace `0.2.0` or later for these controls.
The auditor works without runtime capture. The separate `skill-trace` plugin adds local hook
events for later inspection. Capture is **off by default**, including when upgrading from 0.1.x;
choose whether to enable it. These steps apply only to this plugin's hooks.

### Install the plugin and its Python core

Keep the checkout used above. Register its local marketplace and install the plugin:

```bash
codex plugin marketplace add /absolute/path/to/skills-auditor
codex plugin add skill-trace@skills-auditor-local
```

The hook runs `python3` from the Codex host environment. With this local marketplace, the wrapper
finds `skills_auditor` in the registered checkout; keep that checkout in place. Alternatively,
install the package into that interpreter with `python3 -m pip install /absolute/path/to/skills-auditor`
in a virtual environment used to launch Codex. A `pipx` CLI installation alone does not make the
package importable by an unrelated `python3`. `SKILLS_AUDITOR_REPO` can explicitly name the checkout
if needed; Codex must inherit it. Hook commands resolve scripts from `PLUGIN_ROOT`, so they work
in other projects and paths containing spaces.

### Review and trust this plugin's hooks

Open the interactive **Codex CLI** with the same OS user and `CODEX_HOME` used by the desktop app:

```bash
codex
```

Inside that CLI, enter `/hooks` (it is an interactive slash command, not `codex hooks`). Select
the source **`skill-trace@skills-auditor-local`**, inspect its installed `hooks/hooks.json` and
script, and review/trust its `SessionStart`, `PreToolUse`, `PostToolUse`, and `Stop` handlers.
Enable any of those handlers that you previously disabled.

Installing/enabling a plugin and trusting its hooks are separate steps. Codex persists trust
for the current hook definition hash in its user configuration. New or changed definitions
require review again and are skipped until trusted. Project trust alone does not grant plugin
hook trust. Do not copy trusted hashes between machines or set Codex's global hook switch as
part of this plugin's setup. See the official [hook trust instructions](https://learn.chatgpt.com/docs/hooks#review-and-trust-hooks)
and [plugin hook behavior](https://learn.chatgpt.com/docs/hooks#plugin-bundled-hooks).

### Choose capture and verify it in a task

```bash
skills-audit skill-trace enable
```

This saves only the plugin's preference to `$CODEX_HOME/skill-trace.json` (normally
`~/.codex/skill-trace.json`). It does not grant Codex hook trust. Start a fresh desktop task after
installation or a hook update. Ask the agent to read a local `SKILL.md` in a separate tool call,
then run this **inside that task**:

```bash
skills-audit skill-trace check
skills-audit audit-sensor-logs
```

`healthy` requires this plugin's recent `PreToolUse` and `PostToolUse` writes from the same task
and working directory. A first call may report `unverified` until its post-tool hook finishes;
check again in the next tool call. A successful manual invocation of the script is only a smoke
test and does not verify Codex dispatch or trust. From a terminal outside the task, pass
`--session-id <task-id>` and run from its working directory.

### Turn only Skill Trace capture off or back on

```bash
skills-audit skill-trace disable
skills-audit skill-trace status
skills-audit skill-trace enable
```

Disabling skips payload reading and sensor writes on subsequent hook invocations; a write already
in flight can finish. Existing logs remain readable. The tiny hook process can still launch; to
stop those launches too, disable only this plugin's handlers in CLI `/hooks` or disable the
Skill Trace plugin in Codex. Re-enable the same handlers/plugin when restoring capture.

For a temporary process-level override, set `SKILLS_AUDITOR_SKILL_TRACE=0` or `1` **before launching
Codex**. A shell export inside an agent tool does not change the desktop hook runner's environment.
The override takes precedence over the saved preference; `status` reports its source. Prefer the
persistent commands for desktop use. `SKILLS_AUDITOR_SKILL_TRACE_CONFIG` overrides the plugin's
settings-file location when both the CLI and hook runner inherit it.

`SKILLS_AUDITOR_LOG_DIR` overrides the log root; relative paths resolve against the task working
directory. Configure it in the host environment so the writer and checker use the same root.

### Upgrade existing installations

Update the checkout and reinstall/upgrade the Python CLI, then refresh the local marketplace
and install the updated plugin through Codex. Verify its installed version is `0.2.0` or later;
editing the source checkout does not replace an already cached plugin. Review the updated
definitions in `/hooks` again: this version changes commands to use `PLUGIN_ROOT`. Explicitly
enable capture, restart the task, and repeat the live check above.

See [design, evidence limits, and health states](skill-trace.md) for troubleshooting details.

## Upgrade

```bash
pipx upgrade skills-auditor
```

For a Git URL install, reinstall from the desired tag or commit when the environment does not
resolve upgrades automatically.

## Verify package contents

The release version has one source of truth in `skills_auditor/_version.py`. Distribution builds
include the MIT [`LICENSE`](../LICENSE), documentation, configuration examples, and versioned JSON
Schemas. Maintainers can run the complete [release gate](releasing.md).
