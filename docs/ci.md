# CI and automation

CI should validate sources, create machine-readable plans, and verify receipts. Applying to a
persistent developer home directory is an operational action, not a test.

## Repository test gate

```bash
python3 -m unittest discover -s tests -v
```

The repository's [CI workflow](../.github/workflows/ci.yml) installs the package and runs the suite
on Python 3.9 through 3.14. Its distribution job also builds both package formats and runs
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
