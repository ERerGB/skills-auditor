# Skill Trace / lifecycle compatibility verification

This is acceptance evidence for the [PR #21 compatibility increment](lifecycle-trace-alignment.md)
on Issue #19 / PR #20. The earlier [model verification](lifecycle-model-verification.md)
at `8527a83` is historical, not acceptance of this combined source tree.

## Baseline

The unchanged feature head `8527a83d829037d4a8e56412fef2755a932ec938` and main
`14e6641315c1c6abf1a52208a9ed0048504f57ad` were merged locally as `fa680f1`.
Only two documentation conflicts required resolution; both contracts were kept.
The source suite ran in a frozen archive of that combined baseline, not in a
concurrently edited checkout: **655 tests passed in 327.351 seconds** without
failures or skips. Coverage is **7413/7945 statements (93.30%)** and
**3017/3502 branches (86.15%)**, combined **91.1156%**. The 90% combined floor
and zero excluded lines are unchanged. This is the fresh baseline, not final
acceptance.

## Tests-first evidence

The new CLI tests first reproduced missing preflight in both the early root
dispatch and direct lifecycle entry. The initial four methods produced 23
failure subcases and one missing-command parser error; help and invalid-input
controls already passed. The two new installed E2E methods were separately run
against the frozen baseline wheel outside the checkout: 19 failure subcases
confirmed absent advisories and the missing explicit evidence command.

Consumer tests first failed on the missing additive investigation field and
distribution inventory, then on unsupported capture references. Producer tests
cover the initial missing API and follow-up negative admission: falsey malformed
timestamps, an orphan completion event, serialized size limits and malformed
completion results. All fixtures are temporary; synthetic hook metadata is
explicit test data, not a claim that real host hooks ran.

The FIFO sensor/settings regression separately reproduced a real blocking read:
an ordinary lifecycle command exceeded its three-second subprocess test timeout
for each FIFO. This is an advisory availability defect inherited from the main
baseline, not a Skill authorization defect. The fix must reject non-regular
inputs, bound settings reads and preserve the ordinary command's exit policy.

| Acceptance surface | Automated evidence |
| --- | --- |
| Independent authorization/capture dimensions and all CLI entry points | [CLI source matrix](../tests/test_lifecycle_trace_cli.py) and [installed matrix](../e2e_tests/test_installed_lifecycle_trace.py) |
| Atomic explicit observation, exact retry and changed scope, malformed or interrupted proof | [Capture producer tests](../tests/test_lifecycle_capture.py) |
| Incident attachment/read-only investigation, same owner, bounded pages and retention leaf semantics | [Consumer tests](../tests/test_lifecycle_capture_consumers.py) |
| Historical/additive schema compatibility and distribution inventory | Producer/consumer schema tests and [distribution checker](../scripts/check_distribution.py) |
| Ordinary commands survive unhealthy capture without granting permission | CLI matrix, FIFO regression and [Skill Trace tests](../tests/test_skill_trace.py) |

## Local acceptance

The complete combined implementation passed **710 source tests** on both local
Python 3.12.11 (122.929 seconds) and 3.14.7 (149.884 seconds), without failures,
skips or database ResourceWarnings: 55 more methods than the fresh baseline.
The additions are 22 producer methods, 14 consumer methods, eight lifecycle
CLI methods, eight Skill Trace I/O methods and three fixture-ownership methods.
Parameterized and nested test subcases are not counted as additional methods.
A fresh installed wheel outside the checkout
passed **five smoke tests** and **25 installed CLI E2E tests**, including the two
new installed capture/authorization workflows. The installed test runner copies
only its tests and the independent stdlib model oracle, not the implementation.

The command and persistence matrices cover I13/I14 and S09: orthogonal capture
and authorization states; project A / task B / log ownership; warning-only
stderr and unchanged JSON/exit status; explicit immutable persistence and exact
retries; event/record failure rollback; validated incident references; retention
leaf semantics; historical schema compatibility; and isolated installed usage.

The I/O fix rejects FIFO and other non-regular inputs using the actual opened
descriptor, preserves symlinks to regular files, limits settings to 64 KiB and
sensor reads to 256 KiB, and closes descriptors on success and read failures.
Native SQLite COMMIT denial and actual subprocess death after the event write
and after the record write are automated: reopening shows neither row, and
retrying the same evidence ID succeeds. These are process-failure tests, not
physical power-loss tests.

Independent producer and consumer/CLI reviewers found no remaining actionable
P1/P2 within this bounded increment after fix closure. The producer review
found one defensive proof mismatch under an **injected internal adapter response**:
a valid-looking but wrong returned sequence could report success while the
persisted evidence was unreadable. The regression failed first; a minimal
pre-commit proof readback now rolls back both rows. The native adapter normally
returns the correct sequence; this is not an ordinary production exploit claim.
The separately reproduced FIFO hang was a real advisory availability defect.

### Cross-version test admission

The first pushed implementation `d0d2014` passed its local Python 3.12 suite,
but both remote workflows failed three matrix jobs: Python 3.9/3.10 could not
reset the test's SQLite authorizer with `None`; Python 3.14 exposed unclosed
test-owned database connections whose GC warnings contaminated a strict CLI
stderr assertion. The failed runs are preserved, not described as final passes:
[first PR CI](https://github.com/ERerGB/skills-auditor/actions/runs/34113860127),
[first push CI](https://github.com/ERerGB/skills-auditor/actions/runs/34113857773).

The follow-up changes only tests. The authorizer fault switch is restored in
`finally`, with an added assertion that one actual COMMIT was denied; rollback,
both-row absence and exact retry assertions remain. Shared/reopened Managers and
raw SQLite mutation/schema fixtures explicitly close their owned connections
before deleting temporary directories. SQLite's transaction context is retained
alongside `closing`, so the corruption fixtures still commit their intended
changes. No warning filter, GC scheduling workaround, version skip or stderr
assertion relaxation is used.

The new [ownership regression](../tests/test_lifecycle_fixture_cleanup.py)
holds native handles and verifies they are closed after 13 existing fixture
cases, making failures deterministic across Python GC schedules. All 13 failed
before the fix and passed afterward. An independent reviewer ran the combined
33-method capture/ownership/CLI selection on both Python 3.9 and 3.14 and also
verified connection cleanup when selected fixture bodies deliberately fail.
The two final full local suites above and installed artifact gates were rerun
after this test-only fix; the wheel remains byte-identical.

## Coverage, not just the headline

Both runs use branch coverage with the unchanged **90% combined floor** and
**zero excluded lines**. These counters describe the instrumented source
process; installed E2E and deliberately killed subprocesses are not folded into
the percentages. A passing process-death assertion is independent evidence.
Both final local interpreter runs produced the same statement/branch counters.

| Surface | Statements: baseline → final | Branches: baseline → final |
| --- | --- | --- |
| Whole project | 7413/7945 (93.30%) → 7623/8162 (93.40%) | 3017/3502 (86.15%) → 3115/3608 (86.34%) |
| Lifecycle package | 4173/4535 → 4360/4729 | 1851/2180 → 1934/2272 |
| New capture producer | New → 152/162 (93.83%) | New → 62/72 (86.11%) |
| Lifecycle CLI | 381/407 (93.61%) → 395/419 (94.27%) | 152/174 (87.36%) → 156/178 (87.64%) |
| Incident consumers | 363/374 (97.06%) → 379/390 (97.18%) | 168/180 (93.33%) → 182/194 (93.81%) |
| Retention | 764/855 (89.36%) → 768/859 (89.41%) | 386/480 (80.42%) → 388/482 (80.50%) |
| Skill Trace | 150/150 (100%) → 173/173 (100%) | 49/52 (94.23%) → 64/66 (96.97%) |
| Legacy integration, unchanged | 585/608 (96.22%) | 239/258 (92.64%) |

Whole-project combined coverage is **91.1156% → 91.2319%** (+0.1164 percentage
points); the distinct statement and branch increases are +0.09 and +0.19 points.
No business code, assertions or coverage exclusions were changed to raise it.

## Artifact and publication boundary

Wheel and sdist are built with the normal build frontend; the distribution
checker verifies metadata and inventory. An additional byte comparison checks
all 41 public package Python/schema members in the wheel and all 151
non-generated sdist members against this source tree. Runtime state, bytecode,
Git metadata and `doc/plugin-ecosystem-survey.md` are excluded. Markdown links,
`git diff --check` and dependency consistency are additional local gates.

The final exact remote SHA, artifact hashes and terminal Actions run links are
published on the existing [PR #20](https://github.com/ERerGB/skills-auditor/pull/20)
after push, rather than embedding a self-referential commit hash here. That
receipt must match this implementation and is the remote acceptance authority;
this document alone does not attest a future CI run. The PR remains unmerged
and stacked on PR #15; no Issue is manually closed. Operational evidence and
reviewer reports stay in the adopted project's ignored local ledger/memory.

## Remaining coverage and risk

- Ten producer rejection lines remain unexecuted, including malformed absolute
  paths/session inputs, some preference/health contradictions, future hook
  timestamps, missing paired healthy hooks and one normalized repository-error
  path. Other corruption cases exercise the shared fail-closed reader; this is
  not exhaustive mutation testing of every schema predicate.
- The two remaining Skill Trace branch arcs are loop/control alternatives,
  not uncovered I/O exception handlers. Byte/type bounds prevent the reproduced
  FIFO and over-read failures; they are **not a wall-clock deadline** for slow
  or unresponsive filesystems.
- Existing lifecycle pending, batch, engine and retention defensive paths are
  not all covered. Pending remains 72/104 statements and 31/60 branches;
  retention still has 91 uncovered statements. This increment does not claim
  exhaustive transaction-state or physical-failure coverage for those modules.
- An investigation page is bounded to 50 events × 20 references, with each
  capture body capped at 32 KiB. The maximum canonical capture bodies alone
  can approach 32 MiB; wrappers/pretty printing add overhead. Proof validation
  may still inspect existing history; this is not constant-cost global I/O.
- Hook trust remains an external host decision. Healthy capture does not prove
  semantic Skill use, approval or safety. No real host setting or installed
  Skill was mutated. Point-in-time ownership checks do not stop a hostile
  database owner or every noncooperating filesystem race after the check.
