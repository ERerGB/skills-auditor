---
name: skill-trace
description: Inspect local Skills Auditor sensor logs captured by the Skill Trace plugin.
---

# Skill Trace

Use this skill when the user asks about locally captured skill trace or sensor events.

Before every invocation, run `skills-audit skill-trace check`. This check executes independently
of the plugin's Codex hooks, so missing hook trust cannot suppress the check itself.
`disabled` means capture is intentionally off; continue to inspect existing logs. `healthy` means
recent PreToolUse and PostToolUse writes from this plugin match this task and working directory.
For `unverified`, `stale`, or `error`, disclose the evidence gap once and continue the requested
inspection. Never infer no skill use from missing events or manufacture a successful health check.

Capture is optional and off by default. If the user asks to change it, use:

```bash
skills-audit skill-trace enable
skills-audit skill-trace disable
skills-audit skill-trace status
```

These commands only control Skill Trace capture. They do not grant trust or change Codex-wide
hooks. Review this plugin's four hook definitions in Codex CLI `/hooks` after installation or
changes. Installation alone does not trust hooks. Disabling capture preserves existing logs.

When enabled and trusted, the plugin writes sensor events under `.skills-auditor-local/sensors/` in the active working
directory by default. Use the Skills Auditor CLI to inspect them:

```bash
skills-audit audit-sensor-logs
skills-audit aggregate-sensor-claims
skills-audit log-stats
```

Sensor events are raw runtime facts from hooks or transcripts. They do not by themselves prove
semantic skill usage; align them with canonical skill identity before making automation decisions.
Use `aggregate-sensor-claims` for the first confidence-rated, report-only view.
