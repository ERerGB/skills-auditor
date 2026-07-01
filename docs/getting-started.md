# Getting Started

skills-auditor inspects local AI skill folders and produces evidence before you sync or replace anything.

It is designed for environments where skill folders live in several places:

- Cursor global skills.
- Claude Code global skills.
- Codex or project-local skill roots.
- Shared repositories that provide canonical skill sources.

## What it checks

- Symlink health: `ok` or `broken`.
- Folder mode: `symlink`, `directory`, or `file`.
- Duplicate skill names across resolved `SKILL.md` files.
- Metadata validity for Codex-style frontmatter.
- Discovery collisions across multiple source roots.
- Planned sync actions before any apply step.

## Quickstart

```bash
git clone https://github.com/ERerGB/skills-auditor.git
cd skills-auditor
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

skills-audit audit --skills-dir "$HOME/.cursor/skills"
```

The first output to inspect is the audit table. It tells you whether each entry is a directory, file, symlink, or broken symlink, and whether repository drift is scoped to the skill itself or only to unrelated paths in the backing repo.

## First decision

Run only read-only commands until the output is stable:

```bash
skills-audit audit --skills-dir "$HOME/.cursor/skills"
skills-audit audit --skills-dir "$HOME/.claude/skills" --fail-on-duplicate-names
```

Move to sync only after the audit output explains the state you expected to see.

## Next paths

- Install checklist: [install.md](install.md)
- Command recipes: [examples.md](examples.md)
- CI gate: [ci.md](ci.md)
- Troubleshooting: [troubleshooting.md](troubleshooting.md)
