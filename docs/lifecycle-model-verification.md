# Model-driven lifecycle verification

This acceptance ledger accompanies the [state model](lifecycle-state-model.md).
It records executable evidence, not a new feature scope. The model defines the
requirements; a passing test count alone cannot close this ledger.

This report accepts the historical model-first tree at `8527a83`. Subsequent
main/PR #21 composition is tracked separately in the
[trace compatibility verification](lifecycle-trace-verification.md); these
earlier numbers must not be reused as acceptance for that newer tree.

## Baseline and regression evidence

The fresh baseline at `c1ea6ff4516905a86c904d2c21c9c67a72242a96` passed 582
source tests: 7036/7528 statements (93.46%) and 2874/3324 branches (86.46%).
Combined coverage was 91.3196%, with the existing 90% floor and no excluded lines.
These are baseline measurements, not evidence that the model refinement passes.

Before engine or consumer changes, an independent run of the three new model
suites executed 33 tests and reproduced 18 failure subcases and 13 errors. The
retention recovery-ID producer had already been minimally corrected after its
earlier RED run; the remaining failures were shared identity, nested inverse
history, missing project context and missing pending discovery.

## Executable acceptance map

| Family | Primary source entry | Installed boundary | Required observations |
| --- | --- | --- | --- |
| S01 lifecycle | `test_s01_all_fourteen_operations_follow_literal_state_and_history_model` in [sequences](../tests/test_lifecycle_model_sequences.py) | Candidate/update/rollback/renewal, identity operations and legacy migration in [installed lifecycle](../e2e_tests/test_installed_managed_lifecycle.py) | Stable identities, literal state transitions, new or preserved grants, pointer and normalized bytes, unchanged candidate and immutable history |
| S02 shared content | `test_s02_*` in [sequences](../tests/test_lifecycle_model_sequences.py) | `test_installed_s02_s03_shared_skill_rollout_inverse_and_inverse_of_inverse_keep_history` | Independent plan timestamps, operation/source/parent variations, migration adoption, ordered and reversed batches, real interruption, explicit resume/compensation |
| S03 inverses | `test_s03_*` in [sequences](../tests/test_lifecycle_model_sequences.py) and nested-history case in [retention sequences](../tests/test_lifecycle_model_retention.py) | The same installed S02/S03 sequence inspects every ancestor and traverses retention after each inverse | Historical completion survives later compensation; malformed proofs fail before effects; a chain limit cannot create unreadable accepted history |
| S04 denial | `test_s04_*` in [sequences](../tests/test_lifecycle_model_sequences.py) | Durable warning, completed retry and pending renewal cases in [installed lifecycle](../e2e_tests/test_installed_managed_lifecycle.py) | Candidate drift does not poison H1; observed active damage and revocation persist through byte restoration and cancellation; only a new approved grant renews |
| S05 context | `test_s05_*` in [context sequences](../tests/test_lifecycle_model_context.py) | `test_installed_s05_exported_status_investigation_and_unknown_context_from_other_cwd` | Owner survives JSON/text next actions and investigation pages; correct shell quoting and explicit templates; missing stores are not initialized |
| S06 lost response | `test_s06_*` in [context](../tests/test_lifecycle_model_context.py) and [retention](../tests/test_lifecycle_model_retention.py) sequences | `test_installed_s06_discover_generated_core_batch_and_retention_ids_after_process_death` | Generated intent IDs remain discoverable after restart; bounded read-only pages preserve relationships; exact approved recovery terminates without false completion |
| S07 retention | `test_s07_*` in [retention sequences](../tests/test_lifecycle_model_retention.py), supplemented by [reference and failure tests](../tests/test_lifecycle_retention.py) | Installed quarantine/restore/purge and extra-deletion-approval sequence | Shared and pending roots remain protected; explicit expiry releases only its root; physical ownership, partial deletion and immutable metadata remain honest |
| S08 negative admission | `test_s08_*` in [sequences](../tests/test_lifecycle_model_sequences.py), plus existing [batch](../tests/test_lifecycle_batch.py) tests | Installed approval/checksum/unknown-state and batch exact-approval controls | Wrong or absent approval, stale or conflicting identity and malformed proof never produce unauthorized effects or false receipts |

The source and installed adapters share only the independent standard-library
[oracle](../tests/lifecycle_model.py). The artifact runner copies that test helper
outside the checkout; production imports must still resolve from the installed
wheel. Expected transitions and trees must not come from production validators.

## Independent review closure

Separate authors and reviewers checked the following acceptance gaps after the
initial RED inventory. The source-side review assertions are independently
closed; installed composition passed the fresh-wheel E2E run. Remaining final
gates are tracked below.

