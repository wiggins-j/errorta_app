# Spec 31 — Arm a runnable unit test in the loop (close known-open #1)

**Source:** Run 10 (`gravity-golf`, 2026-07-31) — the first run to reach
`definition_of_done` with a working artifact. Its acceptance test
(`test/acceptance.test.js`, asserting a non-black canvas and that a straight-line
solver fails) was written and merged, but the gate **refused to register it**:

```
gate_bootstrap_refused: candidate 'acceptance' not registered —
status=failed (exit 1); a command that cannot run would be a red gate forever
```

So the DoD's acceptance test exists on master but the harness **never runs it**.
Rendering is gated by GL01's `web:probe`; the unit/acceptance test is unexecuted
theater. "Tested game" currently rests entirely on the probe.
**Target version:** v0.1 (engine — `gate_bootstrap.py`, `runtime*.py`,
`capabilities.py`)
**Depends on:** [SPEC-30](SPEC-30-execution-gate-and-grounded-review.md) (the
web:probe execution gate this complements) · [SPEC-26](SPEC-26-role-capability-closure.md)
(the TESTER seat this would finally arm)
**Relates to:** known-open #1 (ROADMAP-autonomy.md) — "no engine path registers a
UNIT-scoped test command, so a headless run can never arm its own TESTER"
**Status:** PARTIALLY LANDED — the honesty accounting (§2 below) is implemented;
the runtime provisioning (§1) is deferred pending a design decision (see note).
**Owner:** wiggins-j

> **Scope note (what landed vs deferred).** Two corrections to the original plan,
> learned while implementing:
> * **Acceptance-scope is correct; do NOT register UNIT-scoped / arm the TESTER.**
>   `gate_bootstrap` deliberately registers acceptance scope — a whole-project test
>   fails on a single-module per-PR branch — and seating a TESTER member dispatches
>   into a wall (SPEC-30 S3, `test_tier1_concurrent_fanout_completes`). The value is
>   RUNNING the acceptance test on the integrated tree (in-loop + delivery gates),
>   which already happens once it registers — not arming a per-PR tester.
> * **Runtime provisioning fights the executor's security model.** Test runs get a
>   MINIMAL env by contract (`testing._run_one`: `HOME`/`TMPDIR`/`PATH` only,
>   `assert not explicit_env`). Resolving Node deps (a headless browser, jsdom) for
>   the acceptance test therefore needs a *sanctioned deps-provisioning step*, not
>   arbitrary env injection — a design decision, deferred. **What LANDED is the
>   honesty half (§2):** an un-runnable authored test is no longer silently dropped.

---

## Problem

`gate_bootstrap._detect_acceptance_command` registers a test command only if it
**runs green (or produces a real assertion failure) at registration time** — a
deliberate guard so a command that cannot even launch (missing interpreter/module)
does not become "a red gate forever." Correct in intent, but on a buildless-web
project the acceptance test needs `node` + a headless browser (Playwright) + the
project's own `node_modules`, none of which the registration sandbox provisions.
So the command fails to launch → is refused → the tester never arms
(`tester_dispatchable` reads `get_unit_test_commands()`, which stays empty) → and
the written test is never executed by any turn or gate.

The result is the exact gap known-open #1 names: the one role whose duty is
execution has nothing to run, and the project's own test — the artifact the DoD
explicitly demands — is dead weight. SPEC-30 closed the *practical* execution loop
with the probe (render + interaction), but a project's bespoke assertions
(level-count, non-triviality, physics invariants) are still never checked.

## Principle

> A test the team was asked to write, and the DoD requires, must be **run** — or
> the run must say plainly that it could not, and why. A registry that silently
> refuses a test the project depends on is the same "verification that can never
> fire" failure the whole batch exists to remove.

## What this spec does

**§1 — Provision the test runtime (DEFERRED — needs a design decision).** For a Node
test, resolve the project's `node_modules` / point at errorta's own Playwright (the
`web-probe.mjs` mechanism) so "failed to launch" reflects a real defect, not a
missing dep. **Blocked on:** the executor gives test runs a minimal env by contract
(`HOME`/`TMPDIR`/`PATH`, `assert not explicit_env`), so this needs a sanctioned
deps-provisioning step (a controlled `npm ci` in the sandbox, or a curated
NODE_PATH allowlist), not arbitrary env injection. Left for a follow-up + operator
decision on how the sandbox may fetch/resolve deps.

**§2 — Never silently drop an un-runnable authored test (LANDED).**
`gate_bootstrap` still refuses to register a command that cannot execute (an
unrunnable command is not a gate), but now records it as a distinct
`test_runtime_unavailable` decision AND persists `run_state.acceptance_test_unrun`
(`{command_id, argv, reason}`). When a run reaches `done` with that flag set,
`_ack_unrun_acceptance_test` records a `done_acceptance_test_unrun` decision — so
`done` never silently overstates "tested". Rendering is still gated by the
web:probe; the un-run of the *unit* acceptance test is now legible on the ledger.
Best-effort and non-blocking: it does not re-wedge a run whose test simply cannot
run in this environment.

## Regression locks

1. The registration guard's core invariant holds: a command that cannot run is
   never registered as a green gate (no red-gate-forever wedge).
2. A project with no test authored behaves exactly as today (probe-only).
3. `gate_bootstrap`'s acceptance-scope detection is unchanged; this spec adds the
   runtime-provisioning step and the unit-scope arm, it does not relax the
   ambiguity refusals.
4. Offline / no-node hosts degrade to a recorded `test_runtime_unavailable`, never
   a crash and never a false green.

## Definition of done

**Landed (§2):**
- An authored acceptance test that cannot execute is recorded as
  `test_runtime_unavailable` + persisted to run_state — never silently refused.
- A run that reaches `definition_of_done` with such a test records a
  `done_acceptance_test_unrun` acknowledgement, so `done` never overstates "tested".
- Locked by `test_spec31_test_runtime_unavailable.py` and the updated
  `test_spec12_in_loop_gate` refusal test.

**Deferred (§1 — follow-up):**
- On a buildless-web run whose team writes a headless acceptance test, the gate
  actually **runs it** (registers it, runs it on the integrated tree via the
  existing in-loop + delivery gates) once a sanctioned deps-provisioning step
  exists — turning the `test_runtime_unavailable` acknowledgement into a real green.
- The SPEC-28 acceptance fixture gains a tier asserting the authored test is
  actually executed, not merely present on master.
