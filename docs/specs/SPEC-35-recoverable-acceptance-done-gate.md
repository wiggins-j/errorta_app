# Spec 35 — the recoverable acceptance `done` gate: refuse `done` until the team's own gate is green

**Source:** The deferred half of [SPEC-34](SPEC-34-behavioral-acceptance-run-the-teams-oracle.md).
SPEC-34 made the council invoke the project's *declared* test runner (S1) and report
honest oracle provenance at `done` (S4), but it deliberately did **not** ship a hard
`done`-gate: the first draft blocked on the `acceptance_test_unrun` record, and
adversarial review proved that wedges a run **permanently with no recovery** (the
record is written by a one-shot bootstrap and never cleared). This spec is the
*recoverable* version — one that blocks on a signal that clears itself.
**Target version:** v0.1 (engine — `gate_state.py`, `completion.py`, `runner.py`)
**Relates to:** [SPEC-34](SPEC-34-behavioral-acceptance-run-the-teams-oracle.md) (S1/S4
landed; this is its deferred hard gate) · [SPEC-31](SPEC-31-in-loop-unit-test-execution.md)
· [SPEC-28](SPEC-28-autonomy-acceptance.md) (the inert-mechanic fixture)
· Spec 12 (the in-loop gate that provides the recovery)
**Status:** proposed · **Owner:** wiggins-j

---

## Problem

A PM `done=true` claim is verified only against open backlog work
(`pending_completion_work`, F128). It is **not** verified against the greenness of
the project's own acceptance gate. So a run can reach `definition_of_done` with a
*registered, runnable* acceptance test sitting **red** on master, as long as no task
or PR is open — which is exactly how run 10 shipped an inert mechanic whose authored
test would have failed. The in-loop gate and the delivery review block a red gate
*during* the loop, but neither is the PM's `done` chokepoint, and a red acceptance
result does not, by itself, stop `done`.

SPEC-34 could not close this safely because it reached for the wrong signal. The
lesson from its review is the whole design constraint here:

> A `done`-block is only safe if the thing it blocks on can **clear itself**. Block
> on a never-cleared "couldn't run" record and you get a permanent wedge; block on a
> **registered gate's live result** — which the in-loop gate re-runs every merge —
> and recovery is structural.

## Principle

> `done` is refused while the project's **own acceptance gate** — a *registered,
> proven-runnable* acceptance-scoped command — has not been observed **green at the
> current master head**. Because only registered (runnable) commands gate, and the
> in-loop gate re-runs them on every gate-relevant merge, every refusal lifts itself
> the moment the gate next runs green. A project with no acceptance gate, or one whose
> test cannot be provisioned (so it was never registered), is never blocked.

The difference from SPEC-34's rejected draft, precisely: it blocked on
`acceptance_test_unrun` (a can't-run record, written once, never cleared → no
recovery). This blocks on `latest_acceptance_result` (a live gate result, refreshed
by the in-loop gate every merge → recovery by construction).

## What this spec does

**G1 — Precise acceptance-result lookup (`gate_state.latest_acceptance_result`).**
`latest_gate_run` returns the newest recorded run of ANY kind, but `record_test_run`
is also called for `web:probe` (web_probe.py:334) and PR-scoped unit runs
(runner.py:4847) — so it cannot answer "did the ACCEPTANCE gate pass?". Add
`latest_acceptance_result(store) -> {passed: bool, head: str} | None` that scans
`list_test_runs()` for the newest session whose per-command `results` include a
command_id that is **acceptance-scoped in the current registry**
(`get_test_commands()` → `scope == "acceptance"`), ignoring probe/unit/PR runs.
Returns `None` when no such run exists. READ-ONLY, fully guarded (SPEC-12-prep style).

**G2 — The gate status (`completion.acceptance_gate_status`).** A pure, read-only
classifier `acceptance_gate_status(store, current_head) -> Literal`:
- `no_gate` — no acceptance-scoped command registered → **allow** (nothing to gate;
  SPEC-34 S4 already reports "none authored"). This is the no-wedge floor.
- `green` — `latest_acceptance_result.passed` and its `head == current_head` → **allow**.
- `red` — a result exists at `current_head` and it did not pass → **block (fixable)**.
- `stale` — a result exists but at a different head (or none yet) while a gate IS
  registered → **block (needs a fresh run)**.

**G3 — Block + recover at the PM `done` chokepoints (`runner.py`).** Alongside the
existing `pending_completion_work` refusal, at BOTH done paths (the plan turn and the
last-word turn):
- `red` → record `pm_completion_refused` ("acceptance gate is red at head …; fix the
  test/mechanic") and return `completion_refused`. **Recovery:** the team fixes it,
  the fix merges, `_arm_gate_after_merge` arms the in-loop gate, the next GateRun
  re-runs the acceptance command on the new head → `green` → the next `done` passes.
  No state to clear by hand.
- `stale` → **arm the in-loop gate** (`gate_due=True`, `gate_dirty_head=current_head`)
  and refuse this `done`, so the loop runs the gate and the next `done` attempt sees a
  fresh result at head. Bounded (G4).

**G4 — Bounded, never an infinite arm loop.** The `stale` arm-and-refuse is capped by
a run-state counter (`acceptance_gate_stale_arms`). If, after N arm→run cycles, no
result at the current head materializes (the command started failing to LAUNCH
mid-run — it became unprovisionable), the gate degrades to SPEC-34's non-blocking ack
**and** escalates to a human-routed `completion_blocked` Problem, rather than
arming forever. Reaching a fresh result (green or red) resets the counter.

## Recovery invariant (the property SPEC-34's draft lacked)

> Every block this spec raises is lifted **automatically** by a subsequent green
> acceptance-gate run — no operator action, no manual clearing of run-state. The
> only terminal state is `completion_blocked`, which is an explicit, human-routed
> escalation, not a silent wedge.

This is provable from the mechanism: the block reads a live result that the in-loop
gate refreshes every gate-relevant merge; a red→green fix therefore flips the block
off on the next cycle. The rejected SPEC-34 draft read a write-once record, so no
cycle could ever flip it.

## Regression locks

1. **No acceptance gate → no new behavior.** A project with no acceptance-scoped
   registered command returns `no_gate` and `done` is judged exactly as today.
2. **Isolation.** A `web:probe`, a PR-scoped unit run, or a unit-scoped command can
   neither satisfy nor trip the acceptance gate — only a run of an acceptance-scoped
   command_id at the current head counts (asserted with a mixed `list_test_runs`).
3. **Recovery, proven.** A `red` refusal lifts on the next green in-loop run at the
   fixed head with NO manual state change — asserted end-to-end by a test that drives
   red → fix/merge → green → `done` allowed.
4. **Bounded.** The `stale` arm-and-refuse cannot loop forever: after the capped arm
   cycles with no fresh result it degrades to a non-blocking ack + `completion_blocked`
   escalation (asserted).
5. **No sandbox weakening.** No change to `testing._run_one` network/env invariants;
   this spec only reads results the executor already produces.

## Definition of done

- A run with a **registered, runnable** acceptance gate cannot reach `done` unless
  that gate is observed **green at the current master head**.
- Fixing a red acceptance test and merging **lifts the block automatically** (no
  manual clear), verified by a red→fix→green→done test.
- A project with **no** acceptance gate, or one whose test is **unprovisionable**
  (never registered), is never blocked (no wedge) — verified.
- A `web:probe`/unit/PR run never stands in for the acceptance gate — verified with a
  mixed run history.
- Re-running the SPEC-28 fixture: an inert mechanic whose authored acceptance test
  measures effect and fails → `done` refused until the mechanic is fixed.
