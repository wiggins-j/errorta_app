# Spec 28 — End-to-end autonomy acceptance

**Source:** `docs/specs/ROADMAP-autonomy.md` Phase 4 — *"28 last, and it is the
only item that can prove the roadmap worked"* — and the roadmap's flat admission:
*"no run has ever completed."*
**Target version:** v0.1 (test suite + `python/pyproject.toml`; **no engine
change**)
**Status:** proposed
**Owner:** wiggins-j

---

## Problem

Three autonomous gravity-golf runs. Zero finished products.

| Run | What the team did | Why it ended |
|---|---|---|
| 2026-07-24 | 96 PRs, 30% merge rate, 14-deep revise chains | `budget_exhausted` |
| 2026-07-26 #1 | 6 PRs, 6 approved, 6 merged, 0 revises, 0 context requests | `gate_not_improving` — on a **green** gate |
| 2026-07-26 #2 | game loop merged, 7 PRs merged, 2 open | `no_progress` — the PM was **locked out** of a legal turn by the schema |

Twenty-eight specs and gap-fixes have shipped against that evidence. Every one of
them is verified **in isolation**: `python/tests/coding/` collects **2,169** tests
today (`pytest tests/coding --collect-only -q`, 2026-07-26), and each locks a unit
— a detector function, a Pydantic validator, a probe verdict, a prompt segment.

**Not one of them asserts that a run finishes.**

The closest two, read carefully, are both weaker than they look:

* **`python/tests/coding/test_autonomy_loop.py:48`** —
  `test_loop_runs_to_definition_of_done` reaches `DEFINITION_OF_DONE`, but it
  passes `run_turn=FakeTeam().run_turn` (`:26`). `FakeTeam` **replaces the entire
  turn controller**: no model call, no schema parse, no workspace write, no branch,
  no PR, no merge gate, no acceptance gate, no probe. It tests `decide_next` +
  `_apply_outcome` bookkeeping and nothing else.
* **`python/tests/coding/test_coding_runner.py:117`** —
  `test_full_pr_flow_accumulates_and_completes` is the real thing: a scripted
  caller through `CodingRunner` → `build_run_turn` → `run_coding_loop`, real git,
  real PRs, real merges, a real subprocess test command. But the project is
  `calc.py` (`add` + `subtract`), and the transcript is **frictionless** — three PM
  turns, two dev turns, every review approves first time. It asserts source text on
  master. It never asserts anything *runs*.

So the aggregate claim — *"the loop, driven by realistic agent behaviour, produces
a working artifact"* — has never been made by any test. And the specific claim the
whole GL01 batch exists to support is worse than untested: **grep the suite for
`web_probe`, `PROBE_COMMAND_ID`, or `_web_probe_arm` and exactly one file
matches** (`python/tests/coding/test_gl01_web_probe.py`), which calls
`web_probe.run_and_record` and `runner._web_probe_arm` *directly*. **No test drives
`CodingRunner` against a web project at all** — the seven files that use
`CodingRunner` are all Python-calc or route-shaped. The probe arm's *reachability
from a real run* is unproven in the suite, and the roadmap's field evidence agrees:
`test_spec21_no_false_stops.py:86` reproduces "what the gravity-golf run actually
recorded, twice" and it is `{"command_id": "runtime:launch", "exit_code": 0}`.
`web:probe` has never appeared in a real run's ledger.

Without an aggregate assertion, the roadmap's other six specs are **unfalsifiable**.
Spec 23 can convert a heuristic stop into a continuation and still leave the run
unable to finish. Spec 25 can make every turn shape expressible and still leave the
PM unable to converge. Spec 27 can narrow instead of kill and still narrow into a
dead end. Each will ship green unit tests, and we will be exactly where we are
today: a growing pile of verified parts and no working whole.

## Why the existing machinery didn't catch it

Three structural reasons, each fixable and each addressed by an item below.

**1 — The one end-to-end test that exists has no friction, so no detector can
fire.** `test_coding_runner.py:117` runs 2 PRs over ~10 iterations. The default
`gate_stall_limit` is **8** (`autonomy.py:126`), `pm_idle_limit` is **2**
(`:71`), `plan_streak_limit` is **6** (`:134`), `convergence_stall_limit` is **20**
(`:103`). None of them has room to trip in that run. A test that cannot reach a
detector cannot lock it. *Both* 2026-07-26 stops are structurally invisible to it.

**2 — Spec 21's locks are unit-level by construction.** They are good tests —
`test_spec21_no_false_stops.py:105` calls `_account_gate_stall` directly against a
hand-built `_Ledger` stub (`:70`), and `:35`/`:44` construct `PMPlanIntent`
directly. That proves the *detector* and the *validator* behave. It cannot prove
the **loop** behaves: that the green gate a real `_run_gate`/`_web_probe_arm`
records actually flows into `c.last_gate_best` the way the stub does, or that a
`decision`-keyed PM turn survives `parse_coding_turn` → `_materialize_pm_tasks` →
`_apply_outcome` and resets `c.pm_idle`. Every one of the three known false stops
is a *composition* bug that looked fine at every unit boundary.

