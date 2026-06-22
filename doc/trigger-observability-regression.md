# Trigger Observability Regression Method

This document records the current staged method for collecting skill trigger,
observability trigger, and route trace logs, then replaying them through one or more
evaluation adapters.

## Scope

The first phase is local-only and append-only:

- `skill_trigger`: prompt-level expectation vs. actual skill routing.
- `observability_trigger`: whether trace/telemetry capture should fire for a prompt or run.
- `trace`: a lightweight reference to a route state-machine trace file.

Do not store raw prompt bodies in the default logs. Use `prompt_hash`, `prompt_summary`, and
`context_summary` instead.

## Adapter Model

`skills-auditor` owns the local event ledger, privacy boundary, trace references, and resource
accounting. It should not become a full replacement for established eval runners.

Adapters convert local logs into the case format expected by a regression tool:

```text
.skills-auditor-local/logs/*.jsonl
  -> adapter
  -> eval cases / provider config / expected assertions
  -> external or local eval runner
  -> result summary linked back to source event ids
```

Promptfoo is the first adapter target because it already covers the most direct skill-trigger
regression path: compare skill versions, assert that a target skill was or was not used, and
track cost / latency / trace evidence where supported by the provider.

Future adapters can target other evaluation systems when they add materially different value,
such as agent trajectory metrics, RAG/tool-use metrics, human annotation queues, or production
observability backfills.

## Local Storage

Default local root:

```text
.skills-auditor-local/
```

This path is gitignored. The expected layout is:

```text
.skills-auditor-local/
  logs/
    YYYY-MM-DD/
      skill_trigger.jsonl
      observability_trigger.jsonl
      trace.jsonl
```

## Collection SOP

1. Record trigger decisions when a prompt is reviewed:

```bash
skills-audit record-trigger-log \
  --kind skill-trigger \
  --prompt-hash "sha256:<prompt-hash>" \
  --prompt-summary "<short summary>" \
  --expected-skill "<Skill>" \
  --actual-skill "<Skill>" \
  --expected-mode "<Mode>" \
  --actual-mode "<Mode>" \
  --verdict correct
```

2. Record observability decisions when instrumentation should or should not fire:

```bash
skills-audit record-trigger-log \
  --kind observability-trigger \
  --prompt-hash "sha256:<prompt-hash>" \
  --context-summary "<short context summary>" \
  --decision "<triggered|skipped>" \
  --verdict unknown
```

3. Link route trace files when a state-machine run is relevant:

```bash
skills-audit record-trigger-log \
  --kind trace \
  --trace-path "~/.skills-auditor/traces/<run-id>.json" \
  --notes "<why this trace matters>"
```

## Regression Checks

Run the local log audit:

```bash
skills-audit audit-trigger-logs --fail-on-error
```

Current checks:

- JSONL parseability.
- Known event kinds.
- Duplicate `event_id`.
- Missing prompt references for trigger logs.
- Missing skill references for `skill_trigger`.
- Missing trace references for `trace`.
- Accidental `raw_prompt` storage.
- Labeled accuracy and false-positive / false-negative counts.

Run the existing route state-machine audit:

```bash
skills-audit audit-state-machine
```

Use both outputs together:

- trigger logs answer whether the right skill or observability path was selected;
- route traces answer whether the selected route pipeline behaved legally and consistently.

Adapter-backed regression should add a third layer:

- Promptfoo answers whether a candidate `SKILL.md` description changes trigger behavior,
  quality, cost, latency, or trace evidence on a labeled case set.

## Resource Accounting

Run:

```bash
skills-audit log-stats --events-per-day <N> --retention-days <D>
```

Storage estimate:

```text
storage_bytes ~= events_per_day * retention_days * average_record_bytes * (1 + index_multiplier)
```

Compute estimate:

```text
compute_seconds ~= events * (parse_seconds + regression_seconds + optional_llm_judge_seconds)
```

If later regressions use an LLM judge, track token usage separately:

```text
token_cost ~= sum(input_tokens_i * input_price + output_tokens_i * output_price)
```

## Stage-Gate Review

After a meaningful local sample has accumulated, review:

- trigger Hit@1 on labeled events;
- false-positive and false-negative clusters;
- observability coverage gaps;
- storage growth against retention budget;
- route trace state-machine errors and cross-run inconsistencies;
- whether skill descriptions need a proposed patch instead of live mutation.
