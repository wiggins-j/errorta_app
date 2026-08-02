# Spec 40 — white-box acceptance oracle + calibrated, advisory differential

**Source:** The gravity-golf-3 / gravity-golf-4 convergence investigation (2026-08-02).
The behavioral testability contract (SPEC-30 interaction gate + SPEC-37 differential
mechanic oracle) has become the dominant source of council churn, but the root cause is
not "the council can't build instrumentation." It is that the behavioral probe is an
**unsound black-box oracle**: it produces false verdicts in BOTH directions, its
acceptance criteria are hidden and uncalibrated to the game, its one gating signal is
delivered on the wrong review arm, and the autonomy detectors treat it as infallible
ground truth. This spec reworks the oracle so the council can converge.
**Target version:** v0.1 (engine — `scripts/web-probe.mjs`, `web_probe.py`, `anchors.py`,
`runner.py` probe arms, `completion.py`/`gate_state.py`; the `_HOOK_CONTRACT` string)
**Relates to:** [SPEC-37](SPEC-37-behavioral-mechanic-oracle.md) (the differential this
recalibrates + demotes) · [SPEC-38](SPEC-38-interaction-gate-hook-aware.md) /
[SPEC-39](SPEC-39-drop-probe-pause-clause.md) (the interaction-gate + pause-clause fixes
that preceded this) · [SPEC-34](SPEC-34-behavioral-acceptance-run-the-teams-oracle.md) /
[SPEC-35](SPEC-35-recoverable-acceptance-done-gate.md) (this implements SPEC-34's deferred
"run the team's own browser test" via the trusted-chromium path, recoverably) ·
[SPEC-30](SPEC-30-execution-gate-and-grounded-review.md) (the per-PR vs master arm split
this repairs)
**Status:** proposed — **design approved, NOT yet implemented** · **Owner:** wiggins-j

> **Scope note.** This document is the verified root-cause account plus the approved
> architecture (four moves, items A–E below). No engine code has changed yet; the
> `web_probe`/`web-probe.mjs`/`anchors`/`runner` behavior described under "What this spec
> does" is the *target*, not the current state. Implementation is a follow-up, to land on
> a feature branch with the adversarial spec + code reviews the house style requires.

---

## Problem (verified against ledgers, delivered code, and live re-probing)

The council reliably builds a *good game* — it has a tight, observable feedback loop (the
game renders; a human can play it). It cannot converge on the *testability contract*
because the contract's oracle is a black box with a hidden rubric, graded remotely, with
pass/fail as the only feedback. Each recent spec fixed one false verdict and the next run
exposed the next — a hydra, not convergence. Each run below executed on a probe version
that had merged roughly one minute earlier:

| Run | Started (UTC) | Probe era | Stop | Reality |
|---|---|---|---|---|
| gravity-golf-2 | — | pre-SPEC-37 | shipped (`done`) | game genuinely inert → **false PASS** |
| gravity-golf-3 | 01:18:02 | SPEC-37 (01:17) | `no_progress` | game fine, gravity works → **false RED** |
| gravity-golf-4 | 16:46:56 | SPEC-38/39 (16:45) | `revise_livelock` | gravity deflects 125–289px → **false RED** |

**gravity-golf-3 — the interaction gate missed the ball (a pre-SPEC-38 defect).** On all
16 probed PRs `probe_mechanic_has_hook=true`, `probe_mechanic_ok=true`, but
`probe_interaction_changed=false`, reason `"canvas did not respond to input (inert)"`. The
game uses grab-the-ball-to-aim (`onMouseDown` early-returns unless the press lands on the
ball). The pre-SPEC-38 probe drove a **blind fixed-location gesture** at 0.35w/0.55h —
empty grass — so no shot fired and the canvas hash never changed. The `"inert"` string
baited the (correctly grounded) reviewers into chasing a render/`probeMode` phantom; they
churned 9+ PRs relocating `render()`. **Verified:** the *current* probe (post-SPEC-38)
passes golf-3's delivered tree cleanly (`interaction_changed:true, mechanic_matters:true`).
SPEC-38 already fixes this class; it is recorded here only as the first head of the hydra.

