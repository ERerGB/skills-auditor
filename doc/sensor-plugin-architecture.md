# Sensor Plugin Architecture Plan

This plan separates three concerns:

- Sensor: capture runtime facts from agent hosts.
- Coordinate system: map paths and hashes to canonical skills/tasks.
- Controller: automate sync, repair, regression, and rollout decisions.

The current implementation should treat sensor support as a reusable core, not as the final
product surface. The product surface should be a local plugin for Codex and Claude Code.

## Problem

We want to understand how local AI agents consume skills during real work:

- which skill files were read;
- which tool calls happened before and after a skill read;
- which local path, symlink target, and content hash were involved;
- whether different hosts, such as Codex and Claude Code, used equivalent skill identities;
- whether later automation can safely sync, patch, or evaluate those skills.

Raw filesystem access is not enough. A path read tells us that a file was accessed, but not why
the agent accessed it, which session caused it, which tool produced it, or whether it affected
the final behavior. The first-class signal should come from host-level hooks and transcripts.
Filesystem proxying remains a fallback for closed surfaces or out-of-band reads.

## Goals

- Capture host-exposed runtime facts from Codex and Claude Code.
- Store append-only local JSONL events under a gitignored local root.
- Avoid storing raw prompts by default.
- Keep provider-specific parsing outside the canonical identity layer.
- Make events useful for later path alignment, regression, and automation.
- Support local plugin installation without public marketplace review.

## Non-Goals

- Do not build a full observability SaaS.
- Do not start with kernel/FUSE/syscall capture as the main path.
- Do not infer semantic skill usage from file access alone.
- Do not automatically mutate skill files based only on sensor events.
- Do not require Promptfoo, LangSmith, or another eval runner for basic capture.

## Architecture

```text
Agent Host
  Codex / Claude Code / Cursor
        |
        | hooks, transcript files, optional FS proxy
        v
Local Plugin
  hooks/
  skills/
  scripts/
  optional worker or MCP server
        |
        v
Sensor Core
  normalize provider payloads
  extract tool, cwd, session, file path
  append sensor JSONL
        |
        v
Coordinate Layer
  realpath
  symlink resolution
  content hash
  canonical skill id
  platform/source root mapping
        |
        v
Controller Layer
  audit
  regression
  sync
  repair
  rollback
```

## Plugin Product Shape

The plugin should be a thin distribution wrapper around the sensor core.

For Codex:

```text
skill-trace/
  .codex-plugin/
    plugin.json
  hooks/
    hooks.json
  scripts/
    sensor-hook
    transcript-watch
  skills/
    skill-trace/
      SKILL.md
```

For Claude Code:

```text
skill-trace/
  .claude-plugin/
    plugin.json
  hooks/
    hooks.json
  scripts/
    sensor-hook
    transcript-watch
  skills/
    skill-trace/
      SKILL.md
```

Both plugin variants can call the same underlying Python module or CLI:

```bash
skills-audit record-sensor-event --provider <provider> --source hook
```

The plugin should not own the canonical alignment logic. It only captures and forwards events.

## Sensor Sources

### 1. Hooks

Primary source.

Expected events:

- `SessionStart`
- `UserPromptSubmit`
- `PreToolUse`
- `PostToolUse`
- `Stop`
- `SessionEnd`

Useful fields:

- provider
- session id
- cwd
- tool name
- tool input
- tool result presence
- file path
- transcript path
- timestamp

Hook events are the highest-signal capture point because they preserve session and tool context.

### 2. Transcripts

Secondary source.

Codex and Claude Code both write local session artifacts in practice. Transcript watchers are
useful when a hook was not installed before the run, when reconstructing history, or when a host
exposes richer result records in transcripts than in hook stdin.

Transcript ingestion should be idempotent and track offsets or processed event ids.

### 3. Filesystem Proxy

Fallback source.

Use only when host hooks/transcripts are unavailable or insufficient. FS proxy events should be
tagged differently because they lack agent-level semantics:

```json
{
  "source": "fs_proxy",
  "event_type": "file_access",
  "path": "...",
  "operation": "read"
}
```

FS proxy capture is useful for proving that a path was touched, but it should not be the primary
basis for semantic skill-use claims.

## Event Model

The sensor layer emits provider-normalized events:

