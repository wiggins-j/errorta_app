# Spec 37 — a behavioral mechanic oracle: prove a declared mechanic has EFFECT

**Source:** The run-11 analysis + a 12-agent workflow whose **stress phase disproved
the naive design**. gravity-golf shipped inert gravity to `definition_of_done` in
BOTH run-10 and run-11: it renders and responds to input (the liveness gate passes),
but no engine oracle ever measures whether the *declared* mechanic actually changes
outcomes. This spec adds that oracle — on the web:probe's already-trusted headless
chromium, so it needs no security-boundary change.
**Target version:** v0.1 (engine — `scripts/web-probe.mjs`, `web_probe.py`; a
north-star signal into `web_probe.py` / `completion.py`)
**Relates to:** [SPEC-34](SPEC-34-behavioral-acceptance-run-the-teams-oracle.md) (this
is its deferred S5, redesigned) · [SPEC-30](SPEC-30-execution-gate-and-grounded-review.md)
(the probe's render+input phase this extends) · [SPEC-35](SPEC-35-recoverable-acceptance-done-gate.md)
(the done-gate a probe verdict can feed) · [SPEC-36](SPEC-36-detect-declared-test-and-honest-provenance.md)
(detection/provenance — necessary but non-blocking)
**Status:** implemented (v1 — the hook-based win-condition oracle; validated live
against control fixtures) · **Owner:** wiggins-j

> **Implementation note (v1, as shipped).** O1–O4 landed as: a behavioral phase in
> `web-probe.mjs` that fires a straight shot at the hole swept across powers via
> `window.__probe` and reports `mechanic_probe` facts; `web_probe.py`'s
> `_declares_load_bearing_mechanic` (a keyword signal requiring BOTH a mechanic term
> and a non-triviality term) gates the fold in `_verdict_to_result` /
> `_probe_verdict_fields`. Enforcement needs **no DoD-injection plumbing**: the probe
> verdict already feeds the existing gate → delivery-review loop (exactly like
> SPEC-30's `interaction_changed`), and the `no_hook` failure names the exact
> `window.__probe` contract in its reason — so the reviewer/dev is told precisely
> what to build. O5's real coverage landed as `@pytest.mark.live` control fixtures
> (`tests/coding/fixtures/spec37/{live,inert,nohook}`) that drive the real
> `web-probe.mjs`: the live game PASSES, the inert game and the run-11-style
> no-hook game BLOCK. A partial/unreadable hook is fail-open (no false red).

---

## Problem

The only behavioral gate that runs in-loop is the web:probe, whose bar is binary
"did the canvas change after a scripted press-drag-release" (move / no-move). A
straight regular-golf shot moves the ball, so an inert-gravity game passes. No
oracle measures trajectory or effect. The team's own behavioral test can catch it
but (a) may not be detected (SPEC-36) and (b) is typically browser-based and cannot
run in the network-off test executor. So the class ships.

## Why the obvious fix is WRONG (stress-proven — do not build this)

The tempting oracle is "script a shot, measure path curvature, fail if curvature <
threshold." **Adversarial simulation on the delivered physics disproved it:**
- The delivered **inert** game at launch power 100 gives curvature **0.189** → *false
  PASS*.
- A **10× stronger (more live)** build at power 500 gives curvature **~0.000** → *false
  FAIL*.
- The metric is **non-monotonic in strength** (it mostly measures launch geometry),
  and the game integrates with **wall-clock `dt`**, so per-frame sampling in headless
  chromium is **non-deterministic**.

An absolute-curvature threshold would both miss real bugs and block good games. It
must not be built.

## Principle

> Prove a declared mechanic *matters* by its effect on the **win condition**, not by
> a raw geometric proxy — and only assert it where the project **declared** the
> mechanic is load-bearing. The measurement must be deterministic and run on the
> trusted-chromium probe path.

## What this spec does

**O1 — a testability contract (the load-bearing part).** A game that declares a
force/physics mechanic must expose a minimal, scriptable **state hook**
(`window.__probe`) giving: the ball / hole / wells positions, a way to **fire a shot**
(direction + power), and a **deterministic fixed-step advance** (so sampling does not
depend on wall-clock `dt`). This becomes a north-star/DoD requirement, checked at
review. **Enforcement is the actual catch for run-11:** a project whose north-star
declares a force mechanic but ships **no hook** is a **MISS** (not fail-open) — because
without the hook the oracle cannot run, and "can't measure the declared mechanic" must
not be a free pass.

**O2 — north-star signal into the probe.** `web_probe.py` (and the completion path)
gain a small, read-only signal: *does the north-star/DoD declare a force mechanic and
non-triviality* ("gravity must matter" / "straight shots blocked" / "levels require
the mechanic")? The behavioral assertion fires ONLY when this is true — so a plain
mini-golf/target/breakout game, or a warm-up level where a straight shot *should*
sink, is never false-failed. (This plumbing does not exist today; it is part of the
spec.)

**O3 — a differential win-condition oracle** in `scripts/web-probe.mjs`, after the
SPEC-30 interaction phase, via the hook and the fixed-step advance:
- Fire a **straight, ball→hole shot swept across powers** (the team's own test uses
  [100..500]); if the north-star declares non-triviality, a straight shot that
  **sinks on any level** = fail. This is the exact, sound signal ("levels are
  non-trivial ⇒ no straight-line solution").
- Reject discontinuities (wall bounce, sink, velocity reversal) before judging, using
  the hook's state, so a bounce can't masquerade as a curve.
- Prefer a **differential** form where feasible (compare with-mechanic vs a
  mechanic-disabled control the hook exposes) so the verdict is about the mechanic's
  effect, not absolute geometry.

**O4 — fold + gate.** `web_probe._verdict_to_result` folds a `mechanic_probe`
verdict into the probe result. A `fail` (or a declared-force MISS from O1) feeds the
existing gate/delivery path; via SPEC-35 an unreliable/absent result is `stale`
(bounded by the completion_refused ladder), never a hard wedge.

**O5 — real coverage.** The `web-probe.mjs` behavioral logic is currently STUBBED in
the pytest suite (the `node_runner` seam injects verdicts). Add real coverage: a
**positive control** (a known-curving build passes) and a **negative control** (the
run-11 inert build fails), so the oracle itself is tested, not just its fold.

## Regression locks

1. **Scoped to declaration.** The behavioral assertion fires ONLY when the north-star
   declares a load-bearing mechanic + non-triviality; a game where a straight shot
   should legitimately sink is never failed.
2. **No false wedge.** An absent/unreliable probe verdict routes through SPEC-35
   `stale` (bounded → human-routed `completion_blocked`), never a permanent block; a
   flaky measurement must be advisory until proven deterministic.
3. **No security-boundary change.** Runs on the web:probe's own chromium
   (`web_probe._default_node_runner`), never the network-off test executor;
   `testing._run_one` invariants untouched.
4. **No absolute-curvature threshold.** The verdict is win-condition/differential, not
   a raw curvature cutoff (the stress-disproven form).
5. **Non-web / non-force projects** are unaffected (the probe already no-ops off web).

## Definition of done

- A declared-force web game whose mechanic is numerically inert (a straight shot sinks
  on a "non-trivial" level) **FAILS** the behavioral probe — validated against the
  run-11 delivered tree as a negative control.
- A genuinely-curving build **passes** (positive control), across level configs and
  the power sweep.
- A declared-force game that ships without the state hook is a **MISS**, not a pass.
- A non-force game, or a level where a straight shot should sink, is never
  false-failed.

## Open questions (resolve before implementation)

- **Hook contract:** exact `window.__probe` shape, and how strictly it's enforced
  (north-star requirement + review check + completion MISS). Getting this wrong makes
  omitting the hook a free pass.
- **North-star signal:** where the "declares a load-bearing mechanic" flag is derived
  and plumbed (web_probe/completion have no north-star access today).
- **Non-mouse launch:** charge-and-fire / keyboard force games fall outside the
  press-drag-release model — require a scriptable launch entrypoint in the contract?
- **Threshold/sweep calibration:** validate on weak-but-real gravity and multi-well
  S-curve levels to avoid false reds/greens.
