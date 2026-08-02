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
**Status:** proposed · **Owner:** wiggins-j

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
  in the probe's fail reason) and from the gravity-golf DoD template. The hook stays
  `{state, shoot, tick, reset, setMechanic}` with `tick(n)` deterministic; the game
  is no longer told to pause its render/physics loop.
- Keep the existing determinism guard in `web-probe.mjs` (the OFF-vs-OFF check): it
  already catches a genuinely nondeterministic `tick()`, which is the only thing the
  pause clause was gesturing at.

## Regression locks

1. The SPEC-37 differential still runs deterministically for a game whose `tick(n)`
   is deterministic (verified by the existing live `live`/`inert` fixtures, which no
   longer need to pause any loop).
2. The determinism guard still rejects a nondeterministic `tick()` (the `nondet`
   fixture still BLOCKs).
3. No change to the mechanic verdict, the fold, or the gate wiring — this is
   contract-text only.

## Definition of done

- `_HOOK_CONTRACT` and the DoD template no longer mention pausing the render/physics
  loop; the SPEC-37 live/inert/nondet fixtures still pass/fail as before with their
  render loops running normally.
- A game that renders every frame (never pausing on probe control) is accepted by
  the mechanic phase.
