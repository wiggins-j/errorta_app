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
`runner.py` probe arms, `completion.py`/`gate_state.py`, `autonomy.py` policy knobs; the
`_HOOK_CONTRACT` string)
**Relates to:** [SPEC-37](SPEC-37-behavioral-mechanic-oracle.md) (the differential this
recalibrates + demotes) · [SPEC-38](SPEC-38-interaction-gate-hook-aware.md) /
[SPEC-39](SPEC-39-drop-probe-pause-clause.md) (the interaction-gate + pause-clause fixes
that preceded this) · [SPEC-34](SPEC-34-behavioral-acceptance-run-the-teams-oracle.md) /
[SPEC-35](SPEC-35-recoverable-acceptance-done-gate.md) (this implements SPEC-34's deferred
"run the team's own browser test" via the trusted-chromium path, recoverably) ·
[SPEC-30](SPEC-30-execution-gate-and-grounded-review.md) (the per-PR vs master arm split
this repairs)
**Status:** proposed — **design approved (rev. 2, 2026-08-06), implementation in progress**
· **Owner:** wiggins-j

> **Scope note.** This document is the verified root-cause account plus the approved
> architecture (five moves, items A–E below). The
> `web_probe`/`web-probe.mjs`/`anchors`/`completion` behavior described under "What this
> spec does" is the *target*, not the current state.
>
> **Revision 2 (2026-08-06) — what changed and why.** Brainstorming the implementation
> closed four of the five open questions by *changing item D's contract*. The original
> draft had the council author a free-form acceptance test (`window.__acceptance()`) that
> the engine ran and then re-ran with the mechanic force-disabled, requiring failure. That
> works, but it has two flaws the replacement does not: a test that simply never exercises
> the mechanic passes BOTH arms, so anti-vacuity degrades to an opaque, non-actionable bit
> — the exact dynamic structural root cause #2 blames for the hydra; and it adds a second
> instrumentation contract on top of `__probe`, growing the very surface this spec exists
> to shrink.
>
> Item D now uses two small **additive fields on the existing hook** —
> `__probe.solution()` and `__probe.won()` — with the ENGINE driving every arm. See item D
> for the full contract. The consequences are large enough to record here:
> * **Anti-vacuity becomes structural, not enforced.** The negative-control arm fires the
>   *same shot* with the mechanic off, so a mechanic-ignoring solution fails it by
>   construction. There is no council-authored executable test that could re-enable
>   anything, which is strictly stronger than a no-op override.
> * **The `power`-unit migration dissolves.** The council supplies power in the game's own
>   units and the probe discovers the usable range empirically (item A), so `power` is
>   never pinned to `[0,1]` and NO fixture, `_HOOK_CONTRACT`, or DoD-template migration is
>   required. Open question 3 is closed as "not needed" rather than "sequenced carefully".
> * **The trade accepted:** the contract is shot-genre-specific. A declared mechanic that
>   is not expressible as "fire a shot, did you win" needs a new verb. Judged worth it —
>   every fixture and every live run to date is shot-shaped, and YAGNI applies.

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

## What this spec does (five moves, items A–E)

**A — Calibrate the differential (fixes the golf-4 false negative).** In
`scripts/web-probe.mjs`, replace the geometry-anchored power sweep `[0.8,1.3,2.0]×D` with
an **adaptive sweep in the game's own usable power range**.

*The calibration.* Bisect for `P_sink`: the minimum power at which a mechanic-**OFF**
straight shot first reaches the hole. This is sound precisely because the OFF arm is
**monotonic in power** — with the mechanic disabled the ball travels a straight line at
the hole and its reach increases with launch speed, so "does it sink" flips exactly once.
(The ON arm is NOT monotonic — that is the whole point of the mechanic — which is why the
bisect must run on the OFF arm.) Bracket by doubling from `p = 1` until OFF sinks, capped
at 14 probes; then bisect to within 1%. Sweep 7 log-spaced powers across
`[0.15·P_sink, 1.5·P_sink]`.

*Why that band.* The interesting regime is at and below `P_sink`, where the ball is slow
enough that the mechanic has integration time to bend it. On golf-4's numbers (game max
15, gravity demonstrably matters at 3–8) `P_sink ≈ 10`, so the sweep samples
≈ 1.5, 2.2, 3.2, 4.6, 6.7, 9.7, 14 — squarely across the band the old sweep never reached.

