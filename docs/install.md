# Install skills-auditor safely

The recommended install path is a local clone plus virtualenv. This keeps the tool reversible and avoids writing into a system Python environment.

## Virtualenv install

```bash
git clone https://github.com/ERerGB/skills-auditor.git
cd skills-auditor
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

This installs the `skills-audit` console script and enables:

```bash
python3 -m skills_auditor
```

## Verify the install

```bash
skills-audit audit --skills-dir "$HOME/.cursor/skills"
```

If the command is not found, either reactivate the virtualenv or run directly from the repo:

```bash
python3 scripts/skills_audit.py audit --skills-dir "$HOME/.cursor/skills"
```

## Optional pipx install

From the repo root:

```bash
pipx install .
```

Use `pipx` when you want a global CLI without coupling it to a project virtualenv.

## Safety boundary

`audit`, `audit-discovery`, and metadata validation are inspection commands. Sync commands are dry-run by default until `--apply` is passed.

For the agent skill pack, set this environment variable when you want to force dry-run behavior:

```bash
export SKILLS_AUDITOR_DRY_RUN=1
```