| Review item | Why it matters | Closure evidence required |
| --- | --- | --- |
| Full physical state | Checking one `payload` file missed damage to `SKILL.md`; metadata-only `check()` missed pointer/snapshot drift | Complete source-derived normalized trees, modes, symlinks and explicit partial pointer alternatives are checked after actions. Mutation controls damage non-payload bytes, permissions, topology and the moved candidate's actual tree |
| Honest completion oracle | Fabricated retention and batch success could escape the initial oracle; core completion lacked event assertions | Fresh-observer controls reject orphan receipts, missing completion events, nonterminal/missing/mismatched batch children and false deletion progress. A positive control preserves original completion after a later inverse honestly leaves uncompensated work |
| Installed composition | Existing installed batches used different Skills and only one inverse | Installed same-Skill rollout and inverse-of-inverse sequence checks all relevant ancestors and retention traversal |
| Portable and bounded tests | A macOS-only temporary path broke the Linux test contract; arbitrary late exceptions could masquerade as a chain bound | Platform-default temporary directories; an actual 50-batch chain, exact `batch_history_limit`, no new intent/effect on rejection, then a legal rename and readable ancestor/retention history |

The same-invariant audit also reproduced and corrected two sibling paths:
retired candidate aliases must not poison historical discovery or snapshot-ready
recovery; a long-lived Manager must not export a different project's navigation
after its owner path is retargeted. The latter now returns
`project_context_changed`, unverified bound context and no executable next action.
Tests exercise every navigation consumer, both databases remaining unchanged,
owner-read errors before fresh verification and a retarget during status reading.
The original primary/secondary failure text and stable recovery IDs remain
available for core, batch and retention; corrupt pending records get read-only
inspection, not automatic repair.

## Concrete process-death inventory

These are executed `os._exit` boundaries, not a claim to enumerate every possible
OS scheduler interleaving. Fault-injected I/O and negative metadata tests provide
additional evidence but are not counted as process death.

| Domain | Executed durable boundaries / composition | Post-restart assertions |
| --- | --- | --- |
| Core | Prepared; staged/readiness; step 0 intent, staged, effect, completed; before commit; committed ([recovery suite](../tests/test_lifecycle_recovery.py)) | Exact saved intent, no premature receipt, explicit approval, resume/compensate, owned pointer and no duplicate effects |
| Shared rollout | Child 0 committed; child 1 effect and staged; parent committed; completed plus unfinished children with a retired candidate alias | H2/H1 partial result is explicit; immutable H2 origin and child plans; resume or approved inverse; completed retries do not consult retired sources |
| Readiness | Snapshot publication before readiness is recorded, versus durable `transaction:staged` | Exact reviewed source remains mandatory before readiness; after readiness intact recorded snapshots suffice; cancellation cannot revive denial |
| Batch | Parent prepared; child 0 started/effect/committed/recorded; child 1 effect; before commit; committed; nested inverse prepared | Original generated parent is discoverable even with a child WAL; no replacement intent; historical completion and current compensation remain distinct |
| Verification | Started in the [recovery suite](../tests/test_lifecycle_recovery.py); observed before final publication in `test_s04_observation_real_death_preserves_uncertainty_fence_until_new_approval` in [model sequences](../tests/test_lifecycle_model_sequences.py) | Incomplete observation blocks cached selection; bytes restored later do not resurrect the grant |
| Retention | Prepared; collection rename effect; permission change before rename; purge first delete effect | Original ID survives lost response and default/different-ID attempts; exact object/inode preserved; partial purge has no success receipt and still requires deletion approval |
| Installed CLI | Generated core prepared, batch child prepared, retention prepared; core and batch child step 0 effect; purge delete effect | Reopen installed code outside checkout, discover and inspect without writes, reject conflicting new attempts, explicitly approve recovery; full tree/history assertions |

The model independently reads SQLite and filesystem state. It is deliberately a
bounded deterministic sequence oracle, not a second implementation of all
validators and not an exhaustive formal model checker.

## Final local verification — 2026-09-07

The unified frozen-source run passed **639 tests** in 326.543 seconds, with zero
failures or skips. This adds 57 source tests to the freshly measured baseline.
The three model suites contain 55 methods; subcases and deliberate process exits
are assertions inside those methods, not inflated test counts.

The freshly built wheel passed **23 installed CLI E2E** in 68.177 seconds and
**4 clean-wheel smoke** tests. The three new installed sequences cover shared
content/nested inverses, scoped navigation, and three kinds of generated-ID
recovery. The runner copies only the independent stdlib test oracle outside the
checkout; application imports resolve exclusively from the installed wheel.

Markdown checks pass for **27 files**. Wheel/sdist build, required contents,
dependency check and diff check pass. Distribution comparison requires all public
package files to match the checkout and all non-generated sdist members to match
their source, including these model documents and tests. Protected research,
ignored runtime state and bytecode are excluded from both artifacts. After this
report is finalized, the artifacts are rebuilt and checked again; no product or
test change is hidden behind an earlier wheel result.

### Statement and branch coverage

