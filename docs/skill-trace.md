# Skill Trace: purpose and operating contract

Skill Trace is Skills Auditor's optional Codex plugin for collecting local runtime evidence.
The original [architecture proposal](../doc/sensor-plugin-architecture.md) separates capture,
canonical skill identity mapping, and eventual controller actions.

## Intent and current effects

The purpose is to answer which task and tool accessed a skill path, supplying evidence that a
filesystem inventory alone cannot provide. The shipped plugin registers four handlers:

| Handler | Current effect |
| --- | --- |
| `SessionStart` | Records a session boundary |
| `PreToolUse` | Records a tool request and an inferred path when recognizable |
| `PostToolUse` | Records the post-tool event and whether a response was present |
| `Stop` | Records a turn stop; it is not a session-end signal |

When enabled, the wrapper normalizes payloads with the shared sensor core and appends JSONL to
`.skills-auditor-local/sensors/<UTC-date>/codex.jsonl` in the task workspace. Events include time,
task id, cwd, tool/call identifiers, inferred skill path, and compact metadata. Raw prompts,
full tool inputs, and tool output bodies are not stored by the adapter. Paths and task metadata
still deserve the same care as other local development logs. There is no network upload in this
capture path. The handlers match all tools, so logs contain non-skill tool events as well.

`audit-sensor-logs` validates records and reports counters. `aggregate-sensor-claims` groups
evidence into confidence-rated claims. These are report-only. The plugin does not change prompts,
select skills, block tool calls, repair files, sync definitions, or implement a transcript watcher.
Default capture does not resolve symlinks or hash observed files; those are explicit adapter
options. Failures report to stderr and return success to avoid blocking the host task.

## Evidence limits

A file access does not establish that a model followed a skill, selected it correctly, or improved
the outcome. Pre-tool events describe attempts, not successful reads. Post-tool metadata alone
is not a semantic success assessment. The path parser is deliberately limited: simple reads can
be recognized, compound shell commands may be missed, and one payload yields at most one path.
Confidence labels describe evidence correlation, not model correctness.

The hook health check likewise reports recent capture evidence, not a security attestation or
an authoritative read of Codex's effective trust policy. Current trust and per-handler enablement
remain visible in Codex CLI `/hooks`. No synthetic probe is written by the checker.

## Independent preflight

Every auditor/trace skill invocation starts with `skills-audit skill-trace check`, independently
of Codex dispatching this plugin's hooks. Ordinary auditor CLI commands also run this check when
a current Codex task id is available. An optional collector cannot reliably diagnose its own
absence from inside a hook that the host refuses to execute.

| State | Meaning and action |
| --- | --- |
| `disabled` | Capture is off; continue auditing or inspecting existing logs quietly |
| `healthy` | Recent plugin `PreToolUse` and `PostToolUse` writes match this task and cwd |
| `unverified` | No matching evidence or no task id; check installation, trust, runner, and log root |
| `stale` | Some matching evidence exists, but no recent complete pre/post pair; repeat a real tool call and check the missing handlers |
| `error` | Settings or logs could not be read; inspect the reported path/error |

The checker reads only the last 256 KiB of today's and yesterday's Codex sensor files. It requires
both tool phases within five minutes and, for persistent settings, after the latest preference
change. Session-start evidence alone, old sessions, other directories, unmarked manual records,
future timestamps, and `--dry-run` output cannot make it healthy. Session-start and stop sightings
are included in JSON diagnostics but are not required to be recent during an active turn.
This checks the main tool capture path, not coverage of every tool or every lifecycle handler.

Explicit `check` exits `0` for disabled/healthy, `1` for unverified/stale, and `2` for read/config
errors. `status` exits `0` for diagnostic states and `2` for errors. Automatic CLI preflight warns
on stderr without changing the requested command's stdout or exit status. The agent reports a
capture gap once and continues ordinary work; it never interprets the gap as zero skill usage.

## Installation and controls

Follow the [plugin installation guide](install.md#optional-skill-trace-plugin) for the local
marketplace, explicit capture choice, user-level trust review, upgrade, and verification steps.
The plugin-specific JSON preference never edits Codex `config.toml`, trusted hashes, or the global
hook feature flag. The independent collector defaults off; enabling capture and trusting handlers
are both needed. Disabling capture preserves history and all ordinary Skills Auditor commands.

If events are missing, follow [capture troubleshooting](troubleshooting.md#skill-trace-captures-no-events).
Return to the [README overview](../README.md#optional-skill-trace) for the operator entry point.
