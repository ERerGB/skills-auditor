# CI and automation

Use CI as a read-only quality gate. It should prove that skill names, metadata, and route assumptions are stable before a human applies changes.

## Baseline test job

```bash
python3 -m unittest discover -s tests -v
```

## Audit gate

```bash
skills-audit audit \
  --skills-dir "$HOME/.codex/skills" \
  --fail-on-duplicate-names \
  --metadata-platform codex
```

This catches duplicate skill names and invalid frontmatter before the root is used by automation.

## Discovery gate

```bash
skills-audit audit-discovery \
  --profile-file config/discovery-profile.gstack-multiplatform.example.json
```

Use this when source roots are shared across tools and the CI job needs to catch discovery collisions.

## What not to automate first

Do not put `--apply` in scheduled CI until:

- The dry-run output has been reviewed.
- The target root is backed up or disposable.
- The team agrees on the canonical source map.
- Directory replacement archive behavior is understood.

Apply-mode sync is an operational action, not just a test.
