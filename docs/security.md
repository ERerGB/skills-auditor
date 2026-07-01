# Security and dependency review

skills-auditor is a local developer tool. Its main risk surface is filesystem changes during sync and metadata repair, not a network service.

## Default posture

- Audit commands inspect local folders.
- Sync commands are dry-run by default.
- Apply-mode changes require `--apply`.
- Existing directories are archived before relinking.
- The skill pack can be forced into dry-run mode with `SKILLS_AUDITOR_DRY_RUN=1`.

## Dependency review

The package is intentionally small. Review dependency state through the repository:

- Dependency graph: https://github.com/ERerGB/skills-auditor/network/dependencies
- Project metadata: [`pyproject.toml`](../pyproject.toml)

## Dependency review anchor

Use this section when evaluating whether a team can adopt the tool without importing an unexpected dependency surface.

## Local data

Sensor and trace features are local and gitignored. They are intended for prompt-level routing review and operational debugging, not for storing identity-level analytics.

## Reporting issues

Use GitHub issues for security or trust concerns unless the repository later adds a dedicated security policy:

https://github.com/ERerGB/skills-auditor/issues

## Adoption checklist

- Run audit before sync.
- Review dry-run output before apply.
- Avoid scheduled apply jobs until the team owns the source map.
- Keep local trace artifacts out of commits.
