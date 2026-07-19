# Getting started

skills-auditor separates review from filesystem writes. A normal adoption run creates one plan,
applies that exact file, and verifies the resulting receipt.

## Install

```bash
git clone https://github.com/ERerGB/skills-auditor.git
cd skills-auditor
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
skills-audit --version
```

## Quickstart

Assume canonical definitions live under `.agents/skills` and Codex should discover them from the
project's `.codex/skills` root.

```bash
skills-audit integrate \
  --source .agents/skills \
  --target codex \
  --plan-out .skills-auditor-local/plan.json

skills-audit apply .skills-auditor-local/plan.json \
  --receipt-out .skills-auditor-local/receipt.json

skills-audit verify .skills-auditor-local/receipt.json
```

`integrate` only writes the plan and exits `0`. Review it before running `apply`. Apply rejects the
plan if a source-tree hash or affected target entry changed after review. A successful verify reports
`status: passed`.

Repeat `integrate` after apply and every current link is planned as `noop`.

## Multiple hosts

Targets are semantic host names, not raw install paths:

```bash
skills-audit integrate \
  --source .agents/skills \
  --target cursor \
  --target claude-code \
  --target codex
```

Use `codex@global` for `~/.codex/skills`, or `--target-root acme=.acme/skills` for a custom host.

## Repository configuration

Copy [`../config/skills-auditor.integration.example.json`](../config/skills-auditor.integration.example.json)
to `skills-auditor.json`, edit the source and target lists, then run:

```bash
skills-audit integrate
```

Read the [integration contract](integration-contract.md) before automating apply.

## Next paths

- Installation options: [install.md](install.md)
- Advanced command recipes: [examples.md](examples.md)
- CI and machine JSON: [ci.md](ci.md)
- Troubleshooting: [troubleshooting.md](troubleshooting.md)