**3 — There is no CI, and the markers that would tier a slow test are inert.**
`.github/workflows/` does not exist; `CONTRIBUTING.md:15-27` states GitHub Actions
is intentionally OFF and the merge gate is the local sequence ending in
`( cd python && pytest )`. `python/pyproject.toml:84-98` declares `live`,
`acceptance`, `e2e`, `smoke`, `blocking`, `manual` and `flaky` markers — and
`[tool.pytest.ini_options]` has **no `addopts`**, so nothing deselects them. A
`live`-marked test added today would run inside the merge gate and spend real
money on every `pytest`. The markers are documentation, not enforcement.

## Goals

- **One fixture, gravity-golf-shaped** (buildless web: `index.html` + ES-module JS
  + `<canvas>`), driven through the **real** loop — `CodingRunner` →
  `build_run_turn` → `run_coding_loop` → real workspace/branch/PR/merge/gate/probe
  — to `definition_of_done`.
- The fixture is **deterministic** and hermetic enough to sit in the merge gate:
  `( cd python && pytest )` runs it on every PR, unattended, offline, free.
- The transcript carries **friction** — a rejected review, a revise, a duplicate
  task, a context request — so the convergence detectors are actually reached.
- **"Working artifact" means executed, not inspected**: the assertion reuses GL01's
  web probe (`web_probe.py` + `scripts/web-probe.mjs`), the oracle the engine
  already owns, and proves that oracle is **reachable from a real run**.
- A **stop-reason budget**: not just *that* it finished but *how* — no heuristic
  stop, bounded revise depth, no context-request exhaustion, bounded superseded
  PRs. The roadmap's "how we will know it worked" list, made executable.
- The three known false stops get **loop-level** regression locks, above Spec 21's
  unit-level ones.
- A second, **non-gating** tier that runs the same fixture against real models with
  a real browser, with an explicit spend cap.

## Non-goals

- **Not a benchmark suite.** One fixture, one shape. This spec does not build a
  task corpus, a scoreboard, or a cross-model comparison. If the council ever needs
  a benchmark, that is a separate product decision with a separate cost model.
- **Not a test of model quality.** Tier 1 scripts the model's output precisely
  *because* model quality is not deterministic and is not what is broken. The
  failure report's finding is that the harness stopped a working team
  (`multi-agent-pipeline-failure-report.md` §3, Pathology 1) — that is a harness
  property and it is testable. Whether a real PM writes a *good* plan is Tier 2's
  weak, advisory signal and nothing more.
- **Not a replacement for the 2,169 unit tests.** They localise. This localises
  nothing — a red Tier 1 says "the run stopped at iteration 14 with reason X" and
  the unit suite says which part. Both are needed; this spec adds one, deletes
  none.
- **Not an engine change.** Nothing under `errorta_council/` is touched. The only
  non-test edit is `addopts` in `python/pyproject.toml` (Item 7). If Tier 1 needs
  a new seam to be writable, that is a finding to report, not a licence to patch
  the engine inside this spec.
- **Not a delivery/export test.** `deliverable.deliver()` is called from the
  merge-back HTTP route (`errorta_app/routes/coding.py:3894`), not from the loop.
  Tier 1 asserts against the integrated master tree the loop actually produces;
  the export step keeps its existing route tests.

---

## Item 1 — The determinism decision: a scripted caller, not a recording

**The problem.** A real run makes 50–500 model calls. They are slow (the
2026-07-24 run took 3h20m), they cost money, and they are not reproducible. A test
that gates merges cannot have any of those properties. Everything *else* in the
loop is already deterministic: git, the workspace, the PR ledger, the merge gate,
the acceptance-command subprocess, the `python -m http.server` launch. **The model
is the only non-determinism**, and the engine already has a seam for it.

**The chosen approach — a stateful scripted caller.** `CodingRunner.__init__`
takes `caller: MemberCaller` (`runner.py:6276`) and hands it straight to
`build_run_turn` (`:6342`), which is the single funnel every model call in the
loop passes through (`runner.py:4170`, and the wrapping `def caller` at `:4216`).
Roughly fifty tests already exploit this. The canonical shape is
`test_coding_runner.py:86` — a callable object keyed on the prompt's role banner:

```python
class FakeGateway:
    def __call__(self, member: dict, prompt: str) -> str:
        if "You are the PM" in prompt: ...
        if "You are a developer" in prompt: ...
        if "DELIVERY reviewer" in prompt: ...
```

Spec 28's caller is the same idea, hardened in two ways:

