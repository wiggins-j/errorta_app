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

> Prove input-responsiveness by **exercising the pointer control where the control
> is** — the real human path, end to end. Do NOT substitute the SPEC-37 hook's
> programmatic evidence for it: the hook proves the *mechanic* responds to *code*, and
> says nothing about whether the *mouse* is wired. A game with a perfect hook and a
> dead mouse must still fail.

*(Design note: an earlier draft added an S2 that deferred the gate to "the mechanic
phase ran." Two independent adversarial reviews found this **unsound** — it disables
the pipeline's only end-to-end pointer check and would ship a game with a working
hook but a broken/absent mouse handler (the SPEC-30 "renders but ignores input" hole,
reopened for exactly the declared-mechanic class). It is also unnecessary: S1 alone
flips run-3 green. S2 is dropped.)*

## What this spec does

**S1 — a hook-aware, targeted gesture.** When `window.__probe` is present, the
interaction phase reads `state().ball` and drives the **trusted** pointer gesture
(press → drag → release, a slingshot pull) **at the ball's actual position**, then
verifies a real response — a `state()` delta (ball moved) and/or a canvas-hash
change. This exercises the real positional ("grab-the-ball") control end to end.
When no hook is present, the existing SPEC-30 blind gesture is unchanged (its
"renders but ignores input" catch stays intact for hook-less artifacts).

*Coordinate transform (load-bearing):* `state().ball` is in **canvas intrinsic-pixel**
space; `page.mouse` consumes viewport CSS pixels. The probe MUST map
`pageX = rect.left + ball.x * (rect.width / canvas.width)` (and the y analog), so a
CSS-scaled / hi-dpi canvas (`canvas.width=1600`, `style width:800px`) is still hit —
a naive `rect.left + ball.x` only works by coincidence when there is no CSS scaling.
Clamp the mapped point ≥4px inside the rect (preserve the edge-gesture guard). Add a
coordinate-space clause to `_HOOK_CONTRACT`: `state().ball`/`hole` are canvas
intrinsic-pixel coordinates.

**S2 — an honest reason.** Never emit `"inert"` when a hook is present. Replace it
with a control-aware message: with a hook, `"the ball-targeted pointer gesture caused
no response — the mouse control path (mousedown/aim/shoot) appears unwired; the probe
hook works but a human cannot play"`; with no hook, the existing `"canvas did not
respond to input"`. This is what keeps reviewers from being misdirected into fixing
rendering (run-3: a dev blocked with `missing_context`, unable to tell from "inert"
what input the probe sent).

## Regression locks

1. A hook-less web artifact that renders but ignores input (the SPEC-30 target)
   still reds — S1's no-hook path is the unchanged blind gesture.
2. **A hook-PRESENT artifact whose pointer path is dead** (working `__probe`,
   `mechanic_ok=True`, but no/broken mouse handler) still **reds** — this is the hole
   the dropped S2 would have opened; a fixture locks it.
3. A grab-to-aim game whose ball-targeted gesture genuinely responds passes the
   interaction gate — asserted against a fixture the *old blind* gesture fails.
4. Non-web / non-hook projects are byte-identical to today; `_verdict_to_result` and
   `_probe_verdict_fields` stay identical to each other.
5. The reason never says "inert" when `mechanic_probe.has_hook`.

## Testability (there is no interaction-phase test today)

`grep` shows no existing test drives the SPEC-30 interaction phase; SPEC-38 must
create the first one, on the `_serve`/`_probe` scaffold in
`test_spec37_behavioral_oracle.py`. Two new `@pytest.mark.live` fixtures:
- **grab-to-aim** — `mousedown` arms a shot only if it lands within the ball radius
  (unlike the current `live` fixture, which shoots on any click): the *blind* gesture
  fails (baseline red), the *targeted* gesture passes on S1 alone.
- **mouse-dead** — the `live` fixture minus its `addEventListener` lines: renders,
  hook works (`mechanic_ok=True`), but the targeted gesture gets no response → must
  stay **RED** (locks regression #2). Plus a unit test feeding a synthetic verdict
  through `_verdict_to_result` to lock the fold.

## Scope

The interaction gate asserts a **pointer** control path. A legitimately
keyboard-only game has no pointer handler and would red under S1 — an accepted trade
for the mouse-aim golf domain (the DoD requires "Mouse aim + power"); non-pointer
controls are explicitly out of scope rather than silently waved through.

## Definition of done

- Re-running run 3's delivered game (a positional grab-to-aim control with a working
  hook) yields `interaction` satisfied on S1 — the run is no longer red-flagged as
  inert and can converge — asserted by the grab-to-aim fixture.
- A hook-less renders-but-inert artifact still fails the interaction gate.
- A hook-present but mouse-dead artifact still fails the interaction gate.
- No probe reason says "inert" for an artifact that exposes a hook.