```json
{
  "schema_version": 1,
  "event_id": "<uuid>",
  "timestamp": "<utc iso timestamp>",
  "provider": "codex|claude-code|cursor|generic",
  "source": "hook|transcript|fs_proxy",
  "event_type": "pre_tool_use|post_tool_use|file_access|skill_file_access|...",
  "session_id": "<host session id>",
  "cwd": "<working directory>",
  "tool_name": "Read",
  "operation": "read",
  "path": "/Users/me/.codex/skills/foo/SKILL.md",
  "realpath": "",
  "content_hash": "",
  "skill_name": "foo",
  "skill_path": "/Users/me/.codex/skills/foo/SKILL.md",
  "transcript_path": "",
  "call_id": "",
  "status": "",
  "metadata": {}
}
```

Important distinction:

- `skill_file_access` means the path looks like a skill file or skill directory.
- It does not mean the skill was semantically invoked.
- Semantic invocation is derived later by correlating path access, tool sequence, prompt context,
  model output, and host-specific skill invocation metadata when available.

## Storage

Default local root:

```text
.skills-auditor-local/
  sensors/
    YYYY-MM-DD/
      codex.jsonl
      claude-code.jsonl
  logs/
    YYYY-MM-DD/
      skill_trigger.jsonl
      observability_trigger.jsonl
      trace.jsonl
```

The root is gitignored. Raw prompt bodies should not be written by default.

## Coordinate Layer Contract

The coordinate layer should consume sensor events and enrich them into canonical identities:

```json
{
  "sensor_event_id": "...",
  "physical_path": "...",
  "realpath": "...",
  "content_hash": "sha256:...",
  "install_root": "~/.codex/skills",
  "source_root": "/Users/me/code/skill-pack/skills",
  "provider": "codex",
  "logical_skill_id": "foo",
  "variant_id": "foo@codex",
  "version_hint": "git:<repo>@<sha>"
}
```

This should reuse existing skills-auditor capabilities:

- symlink health;
- duplicate frontmatter names;
- content hash comparison;
- platform-aware discovery profiles;
- route traces.

## Controller Layer Contract

The controller should only act on enriched, stable identity events.

Candidate actions:

- ask for review when a frequently used skill has drifted;
- run trigger regressions after a skill description changes;
- propose relinking duplicates;
- promote a canonical source to another host;
- archive unused variants;
- open an issue or PR for risky changes.

The controller should not mutate files based on raw `file_access` alone.

## Open-Source Reuse Strategy

We should not implement every layer from scratch.

Reusable pieces:

- Host hooks and plugin mechanisms for capture.
- Transcript parsers/watchers where available.
- Promptfoo for regression/eval assertions.
- Existing FUSE/logged filesystem projects only for fallback FS capture.

The part that is likely custom is the coordinate layer: mapping host-specific skill roots,
symlinks, path variants, and skill frontmatter into one canonical local identity graph.

## Implementation Phases

### Phase 0: Architecture and Boundaries

- Land this planning document.
- Decide whether the current CLI sensor code stays as core support or is moved behind a plugin
  package.
- Define provider payload examples for Codex and Claude Code.

Exit criteria:

- The team agrees on sensor versus coordinate versus controller boundaries.

### Phase 1: Sensor Core

- Normalize hook/transcript JSON into `SensorEvent`.
- Append local JSONL.
- Validate sensor logs.
- Add unit tests for representative Codex and Claude Code payloads.

Exit criteria:

- A hook payload can be piped into the core and produce a stable JSONL event.

### Phase 2: Local Plugin Packaging

- Build Codex local plugin manifest and hooks.
- Build Claude Code local plugin manifest and hooks.
- Keep scripts thin; they call the shared sensor core.
- Add install/update docs.

Exit criteria:

- A local user can install the plugin without marketplace review and see sensor events during a
  real agent session.

### Phase 3: Transcript Watcher

- Add idempotent tailing for known transcript locations.
- Correlate tool calls and tool results by call id where available.
- Backfill historical sessions into sensor logs.

Exit criteria:

- A session can be reconstructed from transcript files even if hooks were not installed.

### Phase 4: Coordinate Enrichment

- Resolve observed paths into canonical skill identities.
- Join sensor logs with existing discovery, hash, symlink, and route data.
- Emit enriched identity events or an index.

Exit criteria:

- The system can answer: "Which canonical skill identity did this observed path correspond to?"

### Phase 5: Controller Automation

- Add conservative automation on top of enriched events.
- Start with report-only suggestions.
- Add apply modes only after regression and rollback paths are proven.

Exit criteria:

- Automation decisions are explainable from sensor event ids plus coordinate mappings.

## Risks

- Hook schemas may differ by host version.
- Transcript formats may change.
- Path reads can overstate semantic skill usage.
- Plugins may capture sensitive local paths if retention and filtering are not designed carefully.
- FS proxy capture can produce high-volume, low-context noise.

## Design Decisions

