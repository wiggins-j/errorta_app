# Spec 34 — "Renders + responds" ≠ "works": run the team's own behavioral oracle, and gate on it

**Source:** A multi-agent analysis of run 10 (`gravity-golf`, 2026-07-31),
adversarially verified. The council shipped a game whose gravity is numerically
**inert** — a straight tee→hole shot deflects **0.005px** (hole radius 15px) and
sinks whether or not the well exists — to `definition_of_done`, as a "working,
tested game". The same model (Claude), in an ad-hoc seat, caught it in minutes by
*running the delivered modules and measuring the deflection*. This spec is the
engine change that would let the council catch its own version of this.
**Target version:** v0.1 (engine — `gate_bootstrap.py`, `runner.py`; a follow-on will
touch `completion.py` / `gate_state.py` for the recoverable hard gate, and
`web_probe.py` / `scripts/web-probe.mjs` for S5)
**Relates to / advances:** [SPEC-31](SPEC-31-in-loop-unit-test-execution.md) (which
NAMES the un-run test but never invokes the declared runner)
**Relates to:** [SPEC-30](SPEC-30-execution-gate-and-grounded-review.md) (the
render+input probe this shows is insufficient) · [SPEC-32](SPEC-32-reviewers-read-the-tree.md)
(grounded review, shown insufficient here) · failure-report Pathology 1
**Status:** LANDED (verified 2026-08-06 against the code, not the commit log)
**Landed evidence:** _detect_acceptance_command gate_bootstrap.py:162; _ran_zero_tests :119 wired :416; _record_completion_oracles runner.py:3488 on BOTH done paths (:6401, :6531)
**Tests:** tests/coding/test_spec34_behavioral_acceptance.py (all four items)

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

**S2 — Refuse a green gate only on positive zero-test evidence (anti-gaming, no
false wedge).** `_bootstrap_acceptance_command` registered a command that merely
"ran cleanly", so a suite that exits 0 having run nothing would register a GREEN
gate that verifies nothing — the "prove-it-works-by-doing-nothing" hole. S2 refuses
registration when the runner's own output *positively reports zero tests* (`0
passed` / `no tests ran` / `collected 0 items` / `--passWithNoTests`). It does **not**
refuse a green run whose count is merely unreadable — a silently-passing `node
assert` script or a head-truncated pytest summary — because that ran and passed;
refusing it would de-register (and, if it also armed a `done`-block, wedge) a working
project. A real non-zero failure still registers (a valid red gate). *(Adversarial
review corrected the first draft here: an "≥1 assertion or refuse" rule wedged
silently-passing tests and false-refused truncated output.)*

**S3 — Keep the unrun record a NON-BLOCKING honest ack; defer the hard gate.**
SPEC-31 records `acceptance_test_unrun` as a non-blocking advisory, and it stays that
way. The first draft of this spec made it *block* `done`; adversarial review found
that unsafe: `acceptance_test_unrun` is written by a one-shot bootstrap smoke
(`gate_cmd_bootstrap_resolved`) and **never cleared**, so blocking on it wedges a run
**permanently with no recovery** even after the team fixes the test — the exact
"red gate forever" failure regression lock 3 forbids. A *correct* hard gate must
block on a **registered acceptance gate that is red** (which the in-loop gate
re-runs every merge, so recovery is built in) — not on an unrun record — and needs
reliable isolation of the acceptance result among mixed test runs. That is a larger
change; it is deferred as a **follow-on**. In the meantime the in-loop gate and the
delivery review already block a *registered* red acceptance gate during the loop; S4
(below) keeps the `done` summary from overstating what ran.

**S4 — Record oracle provenance at `done` (naming).** The in-loop `web:probe` +
`runtime:launch` gate is recorded as "ran the acceptance gate" (`d-77336403f655`),
which is what let the PM's `completion_summary` claim "verified by acceptance tests"
while the authored `acceptance.test.js` never ran. At `done`, `_record_completion_oracles`
records which oracles actually verified the artifact — the **liveness gate** (renders
+ responds, not effect) vs the **authored acceptance test** — and honestly reports
one of three states: *executed* (an acceptance-scoped gate is registered), *NOT
executed* (an unrun was recorded), or *none authored*. *(Review corrected the first
draft: it inferred "executed" from the mere absence of an unrun record, so a project
that authored no test was labelled "executed" — the very conflation S4 exists to
prevent.)*

**S5 — (optional, deeper) a behavioral probe (follow-on).** Extend `web:probe` from
"renders + responds" to a scripted-shot behavioral assertion — a straight shot must
MISS, or trajectory curvature must exceed a threshold — so even a project that
authors no test still has an effect-measuring oracle.

## Delivered here vs deferred

**Delivered:** S1 (declared-runner detection), S2 (positive-zero-evidence guard),
S4 (oracle provenance), and the SPEC-31 non-blocking ack, hardened so an
unprovisionable `runtime_hint` suite is never registered as a red-forever gate.

**Deferred as follow-on** (each needs infrastructure larger than this change, and
adversarial review showed a naive version is unsafe): the **recoverable hard `done`
gate** (block on a *registered, red* acceptance gate with recovery via the in-loop
re-run — not on the never-cleared unrun record); **network-enabled runtime
provisioning** (S1's install half — the executor is network-off + minimal-env by
asserted design in `testing._run_one`); and **S5**.

## Why this is distinct from SPEC-30/31/32

- **SPEC-30** gave `web:probe` render + input. That passes an inert mechanic (a
  straight-moving ball + a redrawing aim UI still changes the canvas hash). Blind to
  trajectory.
- **SPEC-31** only *names* the un-run test (`acceptance_test_unrun`) and never runs
  it (§1 deferred). S1 here makes the declared runner actually get invoked, and S4
  makes the un-run legible at `done`.
- **SPEC-32** made review grounded — proven insufficient here: grounded, multi-turn
  reading of correct-looking math produced zero findings.

## Regression locks

1. A project with no authored test behaves exactly as today (probe-only); S4 reports
   "none authored" and never claims a test executed.
2. S2 refuses a green registration ONLY on positive zero-test evidence — it never
   refuses a run that genuinely passed (silent `node assert`, head-truncated pytest
   summary), and never registers a run whose output says it executed zero tests.
3. No path wedges `done`: the unrun record is a non-blocking ack, and an
   unprovisionable (`runtime_hint`) suite that cannot run is recorded, never
   registered as a red gate.
4. The minimal-env / sandbox contract of `testing._run_one` is not weakened — no
   network grant or env injection is added; the browser case is recorded
   unavailable, not provisioned.
5. S1's no-`package.json` path is byte-identical to before (`["node", <file>]`), and
   the pytest fallback is unchanged.

## Definition of done

- A buildless-web run whose team declares a test runner (`package.json`
  `scripts.test` / a framework dep / a config) has the **declared runner** proposed
  and smoke-run, instead of a blind `node <file>` that mis-invokes a framework suite.
- A green smoke whose output reports zero executed tests does NOT register a gate; a
  silently-passing test still does.
- `done` records honest oracle provenance and never claims "verified by acceptance
  tests" when only the liveness gate ran or no test was authored.
- No run is wedged by an acceptance test that cannot run or cannot be quantified in
  this environment.
