# Experimental handbook pilot

This opt-in local prototype correlates Skill Trace hook observations with explicit task reports.
It does not score model intelligence or infer effective Skill use from a file read. It does not
edit Skills, upload records, start an LLM service, schedule jobs or promote itself to standard policy.

Use the CLI/plugin from the same reviewed source revision. Install Skill Trace 0.3.0 through its
[normal installer](install.md#optional-skill-trace-plugin), review its hooks, and explicitly enable
capture when requested. Ordinary capture and the pilot are separate controls. A pilot configuration
alone does not enable capture, trust hooks or change the global hook switch.

## Configure a bounded trial

Prepare a JSON array of maintained identities, merging duplicate aliases into one identity:

```json
[
  {
    "id": "example/skill",
    "maintenance_scope": "user_maintained",
    "source_revision": "FULL_MERGED_REVISION",
    "sha256": "64_LOWERCASE_HEX_CHARACTERS",
    "paths": ["/absolute/installed/skill/SKILL.md"]
  }
]
```

The example values are placeholders, not accepted hashes. The initializer verifies actual installed
bytes and rejects third-party classifications. It cannot independently prove that a caller's
ownership declaration is true; establish that from the source catalog before configuration.

```bash
skills-audit handbook-pilot init --catalog /absolute/reviewed-catalog.json \
  --state-dir /absolute/new-private-pilot --max-sessions 3 \
  --exclude-session-id CURRENT_REWRITE_TASK --exclude-cwd /absolute/synthetic-workspace
skills-audit skill-trace enable
skills-audit handbook-pilot status
```

The pilot configuration defaults to `$CODEX_HOME/skill-handbook-pilot.json`. Use
`SKILLS_AUDITOR_HANDBOOK_PILOT_CONFIG` in the host environment or the CLI's leading
`--config PATH` for an explicit alternative. Never overwrite an existing pilot automatically.
The state directory contains a local SQLite database; retain it independently for review.

Only an exact pinned maintained entrypoint read can admit a new session. Two or three distinct
sessions are permitted; the default is three. Repeated reads/turns are not independent tasks.
Current rewrite/evaluation sessions and synthetic workspaces must be excluded. Hooks keep bounded
identifiers, source revision/body hash, model identifier when supplied, correlated call phases and
error indicators. They do not store prompts or full tool results. Model identity can remain unknown.
Paths/identifiers are still private operational metadata.

A bounded context reminder is emitted after the first correlated non-error Skill observation in
an admitted task. Continue the requested work normally. Do not force Skill reads or invent tasks to
fill the quota. For actual Skill use, record its outcome and a local artifact or redacted verification
receipt that supports the result:

```bash
skills-audit handbook-pilot record --session-id ACTUAL_TASK --skill-id example/skill \
  --outcome completed --artifact /absolute/task-evidence.json --real-task
```

`blocked` and `unknown` are valid outcomes. For a concrete problem add `--issue-type` (`ambiguity`,
`missing_fact`, `invalid_command`, `unnecessary_step` or `boundary`) and a short stable `--issue-code`.
The code `none` pairs with issue type `none`. Artifact bytes remain local; the database records only
their path/hash. A duplicate exact report is idempotent; a conflicting report is preserved as an
error for review. Synthetic trials and source inspections must not be labeled `--real-task`.

## Review and pause

```bash
skills-audit handbook-pilot status
skills-audit handbook-pilot pause
```

After two reported tasks have a Stop observation, the hook emits one review-ready notice. Stop
means a turn boundary, not session completion; the separate outcome is an agent attestation, not
independent truth. The status always leaves effectiveness unassessed and lists review triggers
rather than applying changes. Inspect actual evidence before drawing a conclusion. An apparent
completed outcome with a boundary violation is not a successful calibration.

The admission limit stops new sessions, not all normal Skill Trace capture. `pause` stops pilot
observations while preserving reports. `skills-audit skill-trace disable` stops normal capture and
pilot hook calls; neither command changes trust. Failures are non-blocking. Dry-run/manual synthetic
hooks prove only the harness, not real host dispatch or task effectiveness. Simple path recognition
is limited; missing call IDs, compound shell reads and unsupported tools can leave coverage gaps.

The normalizer accepts the current `tool_use_id` as well as older tool-call aliases. Hook response
shape follows [Codex hook output](https://learn.chatgpt.com/docs/hooks). Only additional context or
a one-time status notice is emitted; no hook blocks, rewrites or approves the user's operations.

See [bounded rewriting and update triggers](handbook-calibration.md) for the maintenance contract.