*The `power` unit is NOT migrated.* `_HOOK_CONTRACT` keeps `power` as "launch speed in the
game's own units". The hidden `×POWER_SCALE` coupling stops mattering because the probe no
longer *assumes* a range — it measures one. This is why revision 2 closes open question 3
without a corpus migration.

*Budget and failure mode.* A global tick budget (400,000 ticks across the whole phase)
and the existing 20s timebox bound the cost. Exhausting either aborts the phase to
**uncertain** — advisory, never red. A bisect that never brackets (no power sinks, or
every power sinks) is likewise uncertain, never red.

*Validate: golf-4 flips to `mechanic_matters:true`; golf-2 / the `inert` fixture stays
`false`.*

**B — Demote the differential to advisory + make the detectors sound.** The mechanic
differential no longer contributes to the **anchored** `web:probe` pass/fail — the anchor
tracks only the hard, real signals (renders non-black, no console errors, responds to
input, and item D's white-box result). So a marginal differential flip can no longer drive
`anchor_regressed` / `revise_livelock`. A CONFIDENT, calibrated inert verdict with no
white-box override still blocks `done` (preserving the golf-2 protection), but via a
distinct, **recoverable** path (SPEC-35 `stale`/fixable), and a marginal/uncertain verdict
is advisory only.

*Mechanism.* `anchors.reconcile` keys anchors on `command_id` and reads `r["passed"]`
(`anchors.py:119-121`). So the separation needs no anchor-key surgery: it is sufficient
that `web_probe._verdict_to_result` stop folding `mechanic_ok` into `passed`. The mechanic
verdict continues to travel on the recorded run's `stderr_preview` (so
`gate_state.latest_gate_text` still shows the reviewer the real line) and on the
`probe_mechanic_*` PR-record fields, and item E's done-gate reads it from there. One
consequence to hold onto: **the recorded `web:probe` run's `passed` and the delivery
verdict are no longer the same boolean** — the anchor tracks liveness, the done-gate
composes liveness with the mechanic evidence.

*Confident vs. marginal (open question 4, resolved).* A differential verdict is
**CONFIDENT inert** only when ALL of the following hold:
   1. the bisect converged (a real `P_sink` bracket was found);
   2. at least 5 swept powers ran to completion inside the tick budget;
   3. at every swept power, ON and OFF agreed on the sink outcome **and** their endpoint
      gap was `≤ holeR/2`;
   4. the determinism guard (`off` vs `off2`) agreed at every swept power.
Anything else — a failed bracket, an exhausted budget, observed nondeterminism, or any
power whose gap lands in the `(holeR/2, holeR]` grey band — is **UNCERTAIN → advisory**.

The `holeR/2` margin is what keeps path 3 from re-opening the golf-4 false red: golf-4's
real endpoint gaps were 184–686px against a hole radius of ~20, so the calibrated sweep
reports `matters:true` outright; and any verdict that lands *near* the threshold is routed
to advisory rather than red by construction.

