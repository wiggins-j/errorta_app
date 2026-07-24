# Spec 12 — Implementation plan (in-loop acceptance gate)

Spec: [SPEC-12-in-loop-acceptance-gate.md](SPEC-12-in-loop-acceptance-gate.md).

**Owner:** Engineer A · **Branch:** `feat/spec-12-in-loop-gate`
**Base:** `chore/spec-12-18-prep` (merged) · **PR into:** `main`
**Land after** [Spec 13](SPEC-13-plan.md), **before** [Spec 14](SPEC-14-plan.md).

The spec's **Δ review** notes already fold in the grounding pass; the three that
shape this plan's ordering:

- registering a test command **arms the per-PR merge gate**, so the scope rule
  (Phase 1) must land in the same commit as bootstrap or the branch is
  temporarily wedging;
- running the suite **inside** the merge turn serializes the team and cancels
  Spec 13 — the gate is scheduled off the merge turn (Phase 3);
- `delivery_review` (`runner.py:3984-4010`) **already** runs the whole registry
  against the merged tree bound to `head`; Phase 2 factors that out rather than
  writing a second executor, and Item 4 becomes a test.

## Phase 0 — spec + plan (no code)

Branch off the merged prep PR; commit the spec + this plan. Keeps the spec-first
convention (F151/F157/F158).

## Phase 1 — command scope (must precede bootstrap)

The safety property the rest of the spec depends on.

1. `ledger.py:1235-1260` — `set_test_commands` validates an optional per-command
   `scope: "unit" | "acceptance"`; anything else is a `LedgerError`; absent →
   `"unit"`.
2. `runner.py:2577-2601` — `_set_mergeable_if_ready` counts **unit-scoped**
   commands only when deciding whether the tests gate is vacuous.
3. `runner.py:3448` — the tester-spawn condition likewise. (Leave the TESTER
   branch itself alone — see Phase 5.)
4. `runner.py:3984-4010` — `delivery_review` keeps running **all** commands.

**Tests** (`test_f087_10_gate_scope.py`, new): a registry with only
`acceptance`-scoped commands leaves a reviewer-approved PR mergeable and spawns
no tester task — **the regression lock that stops this spec wedging every run**;
a registry with a unit command behaves exactly as today; a scope-less registry
behaves exactly as today; an invalid scope raises; delivery runs both.

## Phase 2 — factor the deterministic executor

`delivery_review`'s test step (`runner.py:3984-4010`) is already the right call
shape. Extract, no behavior change:

```
_run_gate(store, workspace, *, head, task_id, should_cancel) -> TestRunSession | None
```

— `get_test_commands()` → all ids → `run_test_commands(workspace.root(), …,
require_sandbox=store.get_require_sandbox())` → `record_test_run(…, head=head)` →
decision. `delivery_review` calls it with `task_id=_DELIVERY_TASK_ID`.

**Tests.** `test_f146_delivery_review.py` still passes unchanged (the extraction
lock); `_run_gate` returns `None` on an empty registry and a session otherwise.

## Phase 3 — schedule the gate off the merge turn

1. **Mark, don't run.** In the merge success block (`runner.py:3106-3175`), after
   `_sync_grounding` (`:3154`), set `run_state.gate_dirty` + `gate_dirty_head`
   when the pre-merge `changed_paths` (`:3106-3112`) intersect the gate-relevant
   set (any registered command's entrypoint, any test dir, the runtime profile's
   `working_dir`). Guarded; never fails the merge.
2. **Dispatch at quiescence.** A mechanical gate action fired from the loop's
   quiescent point — the same point the detectors run
   (`autonomy.py:1408-1429` / `:1759-1780`) — **outside** the merge critical
   section (`autonomy.py:1619-1637`). Honors `gate_min_merge_interval` (default
   3) and clears the flag whether the run succeeds or raises.

**Tests** (`test_spec12_inloop_gate.py`, new): a gate-relevant merge sets the
flag and **no `run_test_commands` call happens inside the merge turn** (the
throughput lock — assert via a patched executor); the next quiescent iteration
produces a head-bound `test_run`; a docs-only merge sets nothing; three merges
inside the interval coalesce to one run; `should_cancel` mid-suite fails closed;
a raising gate clears the flag and records a decision.

## Phase 4 — bootstrap

