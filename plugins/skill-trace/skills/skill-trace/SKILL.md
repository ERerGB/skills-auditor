---
name: skill-trace
description: Inspect local Skills Auditor sensor logs captured by the Skill Trace plugin.
---

# Skill Trace

Inspect local Skill access observations. Start each invocation with `skills-audit skill-trace check`; it works independently of hook dispatch. Disabled capture is normal. For unverified/stale/error, state the evidence gap once and inspect existing records; absent events do not prove non-use.

Use `skills-audit audit-sensor-logs` for record validity/counters and `aggregate-sensor-claims` for confidence-rated access claims. Logs default to `.skills-auditor-local/sensors/` in the task workspace. A read attempt, correlated tool event or successful command is not proof the model followed the manual or improved the result.

When the user explicitly requests capture, use `skills-audit skill-trace enable`; `disable` preserves existing logs. These controls never grant native hook trust. After install or hook changes, review only this plugin's four handlers in Codex CLI `/hooks`, then verify real PreToolUse/PostToolUse dispatch in a fresh task. A manual hook invocation or dry-run is only a smoke test.

For an explicitly configured handbook pilot, run `skills-audit handbook-pilot status`. It admits at most two or three distinct tasks against pinned maintained Skill hashes. Hook observations and separately reported task outcomes remain distinct. Only record a genuinely performed task with its local artifact; do not count synthetic evaluation or source inspection as real use. The report can suggest a concrete maintenance review, but never edits a Skill or declares the process effective automatically. Run `skills-audit handbook-pilot --help` for controls; implementation and pilot details are in the canonical Auditor repository's `docs/handbook-pilot.md`.

Preserve private task data locally. Capture preference, native hook trust, installed version integrity and model/task acceptance are separate checks. Keep the duplicate compatibility alias archived; use the plugin's enabled entrypoint.
