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
