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
