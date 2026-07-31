# Spec 34 — "Renders + responds" ≠ "works": run the team's own behavioral oracle, and gate on it

**Source:** A multi-agent analysis of run 10 (`gravity-golf`, 2026-07-31),
adversarially verified. The council shipped a game whose gravity is numerically
**inert** — a straight tee→hole shot deflects **0.005px** (hole radius 15px) and
sinks whether or not the well exists — to `definition_of_done`, as a "working,
tested game". The same model (Claude), in an ad-hoc seat, caught it in minutes by
*running the delivered modules and measuring the deflection*. This spec is the
engine change that would let the council catch its own version of this.
**Target version:** v0.1 (engine — `gate_bootstrap.py`, `completion.py`,
`gate_state.py`; optionally `web_probe.py` / `scripts/web-probe.mjs`)
**Depends on / completes:** [SPEC-31](SPEC-31-in-loop-unit-test-execution.md) (which
NAMES the un-run test but neither runs nor gates on it)
**Relates to:** [SPEC-30](SPEC-30-execution-gate-and-grounded-review.md) (the
render+input probe this shows is insufficient) · [SPEC-32](SPEC-32-reviewers-read-the-tree.md)
(grounded review, shown insufficient here) · failure-report Pathology 1
**Status:** proposed · **Owner:** wiggins-j

---

## The finding (why the council missed it — same model as the finder)

Not intelligence. The **same Opus model** sat in every seat, and one seat even
**wrote the exact test that catches this** (`test/acceptance.test.js` Test 2, a
straight-line solver asserting the shot misses; Test 4 asserting `positionDiff > 10`
with vs without a well). It is an **affordance + DoD gap**:

1. **No seat can execute code in a turn.** `turn_controller._ROLE_TOOLS` grants the
   DEV `code_write` and the REVIEWER/TESTER/PM nothing; every prompt states there is
   no run/exec/shell tool. The DEV wrote correct inverse-square math with a ~1000×
   scale error (`gravity.js`: `F = strength / d²`, no `G`) and could not run it to
   find out.
2. **Reading cannot see a scale error.** The grounded reviewer had `repo_read` and
   spent 4 turns on the gravity PR (`pr-62bc2e4b75ad`), producing **zero findings**:
   the math is *correct*, only the magnitude is wrong. Grounded review (SPEC-32) is
   real but demonstrably insufficient for numeric/behavioral defects.
3. **The one oracle that could catch it never ran.** The authored acceptance test is
   the right oracle, but `gate_bootstrap` smoke-ran it as `node acceptance.test.js`,
   it exited 1, and it was dropped (`gate_bootstrap_refused`, `d-8c5c826f9649`). It
   never executed.
4. **The oracle that *did* fire is blind to this dimension.** `web:probe` checks
   render + responds-to-input (`web_probe.py`: `passed = ok and non_black and
   interaction_changed is not False`). An inert-gravity ball still moves in a
   straight line and the aim UI still redraws, so the canvas hash changes and the
   probe passes. (It is not blind to *all* inertness — earlier in the same run it
   correctly failed a genuinely input-inert head — only to **trajectory / effect
   size**.)
5. **The DoD was satisfiable without measuring the mechanic.** The gravity task
   asked only to "implement `F = strength / distance²`" — no deflection target — so
   "unit-done" = "formula present", which inert code satisfies. `done` was reached
   with a `completion_summary` falsely claiming "a straight-line shot fails on every
   level (verified by acceptance tests)".

**The same-model answer, in one line:** the finder had a verb the council does not —
*execute the artifact and measure the mechanic* — and the council's one out-of-turn
substitute (its own acceptance test) never ran.

## Principle

> An execution gate that proves an artifact **renders and responds to input** has
> not proven the artifact **works**. Only an oracle that measures the mechanic's
> **effect** does that — and the team usually writes exactly that oracle. The
> engine's job is to *run it* and *gate on it*, not to accept "it renders" as proof.

## What this spec does

