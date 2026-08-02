# Spec 39 — drop the spurious "don't auto-advance physics in the render loop" clause

**Source:** The run-3 analysis (see [SPEC-38](SPEC-38-interaction-gate-hook-aware.md)).
The SPEC-37 hook contract — and the gravity-golf-3 DoD — carried a clause requiring
the game to pause its own physics stepping while the probe drives the hook. Verified:
that requirement is **unnecessary**, and it **induced** the render/probeMode coupling
the council churned on. This spec removes it.
**Target version:** v0.1 (engine — `web_probe.py` `_HOOK_CONTRACT`; docs — the
gravity-golf DoD template used for demo runs)
**Relates to:** [SPEC-37](SPEC-37-behavioral-mechanic-oracle.md) (authored the clause)
· [SPEC-38](SPEC-38-interaction-gate-hook-aware.md) (the real stall fix; this is the
companion cleanup)
**Status:** landed (merged in PR #80; the pause clause is gone from `_HOOK_CONTRACT` and
the single-synchronous-`page.evaluate` invariant lock is in place) · **Owner:** wiggins-j

---

## Problem

`_HOOK_CONTRACT` (and the DoD) instruct: *"the game must not auto-advance physics
while the probe drives the hook."* The intent was determinism — the probe's `tick(n)`
should be the sole physics driver. But the SPEC-37 mechanic phase runs its entire
on-vs-off differential **inside one synchronous `page.evaluate`**, so
`requestAnimationFrame` cannot interleave with the probe's ticks: the game's own loop
is already frozen for the duration by the JS event loop. The clause buys nothing.

It is not harmless. In run 3 the devs implemented the clause with a `probeMode` flag
that they then coupled to **rendering** (`if (!probeMode && isMoving()) render()`),
producing 7+ churned PRs whose reviewers kept (correctly, per the clause) demanding
render changes — a self-inflicted tangle around a requirement that did not need to
exist. (The actual stall was the SPEC-38 interaction false-negative; but this clause
manufactured the noise that hid it.)

## Principle

> Don't ask the game for guarantees the probe already provides. A synchronous
> `page.evaluate` already serializes the differential; requiring the game to pause
> its loop adds surface, invites bugs, and buys no determinism.

## What this spec does

- **Remove the pause clause** from `_HOOK_CONTRACT` in `web_probe.py` (the string named
  in the probe's fail reason), from the gravity-golf DoD template, AND from
  [SPEC-37](SPEC-37-behavioral-mechanic-oracle.md)'s own prose (§ "the game must yield
  its render-loop stepping once the probe drives the hook") — otherwise the spec
  corpus stays self-contradictory. The hook stays `{state, shoot, tick, reset,
  setMechanic}` with `tick(n)` deterministic; the game is no longer told to pause its
  render/physics loop.
- Keep the existing determinism guard in `web-probe.mjs` (the OFF-vs-OFF check): it
  already catches a genuinely nondeterministic `tick()`, which is the only thing the
  pause clause was gesturing at. *(Verified: the probe never even calls
  `setProbeMode` — the pause path was never exercised in run 3, yet the clause caused
  most of the PR churn.)*
- **Add an invariant lock (code comment + regression note):** the SPEC-37 differential
  MUST remain within one synchronous `page.evaluate` — that is what actually freezes
  the game's loop (a sync evaluate blocks ALL main-thread callbacks: rAF, setTimeout,
  setInterval). If a future change ever makes it `await` mid-sweep (e.g. an async
  SPEC-38 gesture bleeding in), a per-shot determinism guarantee must be restored.

## Regression locks

1. The SPEC-37 differential still runs deterministically for a game whose `tick(n)`
   is deterministic — asserted by an **always-stepping** fixture (see below), not one
   that pauses.
2. The determinism guard still rejects a nondeterministic `tick()` (the `nondet`
   fixture still BLOCKs).
3. No change to the mechanic verdict, the fold, or the gate wiring — this is
   contract-text + one fixture edit only.

## Testability (make the lock non-vacuous)

The current `fixtures/spec37/live` fixture pauses via a `probeControl` flag
(`if(!probeControl) step()`), so it does NOT actually demonstrate "renders every
frame." Drop `probeControl` from it (or add an always-stepping sibling) so a fixture
whose loop keeps running during probe control still passes the mechanic phase —
proving the pause clause is unnecessary. (Confirmed feasible: the differential is
synchronous, so a free-running loop is frozen during the evaluate regardless.)

## Definition of done

- `_HOOK_CONTRACT`, the DoD template, and SPEC-37's prose no longer mention pausing
  the render/physics loop.
- An **always-stepping** fixture (no `probeControl` pause) passes the SPEC-37 mechanic
  phase; the `nondet` fixture still fails.
- The single-synchronous-`page.evaluate` invariant is recorded as a lock so a later
  refactor can't silently reintroduce loop interleaving.
