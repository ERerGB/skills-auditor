# Community Launch Kit

This file contains copy-ready posts for developer communities.

Project URL: https://github.com/ERerGB/skills-auditor

---

## 1) Cursor Forum (Built for Cursor)

Suggested category:

- Showcase -> Built for Cursor

Suggested title:

- `skills-auditor: one-command audit/sync for local Cursor skills`

Post body:

```md
Built a small utility for people whose `~/.cursor/skills` gets messy over time.

`skills-auditor` helps with:

- broken symlink detection
- local-folder vs symlink mode audit
- dry-run sync plan from canonical source mapping
- safe apply mode (existing dirs are archived before relink)

Repo: https://github.com/ERerGB/skills-auditor

Quick example:

```bash
python3 scripts/skills_audit.py audit --skills-dir "$HOME/.cursor/skills"
python3 scripts/skills_audit.py sync --skills-dir "$HOME/.cursor/skills" --map-file config/sources.example.json
python3 scripts/skills_audit.py sync --skills-dir "$HOME/.cursor/skills" --map-file config/sources.example.json --apply
```

Would love feedback on:

1. additional checks (hash drift, stale backups)
2. output format (table/json)
3. cross-tool support improvements
```

---

## 2) Hacker News (Show HN)

Suggested title:

- `Show HN: skills-auditor, a dry-run-first sync tool for local AI skill folders`

Suggested first comment:

```md
I built this after repeatedly finding broken links and drifted local copies in my `~/.cursor/skills`.

The tool is intentionally simple:

- `audit`: inspect current state (symlink health, SKILL.md presence)
- `sync`: compare against a canonical mapping and propose/apply relink actions
- defaults to dry-run

Repo: https://github.com/ERerGB/skills-auditor

I’d especially appreciate feedback on:
- what checks are most useful in real setups
- how people manage skill source-of-truth across Cursor/Claude/OpenClaw
```

---

## 3) Reddit

Recommended communities (pick relevant ones):

- r/cursor
- r/CodingAgents
- r/ClaudeAI

Suggested title:

- `Open-source tool to audit and sync local AI skill folders (dry-run first)`

Post body:

```md
I just open-sourced `skills-auditor`:
https://github.com/ERerGB/skills-auditor

It audits and syncs local skill directories (like `~/.cursor/skills`) and focuses on safety:

- detect broken symlinks
- detect non-symlink local copies
- generate a dry-run sync plan from source mapping
- apply relinks only with explicit `--apply`

I built it because “skill folder drift” keeps happening in multi-repo setups.

Would this be useful for your workflow? What checks should be added next?
```

---

## 4) X/Twitter

Tweet draft:

```text
Open-sourced skills-auditor: a dry-run-first tool to audit + sync local AI skill folders.

It catches broken symlinks, local drift, and can safely relink to canonical sources.

Repo: https://github.com/ERerGB/skills-auditor

If you use Cursor/Claude/OpenClaw skills, feedback welcome 👀
```

---

## 5) FAQ Snippets for Comments

### Q: Is this Cursor-only?

A: No. It is directory-based, so any skill system with `SKILL.md` folders can use it.

### Q: Is it safe?

A: Yes by default. `sync` is dry-run unless `--apply` is provided. Existing folders are archived before relinking.

### Q: Why not just use package manager sync?

A: This solves local filesystem drift and broken refs, especially when multiple repos are used as canonical sources.