**S1 — Invoke the authored test with its real runner (the load-bearing fix).**
`gate_bootstrap._detect_acceptance_command` proposes `["node", <file>]`. For run
10's suite that mis-invokes Playwright (`node acceptance.test.js` throws "Playwright
Test did not expect test.describe() to be called here" and exits 1 — the exit-1 was
NOT a missing dep). Detect and use the project's OWN test entrypoint — `package.json`
`scripts.test`, a `playwright.config.*`, a `pytest`/`vitest`/`jest` config — and
provision the runtime that entrypoint declares (e.g. `npx playwright test` with its
`webServer` + a Chromium binary), via a **sanctioned** deps step, not arbitrary env
injection (respecting `testing._run_one`'s minimal-env contract). This is what SPEC-31
§1 deferred; S1 is it, corrected: the blocker was invocation, not jsdom.

**S2 — Only register a gate that actually asserted something (anti-gaming).**
`_smoke_ran_cleanly` registers a command that merely "ran cleanly" — it never checks
that *assertions executed*. A suite that exits 0 without running any assertion (an
empty/mis-invoked suite) would register a GREEN acceptance gate that verifies
nothing — the same "prove-it-works-by-doing-nothing" hole the black-canvas oracle
had. Require evidence of ≥1 executed assertion / test case (parse the runner's
"N passed" / TAP / JUnit count) before registering green.

**S3 — Gate `done` on a required-but-unrun acceptance test (completes SPEC-31 §2).**
SPEC-31 §2 records `acceptance_test_unrun` as a non-blocking advisory. Once S1 makes
the runner actually work, upgrade it: `completion.py` refuses
`stop_reason=definition_of_done` while a DoD-required acceptance test is unrun.
Ordering is load-bearing — gating on unrun **before** S1 re-creates the exact "a
command that can't run is a red gate forever" wedge `gate_bootstrap` was built to
avoid, so S3 ships only with S1.

**S4 — Stop calling the render/launch gate the "acceptance gate" (naming).** The
in-loop `web:probe` + `runtime:launch` gate is recorded as "ran the acceptance gate"
(`d-77336403f655`), which is what let the PM's `completion_summary` claim "verified
by acceptance tests" while the authored `acceptance.test.js` never ran. Name them
distinctly (liveness gate vs authored acceptance test) so completion claims cannot
conflate them.

**S5 — (optional, deeper) a behavioral probe.** Extend `web:probe` from "renders +
responds" to a scripted-shot behavioral assertion — a straight shot must MISS, or
trajectory curvature must exceed a threshold — so even a project that authors no
test still has an effect-measuring oracle. This directly closes the "inert but
render+input-passing" class the analysis names.

## Why this is distinct from SPEC-30/31/32

- **SPEC-30** gave `web:probe` render + input. That passes an inert mechanic (a
  straight-moving ball + a redrawing aim UI still changes the canvas hash). Blind to
  trajectory.
- **SPEC-31** only *names* the un-run test (`acceptance_test_unrun`); it neither runs
  it (§1 deferred) nor gates on it. S1+S3 here are its completion, corrected.
- **SPEC-32** made review grounded — proven insufficient here: grounded, multi-turn
  reading of correct-looking math produced zero findings.

The missing lever none of them provide: **run the team's own behavioral oracle, prove
it actually asserted, and refuse `done` without it.**

## Regression locks

1. A project with no authored test behaves exactly as today (probe-only), never
   blocked by S3 (nothing is "required-but-unrun").
2. S2's assertion-count check never registers a command that ran but asserted
   nothing — and never refuses one that genuinely asserted and passed.
3. S3 ships only with a working S1; on its own it must not re-create the
   red-gate-forever wedge (a test that *cannot* run in this environment records the
   inability and does not wedge — the SPEC-31 escape-hatch is preserved for the
   genuinely-unprovisionable case).
4. The minimal-env / sandbox contract of `testing._run_one` is not weakened by
   arbitrary env injection; S1's provisioning is a sanctioned, bounded step.

## Definition of done

- A buildless-web run whose team writes a headless acceptance test has that test
  **executed with its real runner** on the integrated tree, and `done` is refused
  until it is green (or its runtime is genuinely unprovisionable, recorded).
- A suite that exits 0 without asserting anything does NOT register a green gate.
- Re-running SPEC-28's fixture: a delivered mechanic that renders + responds but is
  numerically inert (a gravity well that does not bend the path) FAILS the gate —
  asserted by a fixture whose authored test measures effect size, not presence.
- Completion summaries can no longer claim "verified by acceptance tests" when only
  the liveness gate ran.