Local measurements use Python 3.12.11 on macOS and coverage 7.16. The source
coverage configuration is unchanged: branch measurement, **90% combined floor**,
and **zero excluded lines**. The table compares this frozen source with the
fresh `c1ea6ff` baseline, not with the much smaller legacy PR15 implementation.

| Scope | Final statements | Statement change | Final branches | Branch change |
| --- | --- | --- | --- | --- |
| Whole project | 7250/7785 (93.13%) | -0.34 pp | 2960/3446 (85.90%) | -0.57 pp |
| integration.py | 584/608 (96.05%) | unchanged | 238/258 (92.25%) | unchanged |
| lifecycle/engine.py | 863/952 (90.65%) | -0.35 pp | 418/496 (84.27%) | -0.28 pp |
| lifecycle/batch.py | 426/485 (87.84%) | +0.25 pp | 223/276 (80.80%) | -0.03 pp |
| lifecycle/snapshots.py | 389/402 (96.77%) | unchanged | 164/172 (95.35%) | unchanged |
| lifecycle/retention.py | 764/855 (89.36%) | +0.12 pp | 386/480 (80.42%) | +0.21 pp |
| lifecycle/status.py | 238/241 (98.76%) | +0.09 pp | 91/94 (96.81%) | +0.07 pp |
| lifecycle/incidents.py | 363/374 (97.06%) | +0.02 pp | 168/180 (93.33%) | unchanged |
| lifecycle/invocation.py | 230/253 (90.91%) | +0.18 pp | 86/108 (79.63%) | unchanged |
| lifecycle/cli.py | 381/407 (93.61%) | -0.22 pp | 152/174 (87.36%) | +0.15 pp |
| lifecycle/repository.py | 221/225 (98.22%) | +0.09 pp | 58/62 (93.55%) | +0.44 pp |
| lifecycle/context.py | 40/43 (93.02%) | new module | 13/14 (92.86%) | new module |
| lifecycle/pending.py | 72/104 (69.23%) | new module | 31/60 (51.67%) | new module |

Combined coverage changes from **91.3196% to 90.9091%**. Covered statements and
branches increase by 214 and 86, but code scope also increases; this is not a
percentage-only improvement claim. Coverage measures the main source-test
process, not every child process: some pending page/parent/navigation branches
are exercised by real CLI subprocess tests and installed E2E without contributing
to these counters. Neither those executions nor green counts are relabeled as
100% branch measurement.

### Architecture and remaining risks

The top-level lifecycle import graph has no startup cycles. The context helper
depends only on common errors, pending discovery uses read-only repository
paging, and runtime dependencies remain standard library. Targeted annotation
review found no TODO/FIXME/HACK or coverage exclusions added to lifecycle code.
Governance warning: this repository has no ADR/pitfall index or Fulmail-specific
gates; the project security contract, L01–L16 and I01–I12 models are the applicable
references. Separate reviewers closed the author-owned implementation and model
findings; a mixed-module run during concurrent editing was discarded, and only
the subsequent unified frozen-source run above is final evidence.

Important residual paths include compound malformed-field and cross-record
variants, secondary journal/read failures in some recovery branches, and narrow
retention inode/FD/hash replacement windows. Not every malformed pending parent
or terminal-proof variant is individually fault-injected. Some path-resolution,
platform lock and snapshot publication errors also remain unexecuted in the
coverage report. These are disclosed residual risks, not known reproduced
unfixed defects; the required shared-state, crash, denial and recovery sequences
have repeatable automated evidence.

The model covers bounded deterministic traces, not every scheduler interleaving,
hardware power loss or an exhaustive formal state space. History proof traversal
is not globally memory/I/O bounded by discovery page size. Individual pointer
replacement is atomic; batches and cross-directory effects are recoverable and
compensatable, **not global ACID**. There is no network coordinator, continuous
enforcement, automatic fallback, authenticated actor guarantee, semantic-safety
proof or privileged-tamper resistance.

### Publication gate and reproduction

The exact committed SHA, remote branch/PR head equality and terminal CI runs are
recorded in the [existing PR20](https://github.com/ERerGB/skills-auditor/pull/20)
after publication. This local report does not substitute old-head CI or prove a
later arbitrary revision. The goal cannot close before that remote check.
PR20 remains stacked on the open PR15; normal human review must reconcile the
dependency before merge. This work does not merge or manually close either.

```bash
python -m coverage run -m unittest discover -s tests -q
python -m coverage report
python -m coverage json -o coverage-metrics.json
python scripts/check_markdown.py
python -m build
python scripts/check_distribution.py dist
python scripts/run_artifact_tests.py smoke dist
python scripts/run_artifact_tests.py e2e dist
python -m pip check
git diff --check
```

All failure injection uses temporary fixtures. Protected user research and real
installed Skills are outside the test and packaging scope. No merge or manual
issue closure is part of this acceptance run.