**C — Feedback locality: run the gating verdict on the per-PR arm.** Whatever hard-gates
delivery (item D's white-box result; the calibrated advisory differential as evidence)
must be computed and stamped on the **per-PR** arm too, so the reviewer reviews against the
real bar instead of a weaker per-PR verdict. Keep the SPEC-37 guard against hard-redding
every in-progress partial-module PR: the hard component bites only when the whole-game
hook / authored test is present on the PR head.

**D — White-box council-authored acceptance, in the game's own units (the primary fix).**
Give the council's *own* acceptance claim a runnable browser, on the trusted-chromium path
the probe already owns (SPEC-34's deferred "fix D", done recoverably). It runs **in the
served game's own page context** (browser-sandboxed — it gains no privilege the council's
game does not already have; it is NOT run in errorta's node process, does NOT touch the
network-off unit executor, and crosses no new security boundary).

*The contract — two additive fields on the existing `window.__probe` hook:*

```js
window.__probe = {
  ...,                       // state/shoot/tick/reset/setMechanic, unchanged
  won:      () => boolean,   // the game's OWN win predicate for the current level
  solution: () => ({dx, dy, power}),  // a shot that clears this level, in the
                                      // game's own units — the same units shoot() takes
}
```

Both are things the game already knows. `won()` is the predicate it computes to draw its
own "Hole in one!"; `solution()` is a shot the council necessarily knows works, because it
designed the level. Neither is a test framework, neither is executable council code the
engine evaluates, and neither has numeric semantics the council must guess — `solution()`
speaks the same units as the `shoot()` it already implements.

*The three arms, driven by the ENGINE inside ONE synchronous `page.evaluate` (the SPEC-39
invariant):*

| # | Arm | Requirement | Reason emitted on failure |
|---|---|---|---|
| 1 | `solution()` with the mechanic **ON** | must `won()` | "your own `solution()` does not win with the mechanic on" |
| 2 | **the same shot** with the mechanic **OFF** | must **not** `won()` | "vacuous — the solution wins with the mechanic disabled; either the level is solvable without it, or `setMechanic(false)` does not disable it" |
| 3 | straight-at-hole across the item-A sweep, **ON** | must never `won()` | "a straight shot sinks — the DoD forbids straight-line solutions" |

`solution()` is invoked **exactly once, before either arm**, and its return is snapshotted
as primitive numbers. So it cannot observe which arm is running, cannot return a different
shot per arm, and cannot alias live game state. Arm 3 reuses item A's calibrated sweep and
anchors its scale to `solution().power` when available — a scale guaranteed meaningful,
because it is a power the game itself considers a real shot.

*Anti-vacuity is structural, not enforced.* Arm 2 fires the *identical* shot with the
mechanic off. A solution that does not depend on the mechanic therefore fails arm 2 by
construction — there is no way to author around it, and no council-authored code runs that
could re-enable the mechanic. A game whose `step()` re-enables the mechanic internally
also lands on arm 2's failure, with exactly the right actionable message. This is the
defect golf-4 actually had, reported in words a dev can act on instead of the opaque "the
mechanic has NO effect".

*Fold:*
   - A **green** white-box result (arm 1 passes, arm 2 fails-as-required, arm 3 never wins)
     is the primary delivery verdict for the declared claim and **overrides** a red
     advisory differential — a white-box, council-authored, game-native assertion is
     strictly stronger evidence than a black-box heuristic.
   - A **red** white-box result is a recoverable red, carrying the specific arm's reason
     (SPEC-35 `stale`-not-`red` for a launch/provision failure).
   - A phase that could not run at all (hook absent, threw, timed out) is **not** a red —
     it falls through to item E path 3/4.

**E — Gate hierarchy + completion wiring.** Compose A–D in `delivery_review` / completion
for a declared-mechanic project's delivery, preserving the golf-2 protection:
   1. `solution()`/`won()` contract present, phase ran, all three arms satisfied → GREEN
      (overrides the advisory differential);
   2. contract present but an arm failed → RED, recoverable, with that arm's specific
      reason;
   3. no contract (or the phase could not run) + a CONFIDENT calibrated-inert differential
      → RED, recoverable, with the actionable steer *"expose `__probe.solution()` and
      `__probe.won()` proving the mechanic, or fix the mechanic"*;
   4. no contract + a marginal/uncertain differential → advisory only (SPEC-35 `stale`),
      never a hard block, never an anchor regression.

The new verbs are **never mandatory**: a council that builds a game whose mechanic
demonstrably works clears path 3 on the differential alone. The contract is the *fast* path
and the *unambiguous* path, not a toll gate — which is the concrete answer to structural
root cause #2.

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
5. **No security-boundary change.** The white-box arms run as **page-context evaluation
   against the served game** (browser sandbox), not in errorta's node process and not in
   the network-off unit executor; `testing._run_one` invariants and the "trusted engine
   tool vs. sandboxed server" split are untouched. Under revision 2 this is stronger than
   it was: the engine evaluates no council-authored code at all — it calls two council
   functions and reads a boolean and three numbers.
6. **Non-web / non-force projects** are unaffected (the probe already no-ops off web; the
   declared-mechanic signal still gates on the north-star/DoD word/phrase match).
7. **The reviewed signal equals the gating signal** (item C): the per-PR arm surfaces the
   same verdict components that gate delivery.

## Escape hatches

Four knobs on `CodingAutonomyPolicy` (`autonomy.py`), following the batch convention that
**each disable-value reproduces today's trace exactly**:

| Knob | Default | Disable value restores |
|---|---|---|
| `probe_adaptive_sweep` | `True` | the `[0.8,1.3,2.0]×D` geometry-anchored sweep |
| `probe_mechanic_advisory` | `True` | folding `mechanic_ok` into `web:probe` `passed` |
| `probe_whitebox` | `True` | no white-box phase at all |
| `probe_pr_gating` | `True` | today's weaker per-PR verdict (item C off) |

## Definition of done

- The five moves land behind the switches above, each disable-value reproducing today's
  trace exactly.
- Validation trio holds: golf-2 blocks, golf-4 unblocks, golf-3 stays green — re-probed
  against the three delivered trees.
- A `solution()`/`won()` contract whose three arms are satisfied greenlights delivery and
  overrides a red advisory differential; a solution that wins with the mechanic disabled is
  red with the vacuity reason naming `setMechanic`.
- Marginal differential oscillation no longer produces `anchor_regressed` / `revise_livelock`.
- The per-PR probe stamps the same verdict components that gate delivery.
- Fixtures exist for each regression lock (see below) and run in the coding suite through
  the injectable `node_runner` seam.

## Test fixtures

New, under `python/tests/coding/fixtures/spec40/`:

| Fixture | Asserts |
|---|---|
| `whitebox-green` | all three arms satisfied → GREEN (lock 2, item E path 1) |
| `whitebox-vacuous` | `solution()` wins with the mechanic off → RED naming `setMechanic` |
| `whitebox-red` | `solution()` does not win with the mechanic on → RED, arm 1's reason |
| `golf4` | replicates golf-4's scale (`POWER_SCALE=60`, human cap 15, **unclamped** `shoot`) → the adaptive sweep must report `mechanic_matters:true` (lock 2) |

Reused: `spec37/inert` is the golf-2 negative control (lock 1); `spec38/grabaim` locks the
interaction fix (lock 3); `spec37/nondet` locks the determinism guard's routing to
*uncertain* rather than red (lock 4).

## Open questions

Four of the five original open questions were closed by revision 2 (see the scope note):
the **white-box contract** is `solution()`/`won()`; the **`power` unit migration** is not
needed; the **confident-vs-marginal boundary** is the four-part rule in item B. Remaining:

- **Adaptive sweep robustness.** Validate the bisect on on-axis wells (zero deflection by
  symmetry), multi-well S-curves, and wall-bounce levels, so it neither false-reds a live
  game nor false-greens an inert one. Note this is now much less load-bearing than in
  revision 1 — the differential is advisory, and a game exposing the contract bypasses it
  entirely — but path 3 still rests on it for contract-less projects. Bounded by
  construction: every uncertainty routes to advisory, so the failure mode is a missed
  block, not a false red.

  **Weak-but-real gravity: validated during implementation, and it moved a fixture.**
  `fixtures/spec37/inert` turns out to be misleadingly named — its `GSCALE=8` is weak but
  REAL gravity, measurably deflecting the ball ~14px against a 20px hole radius. SPEC-37's
  `gap > holeR` rule reads that as a flat "no effect": a false red of exactly golf-4's
  kind, only smaller, and it had been sitting in the fixture corpus as the *control*.
  Under SPEC-40 it classifies as UNCERTAIN (14px is inside the `(holeR/2, holeR]` grey
  band) and is therefore advisory — it can neither hard-block nor drive an anchor
  regression. Loosening the band to call it "confidently inert" would re-open golf-4, so
  the band stands and the honest verdict is uncertainty. The golf-2 negative control moved
  to a new `fixtures/spec40/inert-true`, whose `step()` never reads `mechanicOn` at all —
  the defect golf-2 actually shipped — measuring `max_gap <= 1` and `confident: true`.
- **Node-executed browser tests (flavor b).** Deferred, and revision 2 weakens the case for
  it further: with no council-authored executable test there is nothing that *wants* a node
  process. Revisit only if a declared mechanic appears that the shot-shaped verbs cannot
  express.
