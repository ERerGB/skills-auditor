---
name: skill-trace
description: Inspect local Skills Auditor sensor logs captured by the Skill Trace plugin.
---

# Skill Trace

Use this skill when the user asks about locally captured skill trace or sensor events.

The plugin writes sensor events under `.skills-auditor-local/sensors/` in the active working
directory by default. Use the Skills Auditor CLI to inspect them:

```bash
skills-audit audit-sensor-logs
skills-audit aggregate-sensor-claims
skills-audit log-stats
```

Sensor events are raw runtime facts from hooks or transcripts. They do not by themselves prove
semantic skill usage; align them with canonical skill identity before making automation decisions.
Use `aggregate-sensor-claims` for the first confidence-rated, report-only view.
