# Gravity Golf — product fixes (salvaging the 2026-07-24 run's output)

**Source:** `RUN_ANALYSIS_GRAVITY_GOLF_2026-07-24.md` §6, "Product" section
**Target:** the **generated `gravity-golf` project**, not `errorta_app`
**Status:** proposed

> This is not an engine spec. It is the work list for the artifact the run
> produced, kept here because it is also the **acceptance fixture** for
> [Spec 12](../specs/SPEC-12-in-loop-acceptance-gate.md): every defect below is
> discoverable in seconds by running the artifact, and by essentially nothing
> else. If the harness fixes work, a future run finds these itself.
>
> All four are small. They were found out-of-band — the gate was executed by hand
> and the game loaded in a real browser — so each carries a verified root cause,
> not a hypothesis.

---

## P1 — `test/acceptance.test.js`: stop the harness sabotaging itself

**Symptom.** `node test/acceptance.test.js` fails at initialization:
`getState()` returns `ball: null, level: null, hole: null`. The game is fine; the
harness is broken.

**Root cause.** `createGameEnvironment()` constructs JSDOM with
`resources: 'usable'` against the real `index.html` (which carries the seven
`<script src>` tags) **and then** manually injects the same seven scripts inline
and dispatches a synthetic `DOMContentLoaded`. The injected copies run and
initialize correctly; then the asynchronously-loaded copies of the `<script src>`
tags re-execute every module, and a fresh, never-initialized `window.GravityGolf`
IIFE instance overwrites the initialized one.

**Fix.** Remove `resources: 'usable'` (one line) — or strip the `<script src>`
tags from the HTML before handing it to JSDOM, if the harness is later reworked
to load modules from the document instead of injecting them. Do **one** of the
two; the bug is doing both.

**Verified.** With `resources: 'usable'` removed, initialization passes and the
gate runs to completion. This fix is a prerequisite for everything below —
without it the gate reports nothing about the game.

**Acceptance.** `node test/acceptance.test.js` initializes and runs all 12
levels, exiting non-zero only for real gameplay failures.

## P2 — `src/render.js` / `src/main.js`: the real-browser black screen

**Symptom.** Served over HTTP and loaded in a browser: **zero console errors,
fully initialized game state, and a pure black screen.**

**Root cause.** `Render.init()` sizes the canvas backing store from
`getBoundingClientRect()` at `DOMContentLoaded`. In the embedded browser that
returned 0, so `canvas.width = 0` and every subsequent draw is a no-op. There is
no resize handler and no per-frame size guard, so nothing ever recovers. Calling
`GravityGolf.init()` again by hand fixed it instantly and the game rendered and
played.

**Fix.**
1. Guard init against a zero-size layout: if the measured rect is 0 in either
   dimension, defer sizing to the first `requestAnimationFrame` (and retry) rather
   than committing a 0×0 backing store.
2. Add a `resize` handler that re-sizes the backing store and redraws.
3. Guard `init` on `document.readyState` so a late-loaded script still
   initializes (`DOMContentLoaded` may already have fired).

**Acceptance.** Loading the page in a zero-initial-layout container renders the
course; resizing the window keeps it correct; loading the scripts after
`DOMContentLoaded` still initializes.

> Note: this is the same *class* of failure as the prior run's "green square" —
> an init-order/visibility bug that only execution surfaces. It is the single
> strongest argument for
> [Spec 14](../specs/SPEC-14-grounded-reviewer.md) Item 6 (screenshot evidence
> for visual DoDs): zero console errors + valid state + black canvas is invisible
> to every other signal the pipeline has.

## P3 — `src/levels.js`: levels 3 and 5 are trivial

**Symptom.** After the P1 fix, the gate reports 10/12:

```
lev | par | strokes | status
  3 |   3 |       0 | FAIL   <- straight-line path to hole is clear
  5 |   3 |       0 | FAIL   <- straight-line path to hole is clear
```

The solver completes every level within par+2, so the physics is sound; these two
violate the DoD's non-triviality requirement.

**Fix.** Regenerate levels 3 and 5 so the straight-line path from the ball to the
hole is blocked (an obstacle, a wall, or a gravity well positioned to require at
least one deflection). The gate's own triviality check is the oracle — re-run
until it passes.

**Acceptance.** `12/12` PASS, with strokes > 0 on every level.

## P4 — `src/ui.js`: HUD string bug and letterboxing

**Symptom.** The HUD reads `"Level 1: Level"`. The 800×600 course renders
anchored left inside a 1280×720 canvas, leaving a dead right third.

**Root cause.** The HUD reads a level-name field that does not exist; the level's
actual name is `"First Steps"`. The renderer never centers or scales the course
within the canvas.

**Fix.**
1. Read `level.name`.
2. Letterbox the 800×600 course into the canvas: scale to fit, center, and fill
   the surround.

**Acceptance.** The HUD reads `"Level 1: First Steps"`; the course is centered
with no dead region at any canvas aspect ratio.

---

## Remaining gap after all four (not a fix, a known limit)

Even at 12/12 with a rendering course, the visual bar in the DoD is not met:
texture/lighting depth, motion trail/spin, and a polished HUD are absent — the
result is a prototype, not the premium artifact the DoD describes. That is a
scope judgement for the operator, not a defect with a root cause, and it is
exactly the judgement a reviewer that never saw a pixel could not make
(see [Spec 14](../specs/SPEC-14-grounded-reviewer.md)).

## Use as a harness fixture

The P1 + P2 pair is the ideal regression fixture for
[Spec 12](../specs/SPEC-12-in-loop-acceptance-gate.md):

- P1 is a **red gate the loop must see** — under today's code the project has no
  registered test command, so it merges green with zero test runs;
- P2 is a **defect no gate catches** — it needs the runtime probe or a screenshot,
  which is why Spec 12 Item 1 bootstraps a runtime profile as well as a test
  command;
- P3 exercises Spec 04's `gate_not_improving` scoring — but note what the score
  actually measures: `_gate_fingerprint` (`python/errorta_council/coding/autonomy.py:433-435`)
  counts **registered commands** that exited 0, not levels that passed. With one
  `acceptance` command, 10/12 and 11/12 both exit non-zero → identical
  fingerprint and identical score → counted as churn, and only the final 12/12
  moves the score 0→1. So P3 exercises the 0→1 transition and the
  churn-does-not-reset rule; per-level granularity would need one command per
  level, or a command whose exit code encodes progress.