New `coding/gate_bootstrap.py` (no `runner` import). Called at run start
(`runner.py:4285-4288`) and after a merge advances master.

1. **Runtime profiles.** If `list_profiles()` is empty, `runtime.detect(...)` and
   `upsert_profile` **every** proposal — `_detect_node` runs before
   `_detect_static` (`runtime.py:1306-1329`), so gravity-golf's jsdom-only
   `package.json` would otherwise hide the correct static profile.
   `runtime_resolve`'s grounded-or-refuse rule (`:507-512`) discards the
   fictional one at use time.
2. **Acceptance command.** Detect a candidate from master (`test/*.test.js` +
   node; `tests/` + pytest; a `package.json` `scripts.test` whose entrypoint
   exists), then **smoke-run it once** via `run_test_commands`. Register
   `scope: "acceptance"` only if it *executed* — a real test failure registers, a
   missing interpreter/module does not (`gate_bootstrap_refused` + reason).
3. Record `gate_bootstrapped` naming the source signal.

**Tests.** Fixture tree → one acceptance command + all runtime proposals; a
candidate that cannot execute is refused with a decision; idempotent on second
call; an operator-configured registry is never overwritten; a tree with no signal
registers nothing. Plus the Phase-1 merge-gate lock re-asserted end to end.

## Phase 5 — runtime arm

In `_run_gate`, when a runnable `managed_local` profile exists, also drive
`run_runtime_test` via `RuntimeProcessManager` — reusing the machinery F146
Slice C builds at `runner.py:1853-1900` — and record it alongside the command
results. **Do not touch the TESTER branch** (`runner.py:3487-3588`): it calls only
`run_test_commands`, and relaxing its spawn condition would fail closed on empty
`command_ids` (`testing.py:208-212`) and file bogus `fix tests:` tasks.

**Tests.** A runtime-only project produces an in-loop gate record from the
runtime probe; the tester-spawn condition and the `not_applicable` path
(`runner.py:3510-3527`) are unchanged (regression lock).

## Phase 6 — gate output into prompts

`gate_state.latest_gate_text` already exists from the prep PR — fill in its
content here if the prep stub was minimal, then emit
`PromptSegment("gate_output", …)`:

- `_dev_prompt_segments` (`runner.py:1561-1620`) after `repo_snapshot`;
- `_review_pr_prompt_segments` (`runner.py:1713-1775`) after `pr_diff`;
- `_test_prompt_segments` (`runner.py:2077-2116`).

**Absent, not empty, when there is no run.** Do **not** touch any
`tool_guidance` segment — Spec 17 (Engineer B) owns those; see the batch plan's
segment-order contract.

**Tests.** `test_prompt_segments_golden.py` — unchanged goldens for a gate-less
project (the byte-identical lock); with a run, verbatim stderr + the head appear
in each of the three prompts.

## Phase 7 — Item 4 (assert the existing completion guarantee)

No new predicate. `delivery_review` (`runner.py:3859-4090`) already runs the
registry bound to `head`, requires `tests_passed` for `passed`, and files
`"fix delivery tests"` (`:4062`).

**Tests.** A project with a bootstrapped acceptance command reaches
`run_test_commands` inside `delivery_review`; a red result blocks `passed` and
files the fix task with verbatim stderr; a green one completes; a project with
nothing to run is unaffected (`_tests_required`, `evidence.py:115-127`).

## Phase 8 — integration + docs

- **The repro**: a gravity-golf-shaped fixture (buildless web, self-sabotaging
  acceptance script) produces a red in-loop gate, carries the verbatim failure
  into the next dev prompt, and either goes green or trips Spec 04's
  `gate_not_improving`. Assert that on `main`'s code the same fixture merges
  everything green with **zero** test runs.
- `docs/CLI.md` — the gate runs during a run; `errorta gate` reflects in-loop
  runs; what `scope` means for merges.
- `docs/coding/PM_REFERENCE.md` — `gate_bootstrap` / `gate_min_merge_interval`,
  the two new decisions, acceptance vs unit scope, and the (pre-existing, now
  documented) rule that `done` requires a green delivery gate.

## Definition of done

Full coding suite + `ruff` green. Both throughput/safety locks asserted
(no suite inside the merge turn; acceptance scope does not block merges). No
`tool_guidance` segment touched.
