# Releasing

The package version has one source of truth: `skills_auditor/_version.py`. A release is ready only
when tests and distribution checks pass from the same commit.

## Release gate

From a clean checkout:

```bash
python3 -m venv .release-venv
source .release-venv/bin/activate
python -m pip install ".[test]" build
python -m coverage run -m unittest discover -s tests -v
python -m coverage report
python scripts/check_markdown.py

SA_RELEASE_DIST="$(mktemp -d)"
python -m build --outdir "$SA_RELEASE_DIST"
python scripts/check_distribution.py "$SA_RELEASE_DIST"
python scripts/run_artifact_tests.py smoke "$SA_RELEASE_DIST"
python scripts/run_artifact_tests.py e2e "$SA_RELEASE_DIST"
```

The checker rejects a wheel or source distribution that omits the CLI, high-level integration
module, versioned JSON schemas, license, README, agent skill, integration example, contract, artifact
runner, or packaged Smoke/E2E suites. It also checks the wheel version, SPDX license metadata,
license file, and console entry point. The artifact runner then proves that the exact wheel can be
installed and operated outside the source checkout.

## Tag

After the gate passes and the release commit is on `main`:

```bash
SA_VERSION="$(python -c 'from skills_auditor import __version__; print(__version__)')"
git tag -s "v$SA_VERSION" -m "skills-auditor $SA_VERSION"
git push origin "v$SA_VERSION"
```

Use an annotated tag when signed tags are not available; do not move an existing release tag.

## Publication boundary

The repository builds installable wheel and source artifacts and supports Git URL installation.
It does not currently publish to PyPI automatically. Add a publishing workflow only after the
package index project and trusted-publisher identity are owned and reviewed; do not add a long-lived
upload token merely to automate this step.