**gravity-golf-4 — the differential oracle's power sweep is 32–80× miscalibrated (still
reproduces today).** The interaction gate now passes; the blocker moved to the SPEC-37
differential, which reported `"the mechanic has NO effect — a straight shot behaves
identically with the mechanic on vs off"`. This is a **false red**, isolated by driving the
delivered `window.__probe` directly. The oracle sweeps `power = [0.8, 1.3, 2.0] × D`,
`D = dist(tee,hole) = 600` → fires `power ∈ [480, 780, 1200]`. The game computes
`vx = dir · power · POWER_SCALE(60)` and its `__probe.shoot` does **not clamp** (only the
human drag clamps, to `MAX_POWER = 15`). So the oracle launches at `480 × 60 = 28,800`
px/tick — the ball crosses the whole 600px course in ~one tick, before gravity integrates,
so ON ≡ OFF → "no effect." Reproduced verdict on the final delivered tree:

| power swept | on sinks | off sinks | end-gap | `mechanic_matters` |
|---|---|---|---|---|
| 1200, 480, **15** (game max) | yes | yes | ~0–4px | **false** ← the only band sampled |
| **8, 5, 3** | no | mixed | **184–686px** | **true** ← never sampled |
| 1 | no | no | 6px | false |

The mechanic *demonstrably matters*, but only in the power band 3–8, which the
distance-anchored sweep never samples. **golf-4 was unwinnable by any game improvement:**
`power` is an unspecified unit in `_HOOK_CONTRACT` (the probe means a distance-anchored
launch speed; the game means `velocity/60`), and the sweep's anchor to hole *geometry* has
no relation to the game's *power scale/cap*. This is the exact "depends on the unknown
power cap" fragility the SPEC-37 differential *claimed* to solve; it only relocated the
miscalibration from an absolute threshold into the sweep range.

