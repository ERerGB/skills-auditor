# CI and automation

CI should validate sources, create machine-readable plans, and verify receipts. Applying to a
persistent developer home directory is an operational action, not a test.

## Test layers

Each layer crosses a different boundary. Passing a narrower layer is not evidence for a wider one.

### Unit and contract gate

```bash
python3 -m coverage run -m unittest discover -s tests -v
python3 -m coverage report
```

The source suite covers module behavior, command dispatch, validation, rollback, corrupted evidence,
and failure receipts. Branch-aware coverage must remain at or above 90%. CI runs this gate on Python
3.9 through 3.14.

### Clean-wheel smoke gate

```bash
python3 -m build
python3 scripts/run_artifact_tests.py smoke dist
```

The runner creates a disposable virtual environment, installs the wheel without dependencies,
copies the smoke suite outside the checkout, removes `PYTHONPATH` and `PYTHONHOME`, then checks the
installed import, version, help text, module entry point, and `skills-audit` console script. A
directory argument must contain exactly one `skills_auditor-*.whl`; pass an exact wheel path when a
local `dist/` contains older builds.

### Installed-CLI E2E gate

```bash
python3 scripts/run_artifact_tests.py e2e dist
```

The E2E suite invokes only the console script installed from the wheel. It exercises
`integrate → apply → verify → noop` across Cursor, Claude Code, and Codex project and global roots,
plus stale-plan rejection, archive behavior, and source-drift verification. CI runs it on Linux and
macOS. This establishes the filesystem integration contract; it does not launch downstream host
applications or claim model-level behavioral parity.

The distribution job separately builds both package formats, checks Markdown links, and runs
`scripts/check_distribution.py`.

## Source contract gate

```bash
skills-audit audit \
  --skills-dir .agents/skills \
  --fail-on-duplicate-names \
  --metadata-platform codex
```

## Integration plan gate

```bash
skills-audit integrate \
  --config skills-auditor.json \
  --format json \
  --plan-out .skills-auditor-local/ci-plan.json \
  > .skills-auditor-local/ci-plan-output.json
```

`integrate` returns `0` for both changes and `noop`; inspect `summary.changes` in the JSON object if
policy needs to distinguish them. Exit `2` means invalid input. Exit `3` means a contract conflict.

## Receipt gate

When a controlled apply job produces a receipt, verify it in the same environment:

```bash
skills-audit verify \
  .skills-auditor-local/receipts/<receipt-id>.json \
  --format json
```

Verification exits `3` when links or source-tree hashes no longer match the receipt.

## What not to automate first

Do not schedule apply until:

- the repository owns one reviewed `skills-auditor.json`;
- source and target roots are backed up or disposable;
- archive behavior is understood;
- the job preserves plan and receipt artifacts;
- a failed receipt has an explicit handoff owner.

See [integration-contract.md](integration-contract.md) for transaction and partial-failure
boundaries.