* **Keyed on `(role, ledger state)`, never on call index.** The turn *order* is
  decided by `decide_next` (`topology.py:238`) and by the concurrent loop's batch
  planner. A positional script would break on any scheduler change and would
  silently test a different sequence than the one it claims to. The fixture caller
  resolves the role via the prompt banner (as the existing tests do) and the task
  via the prompt's `task id '<id>'` echo (`test_coding_runner.py:37`), then
  answers from a **per-task state machine** — "this task has been rejected once, so
  emit the fixed version". Reordering the schedule cannot change the answers.
* **Every emitted envelope is a real `coding_turn.v1` document** that must survive
  `parse_coding_turn` unaided. The fixture asserts `counters.turns_repaired == 0`
  (Item 5): if the parser has to repair a turn the fixture believes is legal, that
  is a Spec 25 finding surfacing as a red test, which is exactly what we want.

**Δ note — why not a recorded/VCR transcript.** Recording a real run and replaying
it is the obvious alternative and it is wrong here for two independent reasons.
*(a)* A replay must be keyed on something. Keyed on prompt bytes, it invalidates on
every prompt edit — and this repo edits prompts constantly and locks their bytes
with a golden test (`test_prompt_segments_golden.py`, which exists precisely
because prompt bytes drift). Keyed on turn index, it is a positional script with a
large opaque payload, i.e. strictly worse than the state machine above. *(b)* A
recording of a run that **stopped early** encodes the pathology, not the cure:
there is no recording of a completed run to replay, because no run has completed.
The scripted caller is not a compromise — it is the only artefact that can express
"what a healthy run looks like" before a healthy run exists.

**Δ note — why not "just run it live in CI".** There is no CI
(`CONTRIBUTING.md:15-19`). Even if there were, a merge gate that costs money and
takes hours per PR does not get run, and a gate that is not run is not a gate. Live
is Tier 2 (Item 7) and it is explicitly non-gating.

**Which tier gates:** **Tier 1 gates merges.** Tier 1b (browser-backed, Item 4)
gates when the toolchain is present and skips cleanly otherwise. Tier 2 (live)
never gates; it is a manual/scheduled release signal.

## Item 2 — Tier 1: the hermetic loop acceptance test

**New file:** `python/tests/coding/test_spec28_autonomy_acceptance.py`
**Markers:** `pytestmark = [pytest.mark.acceptance, pytest.mark.e2e,
pytest.mark.blocking]` — `acceptance` because it chains a full journey, `e2e`
because Tier 1b drives a browser, `blocking` because a run that cannot finish
holds a release regardless of what else is green (`pyproject.toml:92`).

**Shape.** One project, greenfield (`target="new"`), buildless web. The scripted
team builds a gravity-golf-shaped artifact: `index.html` with relative
`<script src>` ES modules, `src/render.js` sizing and painting a `<canvas>`,
`src/levels.js`, `src/main.js`. The content is deliberately small — it exists to be
*served, loaded, and painted*, not to be a game. The fixture sources live in
`python/tests/coding/fixtures/spec28_gravity_golf/` and the scripted DEV turns emit
them via `{"tool": "code_write", "args": {"path": ..., "content": ...}}`
(`test_coding_runner.py:63`).

**The run.** `CodingRunner("spec28-…", MEMBERS, ScriptedTeam(), root=tmp_path)`
with a PM, two DEVs, a REVIEWER and a TESTER, then
`.run(CodingAutonomyPolicy(checkpoint_cadence=CADENCE_OFF, max_iterations=80))`.
Everything downstream of the caller is production code:
`gate_bootstrap.maybe_bootstrap` (`runner.py:6329`) detects the static profile
(`runtime.py:1051` — `python -m http.server {port} --bind 127.0.0.1`),
`_run_gate` (`runner.py:3837`) executes registered commands for real,
`_web_probe_arm` (`runner.py:3898`) fires off the merge-armed `GateRun`
(`:4880`), and the PM's `done=true` routes through `_apply_outcome`'s
`project_done` branch (`autonomy.py:2349`) into the real delivery review — a real
reviewer turn, the real command gate, the real `_delivery_launch_evidence`
(`runner.py:2992`), the real probe (`:6060`) — before `set_project_status("done")`,
after which `decide_next` returns `Complete("definition_of_done")`
(`topology.py:253`, consumed at `autonomy.py:1812`).

**Two variants, parametrized on the acceptance registry.** This is the point of
using a *web* fixture rather than reusing the calc one:

* **A — registry empty (the gravity-golf configuration).** No test commands are
  registered and `gate_bootstrap._detect_acceptance_command`
  (`gate_bootstrap.py:107`) proposes nothing, because the tree carries no
  `*.test.js` and no `tests/*.py`. `_run_gate` therefore returns `None` and runs
  nothing. **The `web:probe` is the only acceptance signal in the entire run.**
  This variant is GL01's founding premise, executed end to end for the first time.
