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
**Status:** proposed · **Owner:** wiggins-j

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

1. **Provision the test runtime the command needs.** Before judging an acceptance
   candidate's registration run, ensure its declared toolchain is available: for a
   Node test, resolve/install the project's `node_modules` (or point at errorta's
   own Playwright the way `web-probe.mjs` does) in the deterministic executor's
   environment, so "failed to launch" reflects a real defect, not a missing dep.
2. **Distinguish "cannot provision" from "test is red."** If the runtime genuinely
   cannot be provisioned (offline, no node), record a legible
   `test_runtime_unavailable` decision and DO NOT mark the project tested — the
   probe still gates rendering, but the completion summary must state that the
   unit acceptance test was not executed. Never silently drop it.
3. **Register a UNIT-scoped command when it runs.** A green (or genuinely-red)
   registration arms `get_unit_test_commands()` → `tester_dispatchable` → the
   TESTER seat closes `capable` (SPEC-26) and the in-loop gate runs the test on
   every gate-relevant merge, feeding its verbatim output to DEV/REVIEWER.
4. **The completion gate honors it.** `definition_of_done` requires the registered
   unit test green (in addition to the probe), OR an explicit, recorded
   `test_runtime_unavailable` acknowledgement — so "done" never overstates "tested".

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

- On a buildless-web run whose team writes a headless acceptance test, the gate
  either **runs it** (arming the TESTER and gating done on it) or records
  `test_runtime_unavailable` with the reason — never silently refuses it.
- A run that reaches `definition_of_done` did so with its unit acceptance test
  green, or with an explicit recorded acknowledgement that it could not be run.
- The SPEC-28 acceptance fixture gains a tier asserting the authored test is
  actually executed, not merely present on master.