The livelock then followed mechanically: the differential is marginal, so tiny
well-strength tweaks flipped the master `web:probe` anchor green↔red
(`anchors.reconcile` → `anchor_regressed` "oscillation", decisions #197/#231); a hook
"fix" called `tick(game)` against `physics.js`'s `tick(ball,hole,wells,…)` →
`"wells is not iterable"` crash (pr-d49560cba3b5) which broke a revise lineage →
`revise_livelock` (no merge for 5 iterations with one open broken lineage).
`gate_not_improving` reached 105 because the acceptance gate never rose above 1.

**Feedback-locality bug — why 22 green PRs never shipped.** SPEC-37 folds the mechanic
differential ONLY on the master arm (`web_probe.run_and_record(..., pr_scoped=False)`,
`web_probe.py:438`). The **per-PR probe the reviewer sees is green**; the **master
differential that gates delivery stays red**. The council optimized the visible-but-wrong
signal, merged 22 green PRs, and delivery never cleared. The two arms disagree by design,
and the council was steered by the wrong one.

## Structural root cause

Three compounding defects, none of which is "the council can't build instrumentation":

1. **The oracle infers "is the game good" from black-box behavioral heuristics** — a
   pixel-diff, a blind gesture, a distance-anchored power sweep — each with a hidden
   coupling to an *unspecified game convention* (control scheme, render timing, power
   scaling) and therefore a regime where it is simply wrong. golf-2 found the false-pass
   regime; golf-3 and golf-4 found two different false-red regimes.
2. **The contract changes the deliverable** from "a good game" to "a good game *plus* a
   large instrumentation hook whose exact numeric semantics must match the probe's
   undocumented expectations" — instrumentation invisible to human play, on which the
   council gets zero gameplay feedback, graded by a single non-actionable bit.
3. **The autonomy layer treats a red heuristic as infallible.** A marginal/oscillating
   verdict feeds `anchor_regressed`, `revise_livelock`, and `gate_not_improving`, which
   convert "stuck against a wrong oracle" into a hard stop — and, via the anchor path,
   actively *punish* the tuning that flips a marginal verdict. There is no notion that the
   oracle itself might be the thing that is wrong.

## Principle

> Prefer a **white-box, council-authored assertion run in the game's own units** over
> black-box behavioral inference. Where a black-box heuristic is retained, it must be
> **advisory** — it may inform, never solely gate, and never drive a hard stop when it is
> marginal or uncertain. The signal the council *reviews* must be the signal that
> *gates delivery*.

## What this spec does (four moves, items A–E)

**A — Calibrate the differential (fixes the golf-4 false negative).** In
`scripts/web-probe.mjs`, replace the geometry-anchored power sweep `[0.8,1.3,2.0]×D` with
an **adaptive sweep in the game's own usable power range**: bisect for the minimum power
at which a mechanic-OFF straight shot first reaches the hole, and sweep densely across
`[ε, that power]` (plus a low band), so the "the mechanic changes the outcome" band is
actually sampled. Pin the `power` unit in `_HOOK_CONTRACT`: define `shoot(dx,dy,power)`
where `power ∈ [0,1]` is a fraction of the game's own maximum launch power (or drive
through the game's input abstraction), eliminating the hidden `×POWER_SCALE` coupling.
*Validate: golf-4 flips to `mechanic_matters:true`; golf-2 / the `inert` fixture stays
`false`.*

**B — Demote the differential to advisory + make the detectors sound.** The mechanic
differential no longer contributes to the **anchored** `web:probe` pass/fail — the anchor
tracks only the hard, real signals (renders non-black, no console errors, responds to
input, and item D's white-box result). So a marginal differential flip can no longer drive
`anchor_regressed` / `revise_livelock`. A CONFIDENT, calibrated inert verdict with no
white-box override still blocks `done` (preserving the golf-2 protection), but via a
distinct, **recoverable** path (SPEC-35 `stale`/fixable), and a marginal/uncertain verdict
is advisory only. `anchors.reconcile` must separate the liveness verdict it anchors from
the advisory mechanic metadata.

**C — Feedback locality: run the gating verdict on the per-PR arm.** Whatever hard-gates
delivery (item D's white-box result; the calibrated advisory differential as evidence)
must be computed and stamped on the **per-PR** arm too, so the reviewer reviews against the
real bar instead of a weaker per-PR verdict. Keep the SPEC-37 guard against hard-redding
every in-progress partial-module PR: the hard component bites only when the whole-game
hook / authored test is present on the PR head.

**D — White-box council-authored acceptance runner (the primary fix).** Give the council's
*own* acceptance assertion a runnable browser, on the trusted-chromium path the probe
already owns (SPEC-34's deferred "fix D", done recoverably). The council authors a
deterministic acceptance check that drives the game via `window.__probe`; the engine runs
it **in the served game's own page context** (browser-sandboxed — it gains no privilege the
council's game does not already have; it is NOT run in errorta's node process, does NOT
touch the network-off unit executor, and crosses no new security boundary). Fold:
   - A **green** white-box result is the primary delivery verdict for the declared claim
     and **overrides** a red advisory differential (a white-box, council-authored,
     game-native assertion is strictly stronger evidence than a black-box heuristic).
   - A **red** white-box result is a recoverable red (the council's own assertion; SPEC-35
     `stale`-not-`red` for a launch/provision failure).
   - **Anti-vacuity is engine-enforced, not self-reported:** the engine runs the assertion
     with the mechanic force-disabled (`setMechanic(false)`, then a no-op override so the
     test cannot re-enable it) and requires it to FAIL; it must PASS normally. A test that
     passes with the mechanic off is vacuous or `setMechanic` is broken → red, with that
     exact actionable reason (this surfaces golf-4's real defect instead of the opaque
     "no effect"). This reuses the differential's on/off structure with the council's own
     win-condition in place of the engine's endpoint-distance heuristic.

**E — Gate hierarchy + completion wiring.** Compose A–D in `delivery_review` / completion
for a declared-mechanic project's delivery, preserving the golf-2 protection:
   1. authored white-box test present, runs, PASSES + engine negative-control FAILS → GREEN
      (overrides the advisory differential);
   2. authored white-box test present but RED (or negative control did not fail) → RED,
      recoverable, with the specific reason;
   3. no authored test + confident calibrated-inert differential → RED, recoverable, with
      an actionable "author an acceptance test proving the mechanic, or fix the mechanic"
      steer;
   4. no authored test + marginal/uncertain differential → advisory only (SPEC-35 `stale`),
      never a hard block, never an anchor regression.

## Regression locks

1. **golf-2 stays blocked.** A genuinely inert declared-mechanic game must still fail
   delivery — via item E path 3 (confident calibrated inert) or a red white-box test —
   never ship. Validated against the golf-2 delivered tree as a negative control.
2. **golf-4 becomes winnable.** The calibrated adaptive sweep reports `mechanic_matters`
   for the golf-4 delivered tree; and/or a passing council white-box test greenlights it.
3. **golf-3 stays green.** The SPEC-38 interaction fix is not regressed.
4. **No marginal verdict drives a hard stop.** A mechanic differential that flips on a
   sub-threshold change, or differs at some swept powers but not others, is advisory and
   never sets `anchor_regressed` / feeds `revise_livelock`. A *real* regression (a crash,
   a black canvas, a genuine liveness break) still breaks the lineage.
5. **No security-boundary change.** The white-box test runs as a **page-context script in
   the served game** (browser sandbox), not in errorta's node process and not in the
   network-off unit executor; `testing._run_one` invariants and the "trusted engine tool
   vs. sandboxed server" split are untouched.
6. **Non-web / non-force projects** are unaffected (the probe already no-ops off web; the
   declared-mechanic signal still gates on the north-star/DoD word/phrase match).
7. **The reviewed signal equals the gating signal** (item C): the per-PR arm surfaces the
   same verdict components that gate delivery.

## Definition of done

- The four moves land behind their existing on/off switches, each disable-value reproducing
  today's trace exactly (the batch-escape-hatch convention).
- Validation trio holds: golf-2 blocks, golf-4 unblocks, golf-3 stays green — re-probed
  against the three delivered trees.
- A council white-box acceptance test that PASSES (with the engine negative control
  FAILING) greenlights delivery and overrides a red advisory differential; a vacuous test
  (passes with the mechanic off) is red with that exact reason.
- Marginal differential oscillation no longer produces `anchor_regressed` / `revise_livelock`.
- The per-PR probe stamps the same verdict components that gate delivery.

## Open questions (resolve before / during implementation)

- **White-box test contract.** The exact page-context result convention (a `window.__probe`
  extension? a `window.__acceptance` global the engine reads?) and how strictly the engine
  enforces the negative control without a bespoke per-game protocol. Getting this wrong
  re-creates the very instrumentation burden this spec is trying to retire.
- **Adaptive sweep robustness.** Validate the bisect on weak-but-real gravity, on-axis
  wells (zero deflection by symmetry), multi-well S-curves, and wall-bounce levels, so it
  neither false-reds a live game nor false-greens an inert one.
- **`power` unit migration.** Pinning `power` to `[0,1]` changes the contract every
  existing fixture and the DoD template assume; sequence the fixture + `_HOOK_CONTRACT` +
  DoD-template edits so the corpus is never self-contradictory (the SPEC-39 lesson).
- **Confident-vs-marginal boundary.** The precise rule that classifies a differential
  verdict as "confident inert" (path 3, blocks) vs "uncertain" (path 4, advisory) — it must
  be defensible enough that path 3 never re-opens the golf-4 false-red.
- **Node-executed browser tests (flavor b).** Running the council's test as a *node*
  process resolving Playwright from errorta's `node_modules` (matching how tests like
  golf-2's were actually written) is more format-compatible but runs council code in node
  and needs OS sandboxing — deferred behind the safer page-context flavor above; revisit if
  the page-context contract proves too constraining.