* **B — registry armed.** The test pre-registers an acceptance-scoped command with
  a `sys.executable` argv (the `test_coding_runner.py:126` idiom, which avoids
  `gate_bootstrap`'s bare `"python"` argv and its host-`PATH` fragility) asserting
  a property of `src/levels.js`'s level data. Proves the command gate and the probe
  arm coexist and that both must be green for delivery to pass.

**Core assertions (both variants).**

1. `res.stop_reason == DEFINITION_OF_DONE`.
2. `store.get_project().status == "done"` and `completion_summary` is non-empty.
3. `index.html` and every path in its `<script src>` graph exist on master (Spec
   13's own foundation predicate, asserted on the artifact rather than on the
   detector).
4. A `web:probe` run exists in `store.list_test_runs()` whose `head` is the
   delivered head and whose `command_ids == [web_probe.PROBE_COMMAND_ID]` —
   **the probe arm was reached by a real run.**
5. The probe verdict is **load-bearing, not decorative**: a third parametrization
   scripts a RED probe verdict (`non_black: False`) and asserts the run does
   **not** reach `definition_of_done`. A green-only assertion would pass against
   an arm that was silently disabled.
6. Variant B additionally: the recorded acceptance session `passed` is `True` and
   its `head` equals the delivered head.
7. The stop-reason budget (Item 5) and the three false-stop locks (Item 6).

## Item 3 — The friction requirement: how the fixture stays honest

A scripted transcript is only as good as the behaviour it scripts. A transcript
where every review approves and every plan lands is a test of the happy path, and
the happy path is not what broke. **The fixture is required to contain all five of
the following, and a companion meta-test asserts each one occurred** — so that a future
edit which quietly smooths the transcript fails loudly instead of silently
weakening the gate.

**F1 — A rejected review and a revise.** One DEV PR draws a blocking finding
(`ReviewerVerdictIntent` with `severity: "blocking"`, `schemas.py:326`), which
spawns a `revise:` task; the DEV's next turn for that task emits the corrected
file; the re-review approves; the PR merges. *Meta-assert:* at least one PR reached
`changes_requested`, and at least one task title starts with `revise:`.

**F2 — A duplicate task in a PM batch.** One PM plan turn proposes a task that
duplicates an open one. `_materialize_pm_tasks` (`runner.py:2419`) rejects it and
returns `made_progress=False`, which increments `c.pm_idle` (`autonomy.py:2326-2330`).
*Meta-assert:* a duplicate-rejection decision was recorded and `c.pm_idle` reached
at least 1 during the run.

**F3 — The Spec 21 prune turn.** The PM's very next turn is the exact envelope
that locked run 3 out: `{"kind": "plan", "done": false, "tasks": [],
"decisions": [{"decision": "drop the duplicate HUD task", "rationale": "…"}]}` —
no `title` key, no tasks, not done. It must parse (`PMDecision`'s
`_accept_decision_as_title`, `schemas.py:104`), must satisfy `_done_rules`
(`schemas.py:133`), and must **reset** `pm_idle` rather than incrementing it into
`pm_idle_limit=2`. This is F2's whole purpose: F2 arms the counter, F3 must
disarm it.

**F4 — A context request.** One DEV turn emits a
`DeveloperContextRequestIntent` (`schemas.py:200`); the loop answers it; the DEV's
next turn proceeds normally. *Meta-assert:* the task's
`context_request_attempts` ended at exactly 1 — the channel was used and did not
saturate. `_CONTEXT_REQUEST_LIMIT` is 3 (`runner.py:1837`).

**F5 — Enough iterations for the detectors to be live.** The transcript must
produce a run long enough that `gate_stall_limit=8` (`autonomy.py:126`) is
*reachable*: at least 12 iterations after the first green gate signal. Roughly six
tasks × (dev → review → merge) gets there. *Meta-assert:*
`res.counters.iterations >= 12`. Without this the green-gate lock (Item 6, L1) is
vacuous — and vacuous is precisely how `test_coding_runner.py:117` misses it today.

## Item 4 — What "a working artifact" means, mechanically

**Reuse GL01's oracle. Do not invent a second one.** `web_probe.run_and_record`
(`web_probe.py:239`) already defines "working" for a buildless web target: stand
the detected static runtime up, wait for it to answer, drive
`scripts/web-probe.mjs` under Playwright, and record a `web:probe` `TestRunResult`
that `passed` **only when the console is clean AND the first canvas (or the
viewport) is non-black after N rendered frames** (`_verdict_to_result`,
`web_probe.py:178`). `scripts/web-probe.mjs:125-130` emits the verdict, including
the verbatim `"frame is uniformly black (mean=…, var=…)"` reason. That is exactly
the P2 defect the 2026-07-24 artifact shipped with —
`docs/coding/GRAVITY_GOLF_PRODUCT_FIXES.md:45-49` — *"zero console errors, fully
initialized game state, and a pure black screen"* — the failure no other signal in
the pipeline can see.

**The honest gap this spec closes.** The probe is well unit-tested
(`test_gl01_web_probe.py`, 12 cases including `_web_probe_arm` at `:219`), and it
has **never fired in a real run**. The only runtime evidence the live runs
recorded was `runtime:launch` (`runner.py:3817`, reproduced from the real ledger
at `test_spec21_no_false_stops.py:86`). Assertion 4 in Item 2 — a `web:probe` row
bound to the delivered head, produced by a run nobody hand-drove — is the first
proof that the arm is reachable in practice.

**Three levels of browser realism, deliberately separated.**

* **Tier 1 (always, gating).** The browser is seamed:
  `monkeypatch.setattr(web_probe, "_default_node_runner", …)` — the exact seam
  `test_gl01_web_probe.py:230` already uses, and the only seam available, since
  `_web_probe_arm` calls `run_and_record` without a `node_runner` argument
  (`runner.py:3924`). The **launch machinery is real** (a real
  `python -m http.server` child, as GL01's own suite does). Tier 1 therefore
  proves *the arm runs, is bound to the right head, and its verdict decides the
  outcome* — it does **not** prove pixels. The spec says so plainly rather than
  letting a scripted `non_black: True` masquerade as a rendering assertion.
* **Tier 1b (skipif-gated, gates when present).** The same fixture with
  `_default_node_runner` **not** patched, so the real `scripts/web-probe.mjs`
  drives real Chromium against the real served artifact. Gated by the predicate
  that already exists — `test_gl01_node_probe_smoke.py:35`'s
  `_playwright_available()`, applied as a `pytest.mark.skipif`
  (`test_gl01_node_probe_smoke.py:51`). On a machine with `node` +
  `@playwright/test` + a Chromium binary (`package.json:40` declares the
  devDependency), this asserts *the canvas is non-black and the console is clean*
  for real. Elsewhere it skips, exactly as GL01's probe smoke does today.
* **Tier 2 (live, never gating).** Item 7.

**Δ note — why not assert on a screenshot.** `review_screenshot` exists and is
default-OFF (`autonomy.py:179`). Image comparison is a second oracle with its own
flakiness surface (font rendering, GPU, platform) and it answers a question the
probe already answers numerically. The probe's mean/variance verdict is the
oracle; the screenshot stays a human debugging aid (`probe_screenshot` on the PR
record, `web_probe.py:213`).

## Item 5 — The stop-reason budget

`res.stop_reason == DEFINITION_OF_DONE` is necessary and badly insufficient: a run
can reach `done` after 180 wasted iterations, 40 superseded PRs and a saturated
context budget, and that run is a failure we would be shipping green. The fixture
asserts **how** it finished. Each line below is one of the roadmap's "how we will
know it worked" criteria made executable.

| # | Assertion | Anchor / rationale |
|---|---|---|
| B1 | `res.stop_reason == DEFINITION_OF_DONE` | roadmap criterion 1 |
| B2 | No `LoopResult` on any **heuristic** reason: `NO_PROGRESS`, `NOT_CONVERGING`, `GATE_NOT_IMPROVING`, `PLANNING_CHURN`, `DISPATCH_WEDGED`, `REVISE_LIVELOCK`, `COMPLETION_BLOCKED`, `WORKER_UNPRODUCTIVE`, `MEMBER_UNHEALTHY`, `DELIVERY_REVIEW_STALLED`, `HARD_BLOCKER`, `NO_ACTIONABLE_WORK` | constants at `autonomy.py:40-55`; roadmap criterion 2. `BUDGET_EXHAUSTED` is a *hard* stop, not heuristic — the roadmap grants it explicitly — but it is still a fixture failure, restated numerically by B6 |
| B3 | Zero `revise_chain_broken` decisions, and no revise lineage deeper than `revise_chain_limit` (3, `autonomy.py:195`) | the 14-deep chains of run 1; `runner.py:994` records the decision |
| B4 | Zero `context_request_exhausted` outcomes; every task's `context_request_attempts < _CONTEXT_REQUEST_LIMIT` (3, `runner.py:1837`) | `runner.py:5153` is the exhaustion branch |
| B5 | Over `store.list_prs()`: `superseded <= 1`, merge-rate `>= 0.8` | run 1 was 53/96 superseded at a 30% merge rate — the calibration point named at `autonomy.py:232`. GL04's clamp trips at ratio ≥ 0.5 / merge-rate ≤ 0.35 (`autonomy.py:233-234`), so this band is comfortably inside "healthy" |
| B6 | `res.counters.iterations <= 60` (against `max_iterations=80`) | a run that only finishes by nearly exhausting its budget is a regression even when green |
| B7 | `res.counters.turns_repaired == 0` | every scripted envelope is legal by construction; a repair means the parser is repairing something the fixture asserts is valid — a Spec 25 finding |
| B8 | `res.counters.model_escalations == 0` and `task_reassignments == 0` | the F127 ladder is a recovery path; a healthy run should not need it |
| B9 | No `foundation_not_converging` decision | `autonomy.py:945`; the foundation must land, not stall |

**On B2 and Spec 23.** Once Spec 23 lands, a heuristic condition becomes a
last-word turn rather than a stop. B2 is then restated as its stronger form:
*every heuristic condition that fired must have a recorded PM intervention
decision, and the run continued* — which is roadmap criterion 2 verbatim. Until
23 lands, B2's "did not end on one" form is the strongest available statement and
is already enough to catch both 2026-07-26 stops.

## Item 6 — Loop-level regression locks for the three known false stops

Spec 21 locked these at the unit level. This item locks them where they actually
broke: in composition. **This is the spec's central justification — Tier 1 as
specified would have caught both 2026-07-26 stops, and neither existing test
could have.**

**L1 — A green gate does not stop a healthy run.** *(Run 1: `gate_not_improving`
at iteration 22 with 6/6 PRs merged.)* The fixture's variant A run holds a green
`web:probe` across ≥ 12 iterations after the first probe result (F5). Assert
`res.stop_reason == DEFINITION_OF_DONE` and that no `gate_not_improving` decision
or monitor signal was raised. `test_spec21_no_false_stops.py:105` proves
`_account_gate_stall` returns `None` for a hand-built green `_Ledger` stub; L1
proves the *real* green signal — a probe verdict recorded by
`web_probe.run_and_record`, read through `_gate_fingerprint` into
`c.last_gate_best` — behaves the same. The gap between those two statements is
exactly where run 1 died, because the score is "how many commands pass": a green
gate sits at its maximum and can never strictly improve.

**L2 — The PM schema lockout cannot recur.** *(Run 2: `no_progress` while two PRs
were open, after four rejected PM turns.)* F2 + F3 reproduce the sequence at loop
level: duplicate batch → rejection → `pm_idle` at 1 → the prune turn
(`done=false`, `tasks=[]`, one decision) → `pm_idle` back to 0. Assert the run
never reaches `pm_idle_limit=2` (`autonomy.py:71`) and never stops `no_progress`.
Spec 21 proves the *validator* accepts that envelope; L2 proves the **loop**
credits it as progress — a different claim, and the one that was false.

**L3 — The `decision` field-name synonym survives the full path.** F3's envelope
uses `{"decision": …}` with no `title`. `schemas.py:104` maps it; L3 asserts the
resulting decision is visible in `store.list_decisions()` with the mapped title
after passing through `parse_coding_turn` → `_materialize_pm_tasks` →
`_apply_outcome`. Run 2 lost three of four PM retries to
`missing decisions[0].title`.

**Sequencing note — write it before the fixes, do not `xfail` it.**
`CONTRIBUTING.md:30` forbids `xfail`, and rightly. So Spec 28 lands in two
commits rather than one: **28a** ships the fixture, the run, Item 2's core
assertions and the Item 5 budget with a *frictionless* transcript — that passes
today and is already more than the suite has. **28b** adds F1–F5 and L1–L3, and
lands **with or immediately after** the specs that make them pass (23 for the
last-word turn, 25 for expressibility, 27 for narrow-instead-of-kill). The
roadmap's "28 last" means 28 *proves* the roadmap; it does not mean 28's harness
must wait for it.

## Item 7 — Tier 2: the live smoke run

**New file:** `python/tests/coding/test_spec28_live_smoke.py`
**Markers:** `pytestmark = [pytest.mark.live, pytest.mark.acceptance,
pytest.mark.e2e, pytest.mark.manual]`.

Marker discipline, read against `pyproject.toml:84-98`:

* `live` — *"needs real models/network; non-gating (nightly/manual opt-in)"*.
  Correct and load-bearing.
* `manual` — *"not fully automatable; tracked for human release sign-off"*.
  Correct: with no CI, "nightly" is a maintainer's local schedule and the release
  signal is a human confirming it passed.
* **`blocking` is deliberately NOT applied.** *"A failure here holds the release
  regardless of other results"* — a live run that fails because a provider is down
  or a key expired must not hold a release. The release gate is the documented
  sign-off, not a pytest marker.
* `smoke` is also not applied: *"minimal liveness set; fast, runs on every push"*
  is the opposite of this test.

**The load-bearing prerequisite — make the markers real.** Add to
`python/pyproject.toml`'s `[tool.pytest.ini_options]`:

```toml
addopts = "-m 'not live and not flaky and not manual'"
```

Today there is no `addopts`, so a `live` test would run inside
`( cd python && pytest )` and spend money on every PR. Verified inert as a change:
no test in `python/tests/` currently carries `live`, `flaky` or `manual`. This one
line is what lets Tier 2 exist at all, and it simultaneously makes `flaky`'s
declared contract — *"never part of the merge gate"* — true instead of aspirational.

**What it runs.** The same north star and definition of done as the Tier 1
fixture, against real members (real routes, real Opus-class models), with the real
`_default_node_runner` (real Playwright, real Chromium), on a real workspace.
Assertions: B1 (`definition_of_done`), Item 2 assertions 3–4 (artifact loads, a
green `web:probe` bound to the delivered head), and a **relaxed** budget — a live
PM will genuinely need revises and context requests. The strict Item 5 bands stay
in Tier 1 where they are meaningful.

**Cost and cadence — stated honestly.** This tier spends real money and real time.
The 2026-07-24 run is the reference for what "unbounded" costs: 96 PRs over 3h20m.
Tier 2 is capped by the engine's own hard budget rather than by hope:

* `max_model_calls = 120` — a hard cap enforced before dispatch by
  `reserve_model_calls` (`autonomy.py:840`), including for the concurrent loop, so
  a parallel batch cannot overshoot it.
* `max_iterations = 60`.
* A wall-clock cap of 45 minutes via `should_cancel`, which the loop honours at
  the top of each iteration (`autonomy.py:1807`) and inside the probe's
  ready-wait (`web_probe.py:99`).
* An env guard: the test skips unless `ERRORTA_LIVE_ACCEPTANCE=1` **and** a live
  route is configured. `-m live` on a laptop must not start spending by accident.
* **Cadence: weekly, and mandatory within 7 days of a release cut.** Not nightly —
  nightly implies an automation that does not exist here, and a schedule nobody
  runs is worse than an honest weekly one a maintainer actually performs. Driven by
  `scripts/live-acceptance.sh` (thin wrapper: sets the env guard, runs
  `pytest -m live tests/coding/test_spec28_live_smoke.py -q`, prints the ledger's
  stop reason and the model-usage rollup).

**Order-of-magnitude cost — an estimate, flagged as such.** At 120 frontier model
calls with coding-sized prompts, expect single-digit to low-double-digit dollars
per run and 20–45 minutes wall clock. The **enforced** figure is the model-call
cap, which is exact; the dollar figure is a translation of it and should be
re-derived from the run's own usage rollup rather than trusted from this document.

---

## Implementation notes

- **New:** `python/tests/coding/test_spec28_autonomy_acceptance.py` (Tier 1 +
  Tier 1b), `python/tests/coding/test_spec28_live_smoke.py` (Tier 2),
  `python/tests/coding/fixtures/spec28_gravity_golf/` (the artifact sources the
  scripted DEV writes), `scripts/live-acceptance.sh`.
- **Changed:** `python/pyproject.toml` — add `addopts` to
  `[tool.pytest.ini_options]` (Item 7). The single non-test edit in this spec.
- **Unchanged:** everything under `python/errorta_council/`. If Tier 1 cannot be
  written without a new engine seam, **stop and file the finding** — an
  end-to-end test that needs production code bent to accommodate it is testing the
  bend.
- The suite's `python/tests/coding/conftest.py` already pins `ERRORTA_HOME`,
  `HOME` and `USERPROFILE` under `tmp_path` autouse, so the fixture inherits
  hermeticity for free — including the `ApplyWorkspace` snapshot root that used to
  leak into the developer's real `~/.errorta`.
- Reuse, do not re-derive: the envelope builders (`_pm_env` / `_dev_env` /
  `_rev_env` / `_tester_env`, `test_coding_runner.py:50-80`), the prompt parsers
  (`_task_id` `:37`, `_pr_head` `:41`, `_delivery_head` `:45`), the web-project
  builder (`_web_project`, `test_gl01_web_probe.py:42`), and the Playwright
  predicate (`_playwright_available`, `test_gl01_node_probe_smoke.py:35`). If a
  helper is shared by three files it moves to `python/tests/coding/conftest.py`;
  below three it stays local.

## Edge cases

- **A fixture that passes because the transcript is too easy.** The realest risk
  in this spec, and the reason Item 3 exists as a *requirement* with its own
  meta-assertions rather than as advice. F1 (rejected review + revise), F2
  (duplicate task), F3 (the prune turn), F4 (context request) and F5 (≥ 12
  iterations) are each asserted to have *occurred*, so an edit that smooths the
  transcript turns the gate red instead of quietly hollowing it out. A reviewer's
  standing question on any change to this file: *which detector can still fire?*
- **Flakiness policy.** Tier 1 is deterministic by construction; if it flakes, the
  flake **is the finding** — a real non-determinism in the loop (a thread race in
  the concurrent dispatcher, an unordered ledger read, a timing-dependent gate
  fingerprint) — and it gets root-caused. It is **never** marked `flaky`:
  `pyproject.toml:97` defines that marker as *"quarantined until root-caused; never
  part of the merge gate"*, and quarantining the one test that proves the product
  works would defeat the entire spec. The legitimate escape hatch for
  environment-dependent behaviour is Tier 1b's `skipif` on toolchain
  availability, which is a *capability* check, not a flake suppressor.
- **No `node` / no Playwright / no Chromium.** Tier 1 is unaffected (the runner is
  seamed). Tier 1b skips with the same reason string GL01's probe smoke already
  uses. Tier 2 fails loudly — a live acceptance run without a browser is not a
  smoke test.
- **The concurrent loop.** `run_coding_loop` dispatches to `_run_concurrent_loop`
  when `runtime_cap(...) > 1` (`autonomy.py:903`), and a greenfield project is
  clamped to 1 worker until the foundation merges (`autonomy.py:93-98`), then hands
  *back up* (`autonomy.py:1802`). The fixture must therefore exercise **both**
  loops in one run — which is desirable, and is why the caller is keyed on state
  rather than on call order. A second parametrization pinning
  `max_parallel_workers=1` isolates the sequential path when the concurrent one
  fails, for diagnosis.
- **Wall-clock cost of Tier 1.** It spawns real subprocesses (the acceptance
  command, `python -m http.server`) and does real git work, so it is seconds, not
  milliseconds. That is acceptable for one test in a 5,286-test suite; if it
  exceeds ~60s it should be profiled, not marked slow.
- **`gate_bootstrap`'s one-shot memo.** `_mark_cmd_resolved`
  (`gate_bootstrap.py:264`) sets `gate_cmd_bootstrap_resolved` after a single smoke
  attempt, so a test file that appears on master *later* in the run will not arm
  the gate. Variant B must therefore pre-register its command, not expect
  bootstrap to find one mid-run. Stated here because it is a non-obvious trap.
- **A future engine change that legitimately alters the transcript.** If the
  prompt banners the caller keys on are renamed, Tier 1 breaks loudly at the
  `_task_id` regex. That is correct: the fixture asserts a contract between the
  prompt builders and the turn parser, and it should not survive a silent change
  to either.

## Testing

The deliverable *is* tests, so this section states how the **tests themselves** are
verified — the meta-layer that keeps a green Spec 28 meaningful.

- **The friction meta-test** (Item 3): one test per F1–F5 asserting the condition
  occurred in the recorded run, so a hollowed transcript fails.
- **The negative control** (Item 2, assertion 5): a scripted RED probe verdict must
  prevent `definition_of_done`. Without this, a disabled probe arm passes.
- **The second negative control**: a scripted PM that never emits `done=true` must
  end `budget_exhausted`, not `definition_of_done` — proving the DoD assertion is
  reading the real completion path and not defaulting to it.
- **The marker lock**: assert `python/pyproject.toml`'s `addopts` deselects `live`,
  `flaky` and `manual`, in the established anti-drift-canary style
  (`test_f145_pm_reference.py`, `test_spec12_18_prep.py`). Without this lock the
  one line that makes Tier 2 safe can be deleted by anyone editing pytest config.
- **Determinism**: the Tier 1 test runs clean 20× consecutively before it is
  allowed into the merge gate. A single flake in 20 blocks the merge and gets
  root-caused (see the flakiness policy above).

## Documentation

- `docs/specs/ROADMAP-autonomy.md` — mark SPEC-28 as specified, and record which
  of the five "how we will know it worked" criteria are now executable (1, 2, 4 in
  part) versus still prose (3, 5).
- `CONTRIBUTING.md` — the local verification sequence gains one line: `pytest -m
  live` is **not** part of it, and why.
- `python/tests/acceptance/README.md` — cross-reference Spec 28 as the coding
  council's end-to-end journey, noting it lives in `tests/coding/` (for the
  hermetic-home conftest) rather than in `tests/acceptance/`.
- A short `docs/coding/` note on running Tier 2: the env guard, the cost, the
  cadence, and how to read the stop reason and usage rollup afterwards.

## Out of scope / follow-ups

- **A second fixture shape** (a Python CLI, a Node service) to prove the loop is
  not overfitted to buildless web. Worth doing once one shape is green; not on the
  critical path, and a second shape before the first one passes is procrastination.
- **`docs/TEST_AUTOMATION_PLAN.md` and `docs/TEST_CASES.md` do not exist**, though
  `pyproject.toml:86-88` and `python/tests/acceptance/README.md:3` both cite them
  as the source of the marker taxonomy and the `TC-NN.M` case ids. Spec 28 uses the
  markers as their inline descriptions define them and does not claim a TC id.
  Reconciling the docs with the markers is a real gap and a separate change.
- **Wiring Tier 2 to a scheduler.** With Actions off, this would be a local
  `launchd`/`cron` entry on the maintainer's machine. Deliberately left manual
  until Tier 2 has proven stable enough to be worth automating.
- **Asserting the delivery/export path** (`deliverable.deliver()`, the merge-back
  route at `errorta_app/routes/coding.py:3894`). Outside the loop, and it has its
  own route tests.
- **A cost regression signal** — tracking Tier 2's token spend run over run so a
  prompt change that doubles cost is visible. The usage rollup already records the
  data; nothing reads it as a trend.
