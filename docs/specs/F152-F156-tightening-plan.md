# Delivery-gate tightening — implementation plan (F152–F156)

One branch, one PR into `main`. The specs close a family of "the council declared
success without verifying the delivered artifact actually works (or without
terminating truthfully)" gaps, found by auditing the F146 delivery/merge gates
after a live demo shipped a Next.js app that didn't compile.

## The set

| Spec | Gap | Severity | This PR |
|---|---|---|---|
| **F152** | Launch probe never makes an HTTP request → a server that binds but 500s is "clean" | HIGH | **implement** |
| **F153** | Launch classifier: long-running exit-0 during window = "clean" (G1); crash detection is CPython-only (G2) | HIGH | **implement** |
| **F155** | No delivery-review round cap → `--autonomous` livelocks to `budget_exhausted` (G4) | MED (user-hit) | **implement** |
| **F154** | Test-less project merges/completes with zero compile ever run; no default build gate (G3) | HIGH | **spec only** (fast-follow) |
| **F156** | `no_reviewer` short-circuits tests+launch (G7); tester `not_applicable` self-exemption (G5) | MED | **spec only** (fast-follow) |

**Why F154/F156 are spec-only in this PR:** F154's dependency-install ordering is
the one genuinely non-trivial piece (a build that fails only because deps weren't
installed would file *false* findings) and deserves its own careful implementation +
verification pass; F156 is degenerate/adversarial (needs a PM-less team or a lazy
tester model) and is backstopped by F154's deterministic final build. Both ship as
reviewed specs so the gaps are documented and queued, and are implemented in the very
next pass. This keeps the PR to three cohesive, low-risk, high-value changes that
directly fix the demo failures (app didn't serve; app process died; autonomous
livelock).

## Implementation order (this PR)

Implemented bottom-up, each with tests green before the next.

### 1. F153 — launch classifier (`runtime_process.py`)
- Add module helper `_has_crash_signature(tail) -> (bool, str)` with the marker
  tuple; replace `has_traceback` usage.
- Reorder the post-exit classification: `long_running` (any exit code) → crash,
  before the generic `rc == 0` → clean; `rc == 0` clean now applies only to
  non-long-running one-shots. Preserve the intentional "non-zero CLI, no signature →
  clean" branch.
- Tests: extend `tests/coding/test_f146_slice_c_launch.py`.

### 2. F152 — HTTP serve assertion (`runtime_process.py::launch_probe`)
- During the observe loop, for an `http`-health profile, poll `probe_http`-style
  against the live port each tick; classify: any `<500` response → clean (early
  exit); only-ever-`>=500` through a widened window (`_LAUNCH_HTTP_PROBE_SECONDS =
  45`) → crashed (compile/load error), detail enriched via F153's
  `_has_crash_signature`; never-responds + survived → clean (unchanged survival).
- Reuse `_sub_port(health.url, live.port)`. Best-effort/wrapped: our own probe error
  ≠ app crash.
- Tests: HTTP 500-through-window, 200 early-exit, warmup-500-then-200, 4xx-clean,
  never-responds-clean, non-http-unchanged.

Order note: F153 lands first because F152's detail-enrichment consumes
`_has_crash_signature`, and the two edits are in the same classification block —
implemented as one cohesive rewrite, committed together.

### 3. F155 — delivery-review round cap (`autonomy.py`)
- New stop reason `DELIVERY_REVIEW_STALLED`; register in the terminal-reason set.
- `CodingAutonomyPolicy.delivery_review_round_limit: int = 3` (+ to_dict/from_dict,
  clamp ≥1); `LoopCounters.delivery_review_rounds: int = 0` (persist in run state if
  counters rehydrate on resume).
- Count failed-with-findings delivery rounds at the `_apply_outcome` call site (keep
  `_apply_outcome -> bool`); at the cap, return `LoopResult(DELIVERY_REVIEW_STALLED)`.
  Reset on a passing review.
- CLI: `setup --delivery-review-round-limit N` passthrough (registry param +
  run-setup confirm).
- Tests: increment, stop-at-cap, pass-resets, from_dict clamp, resume-persist.

## Shared files / collision map

- `python/errorta_council/coding/runtime_process.py` — F152 + F153 (same function;
  one edit).
- `python/errorta_council/coding/autonomy.py` — F155 only.
- `python/errorta_cli/commands/runctl.py` + `docs/CLI.md` — F155 CLI flag + docs.
- Tests: `tests/coding/test_f146_slice_c_launch.py` (F152/F153), a new
  `tests/coding/test_f155_round_cap.py` (F155).
- No overlap between the runtime_process change and the autonomy change → low
  regression surface; the large existing F146 suite is the guardrail.

## Risk & mitigation

- **False crash from a marker** (F153-G2 / F152 enrichment): markers are
  startup-crash phrases, tail-scoped, and a false crash is *reversible* (re-opens the
  run, next clean head clears it) — bias toward catching real crashes, matching the
  existing probe's stated posture.
- **False launch failure from the HTTP window** (F152): only a *persistent* ≥500
  through 45s fails; a transient warmup 500 that later 200s is clean; never-responds
  stays clean. Early-exit keeps the healthy path fast.
- **Round cap too tight** (F155): default 3, operator-configurable; only counts
  findings-filed rejections.
- **Existing tests**: run the full `tests/coding/` + `tests/cli/` suites after each
  step; every pre-existing Slice-C assertion must stay green (the reorder is
  specifically designed to preserve `test_launch_probe_cli_nonzero_no_traceback_is_clean`).

## Verification

- `pytest tests/coding/ tests/cli/ -q` green.
- `/code-review` (or subagent review) over the branch diff before the PR.
- PR body enumerates all five specs, marks F154/F156 as spec-only fast-follows.

## Docs

- `docs/CLI.md`: launch-probe now asserts serve + treats any web/api/desktop exit as
  a failed launch + language-agnostic crash detection (F152/F153);
  `--delivery-review-round-limit` + `delivery_review_stalled` stop reason (F155);
  pointer to `test-commands set` for a full build gate (F154).
