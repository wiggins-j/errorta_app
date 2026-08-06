# Spec 36 — detect the team's declared test, and never lie about it at `done`

**Source:** A verified multi-agent analysis of run 11 (gravity-golf-2, 2026-08-01) —
the council shipped an inert-gravity game to `definition_of_done` AGAIN, on the engine
WITH SPEC-34+35 merged. The council *did* author a valid straight-line-solver at
`test/acceptance.js`, but the gate never saw it and `done` then reported "none
authored". This spec closes the two small, sound gaps that made the miss silent.
(The behavioral oracle that would actually *block* an inert mechanic is
[SPEC-37](SPEC-37-behavioral-mechanic-oracle.md) — bigger, specced separately.)
**Target version:** v0.1 (engine — `gate_bootstrap.py`, `runner.py`)
**Relates to:** [SPEC-34](SPEC-34-behavioral-acceptance-run-the-teams-oracle.md) (S1
detection / S4 provenance — this fixes both) · [SPEC-35](SPEC-35-recoverable-acceptance-done-gate.md)
(the done-gate that saw `no_gate` because of the detection miss)
**Status:** LANDED (verified 2026-08-06 against the code, not the commit log)
**Landed evidence:** _choose_js_argv gate_bootstrap.py:197 with npm-placeholder skip :239; _tree_has_authored_test runner.py:3460 consumed :3514
**Tests:** tests/coding/test_spec36_detect_and_provenance.py

---

## Problem (verified against the run-11 ledger + delivered tree)

1. **Detection miss.** The council authored `test/acceptance.js` (a real
   straight-line-solver that would have caught the inert levels) and declared it in
   `package.json`: `"scripts": {"test": "node test/acceptance.js"}`. But
   `gate_bootstrap._detect_acceptance_command` only matches files ending `.test.js`
   (or pytest `*.py`). `acceptance.js` matches neither → returns `None` → no
   acceptance command registered → SPEC-35 `acceptance_gate_status` returned
   `no_gate` → `done` allowed. The ledger confirms the negative: the only
   gate-bootstrap decision is the *runtime* one — no acceptance command, no
   `test_runtime_unavailable`, no refusal.
2. **Dishonest provenance.** SPEC-34 S4 (`_record_completion_oracles`) then recorded
   *"authored acceptance test: none authored"* — twice — because it reads the
   registered test-command registry, not the delivered tree. A test file existed on
   master; S4 hid the miss instead of surfacing it.

## Principle

> The engine must (a) detect the test the team actually declared — not only the one
> whose filename matches a hard-coded pattern — and (b) tell the truth at `done`
> about whether that test was registered, run, or merely present-but-unrunnable.

## What this spec does

**B — detect the project's DECLARED test script.** `_detect_acceptance_command` adds
a step (after the `*.test.js` branch, before pytest): when `package.json` has a
non-placeholder `scripts.test` AND a JS/TS file ships under `test(s)/`, propose the
declared runner via the existing `_choose_js_argv` (for run 11: plain `playwright`
dep + `scripts.test` → `npm test --silent`). The npm-init placeholder
(`"... no test specified ... && exit 1"`) is skipped — running it exits 1 with no
framework output and would register a **red-forever gate** (a wedge). The existing
`*.test.js` / pytest / no-`package.json`→`node <file>` paths keep priority and are
byte-unchanged.

*B is necessary but NOT sufficient.* For the run-11 artifact the declared test is
browser-based (imports `playwright`, spins a server), so the smoke run fails
`Cannot find module 'playwright'` (no in-loop `npm install`) and it is recorded as a
non-blocking `test_runtime_unavailable`. **B upgrades a silent `no_gate` into an
honest acknowledgement; it does not by itself block an inert mechanic.** Blocking is
SPEC-37's engine-owned behavioral oracle (and, for the team's own browser test, the
deferred in-loop provisioning).

**C — honest S4 provenance from the delivered tree.**
`_record_completion_oracles(store, workspace)` now consults the merged master tree
(`_tree_has_authored_test`: an authored-test filename, or a non-placeholder
`scripts.test`) and reports one of: *executed* (a registered acceptance gate),
*NOT executed* (an unrun was recorded), **authored but NOT registered/runnable** (a
test artifact is on master the gate could neither register nor run — the run-11
case), or *none authored*. Provenance-only, non-blocking, fully fail-open.

## Regression locks

1. A `*.test.js` file, a pytest suite, and a no-`package.json` JS test keep their
   exact pre-SPEC-36 detection (the `.test.js` branch runs first; `node <file>`
   fallback preserved).
2. The npm-init placeholder `scripts.test` is never proposed (no red-forever gate)
   and never counts as an authored test in provenance.
3. A project with a genuinely clean tree (no test file, no declared test) still
   reports `none authored`; S4 never invents an authored test (fail-open to False).
4. C is provenance-only — it cannot block `done`, run a command, or change the gate
   verdict; a workspace read error degrades to today's behavior.

## Definition of done

- A project that ships `test/acceptance.js` + `scripts.test` has an acceptance
  command **proposed** (then registered if runnable, or honestly recorded
  `test_runtime_unavailable` if not) — no longer a silent `no_gate`.
- `done` never reports "none authored" when a test artifact is present on master.
- All existing detection/golden paths and the SPEC-34/35 suites stay green.