- Use hooks first, transcript watchers second, FS proxy third.
- Keep plugin scripts thin and provider-specific.
- Keep the core event ledger local and append-only.
- Keep raw prompts out of the default logs.
- Treat current `record-sensor-event` support as core plumbing, not the final UX.

## Immediate Next Step

Before expanding code, create concrete payload fixtures:

- one Codex hook or transcript event for reading a `SKILL.md`;
- one Claude Code `PreToolUse` event for reading a `SKILL.md`;
- one non-skill file read;
- one tool result event.

Those fixtures should drive the plugin hook scripts and the coordinate enrichment tests.

## Aggregation and Trust Plan

The sensor layer can produce overlapping evidence. A hook event, a transcript event, and an FS
proxy event may all describe the same underlying access. The next layer should aggregate raw
events into claims before any controller makes decisions.

### Claim Model

An aggregated claim is a statement about a likely runtime fact:

```json
{
  "schema_version": 1,
  "claim_id": "sha256:<stable-key>",
  "claim_type": "skill_file_access",
  "provider": "codex",
  "session_id": "sess-1",
  "call_id": "call-1",
  "operation": "read",
  "path": "/Users/me/.codex/skills/foo/SKILL.md",
  "realpath": "/Users/me/source/skills/foo/SKILL.md",
  "content_hash": "sha256:...",
  "skill_name": "foo",
  "skill_path": "/Users/me/.codex/skills/foo/SKILL.md",
  "evidence_event_ids": ["hook-event-id", "transcript-event-id"],
  "evidence_sources": ["hook", "transcript"],
  "confidence": "strong",
  "score": 0.95,
  "status": "supported",
  "notes": []
}
```

This is still not a controller decision. It is an evidence-backed assertion that downstream
systems can consume.

### Aggregation Key

Initial grouping should be conservative:

```text
provider
session_id
call_id when present
operation
normalized path or realpath
skill_name
short timestamp window when call_id is missing
```

If `call_id` exists, it should dominate grouping. If it does not exist, group only events that
share provider, session, operation, and path within a small time window.

### Confidence Rating

Start with explicit, explainable levels:

| Confidence | Evidence | Meaning |
| --- | --- | --- |
| `strong` | hook + transcript agree on same tool/path/session or one host-native explicit skill invocation | High enough for reporting and regression triggers |
| `medium` | transcript-only or hook-only with tool/path/session | Good runtime evidence, but missing cross-source confirmation |
| `weak` | fs-proxy-only or path-only evidence | Proves file access, not agent semantics |
| `manual` | manually injected/test event only | Useful for fixtures and labeled regression, not production claims |
| `disputed` | sources conflict on path/hash/operation for same call/session | Requires review |

Suggested numeric scores:

```text
strong: 0.95
medium: 0.70
weak: 0.40
manual: 0.30
disputed: 0.10
```

### Source Semantics

- `hook`: high-context, real-time signal.
- `transcript`: high-context, durable/backfillable signal.
- `fs_proxy`: low-context, durable filesystem fact.
- `manual`: test/debug/labeled adapter signal.

Multiple weak signals should not automatically become strong unless at least one high-context
source is present. For example, multiple FS proxy reads still remain weak unless they correlate
with a transcript or hook.

### Conflict Rules

A claim becomes `disputed` when grouped evidence disagrees on any of:

- different non-empty `content_hash`;
- different non-empty `realpath`;
- incompatible operations, such as read vs write;
- same call id but different skill names or paths.

Disputed claims should be report-only and should never drive automation.

### Controller Gate

Controllers should consume claims, not raw sensor events:

```text
SensorEvent[] -> Claim[] -> report/regression/sync/repair proposal
```

Minimum safe gate:

- `strong`: may trigger regression or report-only sync recommendations.
- `medium`: may appear in reports and queues.
- `weak`: only used as supporting context.
- `manual`: test/labeled data only unless explicitly promoted by a human.
- `disputed`: review only.

### TDD Implementation Plan

1. Add tests for `aggregate_sensor_claims(events)`.
2. Verify hook + transcript for same call/path becomes one `strong` claim.
3. Verify transcript-only becomes `medium`.
4. Verify fs-proxy-only becomes `weak`.
5. Verify manual-only becomes `manual`.
6. Verify same call with conflicting hash/path becomes `disputed`.
7. Add a CLI dry-run command that reads sensor logs and prints claims without writing anything.

### Dry-Run Validation

After implementation and plugin reinstall:

1. Run unit tests.
2. Run plugin validator.
3. Run installed hook script with `--dry-run`.
4. Write a small local sample set with hook/transcript/manual events.
5. Run claim aggregation in dry-run mode.
6. Confirm output includes one expected `strong` claim and no controller actions.
