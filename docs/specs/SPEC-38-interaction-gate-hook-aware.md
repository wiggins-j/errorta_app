# Spec 38 — the interaction gate must target the ball, not fire blind

**Source:** A verified multi-agent analysis of run 3 (gravity-golf-3, 2026-08-02),
which STALLED on `no_progress` — not because the game was broken, but because the
web:probe's SPEC-30 interaction gate is a **false negative** for a positional
("grab-the-ball-to-aim") control scheme. The delivered game was genuinely playable
(SPEC-37's hook-driven differential proved the mechanic responds), yet the blind
gesture never hit the ball, so `interaction_changed=False` red-flagged every one of
17 probe runs and the run churned to a stall.
**Target version:** v0.1 (engine — `scripts/web-probe.mjs`, `web_probe.py`)
**Relates to:** [SPEC-30](SPEC-30-execution-gate-and-grounded-review.md) (added the
blind press-drag interaction gate this corrects) · [SPEC-37](SPEC-37-behavioral-mechanic-oracle.md)
(the hook whose evidence this gate should defer to) · [SPEC-39](SPEC-39-drop-probe-pause-clause.md)
(the contract-wording cleanup from the same tangle)
**Status:** proposed · **Owner:** wiggins-j

---

## Problem (verified against the run-3 ledger + delivered code)

The SPEC-30 interaction phase drives a **fixed-location** pointer gesture — a
press-drag at canvas fractions ≈`(0.35,0.55)`→`(0.6,0.4)` and a click at `(0.5,0.5)`
(`web-probe.mjs`) — then hashes the canvas before/after; an unchanged hash sets
`interaction_changed=False`, which reds the probe (`passed = … and
interaction_changed is not False`).

For a **positional control scheme** — the ball is grabbed/aimed where it sits — that
gesture lands on empty grass, never on the ball's ~8px target at the tee. No shot
fires, nothing moves, the hash is unchanged, and the probe is red **forever**,
regardless of how good the game is. Run 3's delivered game proved it responds to
input via the SPEC-37 hook (`mechanic_probe.has_hook && ran`, ball moved,
`mechanic_ok=True`) on all 17 runs, yet `probe_interaction_changed=False` on all 17.
Worse, the reason string — `"canvas did not respond to input (inert)"` — **baited the
grounded reviewers** into chasing render/probeMode fixes that could never move the
gate, churning 7+ PRs into a `no_progress` stall. The gate is measuring "did a blind
gesture happen to hit an interactive element", not "does the artifact respond to
input".

## Principle

> Prove input-responsiveness by **exercising the control where the control is**, and
> by **deferring to stronger evidence** when it exists. A hook the SPEC-37 phase has
> already driven to move the ball is stronger proof of responsiveness than a
> location-blind gesture — the gate must not red an artifact that phase proved live.

## What this spec does

**S1 — a hook-aware, targeted gesture (primary).** When `window.__probe` is present,
the interaction phase reads `state().ball` and presses/drags **at the ball's actual
position** (a slingshot pull from the ball), so the gesture exercises the real
positional control. This is a genuine end-to-end pointer test — it keeps SPEC-30's
"renders but ignores input" catch (the empty-gradient "big square") intact — but
aimed where input is accepted.

**S2 — defer to hook-proven responsiveness (fallback).** If the targeted gesture
still shows no change (e.g. the control is not pointer-driven, or the ball can't be
located) BUT the SPEC-37 mechanic phase **ran** (`mechanic_probe.ran is True` — it
drove `shoot()`/`tick()` and observed the ball move), treat interaction as
satisfied: hook-driven movement is strictly stronger evidence than a synthetic
gesture. Fold this in `web_probe.py` (`_verdict_to_result` **and**
`_probe_verdict_fields`, kept identical). No hook + no interaction change still reds
(the SPEC-30 catch is preserved for hook-less inert artifacts).

**S3 — an honest reason.** Never emit `"inert"` when a hook proved the ball moved.
Replace it with a control-aware message (e.g. `"a location-blind gesture caused no
change; the game exposes a probe hook that DID move the ball — treat as responsive"`
or, when no hook, `"canvas did not respond to input"`), so reviewers are not
misdirected into fixing rendering.

## Regression locks

1. A hook-less web artifact that renders but ignores input (the SPEC-30 target)
   still reds — S2 only defers when the mechanic phase actually ran.
2. A game whose targeted gesture genuinely moves the ball passes on S1 alone (no
   dependence on S2).
3. Non-web / non-hook projects are byte-identical to today; `_verdict_to_result` and
   `_probe_verdict_fields` keep the same folded `passed`.
4. The reason string never says "inert" when `mechanic_probe.has_hook && ran`.

## Definition of done

- Re-running run 3's delivered game (a positional grab-to-aim control with a working
  hook) yields `interaction` satisfied — the run is no longer red-flagged as inert
  and can converge — asserted by a live fixture whose control is grab-the-ball.
- A hook-less renders-but-inert artifact still fails the interaction gate.
- No probe reason says "inert" for an artifact whose hook moved the ball.
