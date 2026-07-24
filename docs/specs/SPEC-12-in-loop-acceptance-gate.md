# Spec 12 — Execute the acceptance gate inside the loop

**Source:** `docs/coding/RUN_ANALYSIS_GRAVITY_GOLF_2026-07-24.md` §6 **S1 (P0)**
**Target version:** v0.1 (engine)
**Status:** proposed (revised after a code-grounded review — see the **Δ review** notes)
**Owner:** wiggins-j

> Continues the `Spec NN` series the code comments cite (Spec 01–11). This is the
> keystone spec of the gravity-golf batch.
>
> **Δ review reshaped this spec substantially.** Three findings changed the
> design rather than the prose: (a) registering a test command **arms the
> per-PR merge gate**, which would wedge every merge — the original Item 1 would
> have traded a livelock for a deadlock; (b) running the gate **inside** the
> merge turn serializes the whole team, directly cancelling
> [Spec 13](SPEC-13-foundation-gate-buildless-web.md); (c) the head-bound gate
> verification Item 4 proposed **already exists** in `delivery_review`. All three
> are corrected below.

---

## Problem

The gravity-golf run's Definition of Done was, in effect, *"iterate until the
acceptance gate passes"*. No member in the run could run the gate. So the loop
spent its last ~20 minutes on a single task — *"Run acceptance gate and fix
failures"* — that cycled dev → reviewer → `revise:` → dev forever, because the
reviewer (correctly, by the task text) demanded a strokes-per-level table and no
role could produce one.

Meanwhile the actual artifact had three defects that a single `node
test/acceptance.test.js` and one browser load surface in seconds:

- the test harness sabotages itself (`resources: 'usable'` re-executes the seven
  `<script src>` modules over the manually-injected copies, so a fresh
  never-initialized `window.GravityGolf` overwrites the initialized one);
- `Render.init()` sizes the canvas from `getBoundingClientRect()` at
  `DOMContentLoaded`, which returned 0 → `canvas.width = 0` → black screen;
- levels 3 and 5 are trivially solvable (straight-line path to the hole).

None of these are visible by re-reading a diff. All are visible by running.

## Why the existing machinery didn't catch it

The engine **already has a real, sandboxed execution path, and already runs the
whole registry deterministically against the merged tree**. It is simply never
reachable on a greenfield headless run. The links, each fine on its own:

1. **The executor exists** — `run_test_commands` (`testing.py:158-215`) runs
   allow-listed argv in a worktree via the F039 `LocalToolRunner` and derives
   `passed` from the real exit code.
2. **Two callers already drive it.** The tester turn (`runner.py:3487-3588`),
   which runs a **model-chosen** subset against the **branch** worktree; and —
   **Δ review, the one the original draft missed** — `delivery_review`
   (`runner.py:3984-4010`), which runs **every** registered command against
   `workspace.root()` (the merged master tree) bound to `head`, records it via
   `record_test_run`, and files a `"fix delivery tests"` task carrying Spec 11's
   verbatim stderr (`runner.py:4006`, `_failed_stderr_appendix`,
   `runner.py:452-469`). Its comment calls it *"deterministic (no model command
   selection) so the test verdict cannot be gamed"*. **That is exactly the call
   shape this spec needs — it exists, it is correct, and it runs once, at the
   very end.**
3. **But nothing ever populates the registry.** `set_test_commands`
   (`ledger.py:1235`) is called from exactly one route — `PUT
   /coding/projects/{id}/test-commands` (`routes/coding.py:3142`, the call at
   `:3152`), i.e. the app UI or `errorta test-commands set` — plus the
   `python/scripts/*` validators. No planning, foundation, or bootstrap path
   seeds it. A greenfield project starts empty and stays empty.
4. **So every gate is vacuously satisfied.** `_set_mergeable_if_ready`
   (`runner.py:2577-2601`) treats tests-green as satisfied when the registry is
   empty — deliberately, else nothing would ever merge. A tester task is only
   spawned when it is non-empty (`runner.py:3448`). `delivery_review`'s test step
   is `if registry:`. Result: every PR merged on review approval alone, and
   **tester-1 took 0 turns in a run whose DoD is one giant test loop.**

The runtime half has the identical shape. `_has_runnable_runtime`
(`evidence.py:51-60`) reads only **stored** profiles via
`RuntimeProfileStore.list_profiles` (`runtime.py:296`). The detector that would
produce one — `detect()` (`runtime.py:1293-1335`) — is called from exactly two
places: `POST /projects/{id}/runtime/detect` (`routes/coding.py:3267`) and
`runtime_resolve.py:525`. Neither is on the autonomous path.

Downstream, this makes two more guards structurally blind:

- **Spec 04's gate-stall detector** (`_account_gate_stall`, `autonomy.py:818`)
  keys on `_gate_fingerprint` (`autonomy.py:405-440`), which returns the
  no-signal sentinel `((), -1)` when there are no test runs. With zero test runs
  it can never trip, by design.
- **F146 Slice C's launch probe** (`_delivery_launch_evidence`,
  `runner.py:1853-1900`) does launch the delivered head for real — but only at
  **delivery**, and only for a stored runnable profile. This run never reached
  delivery (delivery root empty), and had no stored profile.

So the honest summary is not "the harness cannot execute". It is: **the harness
executes correctly, once, at the end, on a registry nothing ever fills.**

## Goals

- A greenfield run acquires a **real acceptance command and/or runnable runtime
  profile automatically**, without an operator visiting the UI.
- That gate **runs during the build loop** — periodically against the merged
  master tree — not only at delivery.
- Its **verbatim output** reaches the next dev and reviewer prompts, so "iterate
  until green" has a feedback signal.
- Spec 04's `gate_not_improving` detector becomes live (it gets a real signal to
  key on), so a run that churns at a fixed failure count stops.
- **None of this arms a new merge blocker.** (Δ review — this is now a
  first-class goal, not an assumption.)

## Non-goals

- Not a new execution primitive. Everything runs through `run_test_commands` /
  F039 `LocalToolRunner` / the F101-03 runtime-process path, under the existing
  sandbox settings (`store.get_require_sandbox()`).
- Not giving DEV/REVIEWER an `exec` tool. Execution stays engine-driven (the
  F087-14 WS-3 rationale, `turn_controller.py:20-26`). Roles consume gate
  *output*; they never dispatch a command.
- **Not making the acceptance gate a per-PR merge blocker.** A bootstrapped
  command is a project-level integration signal, not a branch-level one — see
  Item 1's scoping rule. The delivery gate remains the hard gate.
- Not a browser-automation framework. Headless screenshots are
  [Spec 14](SPEC-14-grounded-reviewer.md) Item 6.

---

## Item 1 — Gate bootstrap: acquire a gate, without arming the merge gate

**Design.** A new module `coding/gate_bootstrap.py`, called once at run start
(`CodingRunner.run`, next to the seeding call at `runner.py:4285-4288`) and again
after a merge advances master. Idempotent, read-mostly, fail-open.

**Δ review — the scoping rule is load-bearing.** `_set_mergeable_if_ready`
(`runner.py:2577-2601`) satisfies the tests gate iff `tests_passed is True or not
store.get_test_commands()`. Registering *any* command therefore instantly
requires a real tester-green verdict on **every** PR — and the tester runs the
suite against the **branch** worktree (`task_root(pr["task_id"],
branch=…)`, `runner.py:3549-3556`), where a whole-project acceptance script fails
by construction on a single-module branch. Bootstrap-as-drafted would have
stopped every merge, frozen `foundation_status` forever, and wedged the run
harder than the livelock this batch exists to kill.

So bootstrapped commands are registered with an explicit **scope**:

- `set_test_commands` gains a per-command `scope: "unit" | "acceptance"`
  (validated like the rest of the spec, `ledger.py:1235-1260`; absent →
  `"unit"`, so every existing operator-registered command keeps today's exact
  meaning and the merge gate is unchanged for them);
- `_set_mergeable_if_ready` and the tester-spawn condition (`runner.py:3448`)
  consider **unit-scoped commands only**;
- the in-loop gate (Item 2) and `delivery_review` (`runner.py:3984-4010`) run
  **all** commands.

Bootstrap registers `scope: "acceptance"` exclusively. An operator who wants a
command to block merges registers it themselves, as today.

Two acquisitions, both keyed on the merged master tree (`workspace.root()`):

1. **Runtime profile.** If `list_profiles()` is empty, call
   `runtime.detect(workspace.root(), project_id=...)` and `upsert_profile`
   **every** proposal, not just the primary. **Δ review:** `detect` tries
   `_detect_node` before `_detect_static` and passes `has_primary` down
   (`runtime.py:1306-1329`), so gravity-golf's jsdom-only `package.json` makes an
   `npm`-shaped profile the primary and the correct `python -m http.server`
   static profile secondary. Registering all proposals and letting
   `runtime_resolve`'s grounded-or-refuse rule (`runtime_resolve.py:507-512`)
   discard the one whose `start` entrypoint/script does not exist is both simpler
   and more honest than second-guessing the detector's ordering.
2. **Acceptance command**, registered only if *provably runnable on master*:
   - a team-authored test file the ecosystem runs directly (`test/*.test.js` with
     `node`; `tests/` with `pytest`);
   - a `package.json` `scripts.test` whose entrypoint exists;
   - else nothing.

   **Δ review — smoke-validate before registering.** "Never invent a command
   whose entrypoint is absent" is not sufficient: `node
   test/acceptance.test.js` also needs jsdom from an `npm install` that no engine
   path ever runs, so the command would fail `MODULE_NOT_FOUND` on every tree
   forever. Bootstrap therefore **runs the candidate once** on master via
   `run_test_commands` and registers it only if it *executed* — a non-zero exit
   from real test failures registers (that is the signal we want); a
   missing-interpreter / missing-module / immediate-crash result does not, and
   records `choice="gate_bootstrap_refused"` with the reason. A gate that can
   never pass is not a gate.

Record `choice="gate_bootstrapped"` naming what was registered and from which
signal.

**Acceptance.** A greenfield JS project whose master carries `index.html` +
a runnable `test/acceptance.test.js` ends up with runtime profiles and one
`acceptance`-scoped command, with no operator action — **and its PRs still merge
on review approval alone** (the scope rule). A candidate command that cannot
execute on master is refused with a recorded reason. Re-running bootstrap never
duplicates or overwrites an operator-configured entry. An existing registry with
no `scope` field behaves exactly as today (merge gate armed).

## Item 2 — Run the gate in-loop, on the merged tree, off the merge turn

**Design.** Factor the deterministic executor out of `delivery_review`
(`runner.py:3984-4010`) into one shared helper — it already does precisely the
right thing — and call it from a new scheduled point:

```
_run_gate(store, workspace, *, head, task_id, should_cancel) -> TestRunSession | None
```

**Δ review — do not run it inside the merge turn.** The concurrent loop defers a
`Merge` until every in-flight `Assign` drains and starts no new work while a
merge is in flight (`autonomy.py:1619-1637`). Putting the suite inside the merge
success block stops the entire team for its wall time on every gate-relevant
merge — handing back exactly the throughput
[Spec 13](SPEC-13-foundation-gate-buildless-web.md) exists to unlock, and with
`gate_min_merge_interval` defaulting to 1 the default configuration would be the
worst case.

Instead the merge block **marks master dirty** (`run_state.gate_dirty = True`,
plus the head) when the merged PR's `changed_paths` — already captured pre-merge
at `runner.py:3106-3112` for F159 — intersect the gate-relevant set. The loop
then dispatches the gate as its own mechanical action at the next quiescent
point, outside the merge critical section, in the same family as the existing
mechanical actions. A docs-only merge never marks dirty.

Cadence: `gate_min_merge_interval` (policy, default **3** — Δ review, was 1) so a
burst of merges coalesces into one gate run.

Fully guarded: an exception records a decision and clears the dirty flag; it
never fails a merge or a turn.

**Acceptance.** A merge touching `src/*.js` sets the dirty flag; the next
quiescent iteration produces a `test_run` bound to that head. A docs-only merge
produces none. Three merges inside the interval produce one gate run, not three.
The merge turn's wall time is unchanged (assert no `run_test_commands` call
occurs within it). Cancelling stops promptly (`testing.py:168-177`) and fails
closed.

## Item 3 — Verbatim gate output into dev, reviewer, and tester prompts

**Design.** A shared helper — **Δ review: placed in a module that does not import
`runner`, so [Spec 15](SPEC-15-capability-aware-planning.md) and
[Spec 17](SPEC-17-prompt-tool-catalog-coherence.md) can consume it from a
separate branch without a cross-branch dependency** (see the batch plan's prep
PR): `coding/gate_state.py` with

```
def gate_available(store) -> bool           # today's evidence._tests_required
def latest_gate_run(store) -> dict | None   # newest list_test_runs() record
def latest_gate_text(store, *, cap) -> str  # rendered block
```

`latest_gate_text` renders command ids, status, exit code, and the **verbatim**
`stdout_preview` / `stderr_preview`, stating the head it was produced against.
Emitted as a `PromptSegment("gate_output", …)`:

- `_dev_prompt_segments` (`runner.py:1561-1620`), after `repo_snapshot`;
- `_review_pr_prompt_segments` (`runner.py:1713-1775`), after `pr_diff`;
- `_test_prompt_segments` (`runner.py:2077-2116`).

Labeled as **observed output, not instructions**. The segment is **absent** (not
empty) when there is no gate run, so goldens for gate-less projects stay
byte-identical; `test_prompt_segments_golden.py` really does byte-lock all four
prompts, so the break is deliberate and scoped.

**Segment-ownership contract (Δ review).** `gate_output` segments are owned by
this spec (Engineer A); `tool_guidance` segments are owned by
[Spec 17](SPEC-17-prompt-tool-catalog-coherence.md) (Engineer B). The batch plan
fixes the segment order both sides code against.

**Acceptance.** With a failing gate, the next dev prompt contains the failing
command's real stderr and the head it came from; with no gate run, the prompt is
byte-identical to today.

## Item 4 — Head-bound completion verification *(already exists — assert it)*

**Δ review — the original Item 4 was redundant and would have made things
worse.** It claimed the done-claim chokepoint consults delivery evidence. It does
not: the PM `done=true` branch (`runner.py:3053-3070`) consults only
`completion.pending_completion_work`. The real integrated-head gate is
`delivery_review` (`runner.py:3859-4090`), which already runs every registered
command against `workspace.root()` bound to `head`, requires `tests_passed` for
`passed`, and **files a `"fix delivery tests"` task** (`runner.py:4062`) so the
team iterates.

Adding a second `gate_green_for_head` refusal at the PM plan branch would return
`completion_refused` with **no work filed**; `completion_refused_limit` (default
2) then stops the run `completion_blocked` — strictly worse than today.

**Design.** No new predicate. Once Item 1 populates the registry, the requirement
"`project_done` requires a green gate at the delivered head" is met by the
existing path. This item's work is therefore:

- a **test** asserting that a project with a bootstrapped acceptance command
  reaches `run_test_commands` inside `delivery_review` and that a red result
  blocks `passed` and files the fix task;
- documentation of the guarantee (it was previously only implicit);
- confirmation that the acceptance-scope rule from Item 1 does **not** exclude
  the command here — delivery runs everything.

**Acceptance.** A project with a bootstrapped acceptance command and a red gate
does not complete, and gets a fix task carrying verbatim stderr. With a green
gate it completes. A project with nothing to run is unaffected
(`_tests_required`, `evidence.py:115-127`).

## Item 5 — Runtime-only projects get a real validation arm

**Δ review — the original Item 5 was unimplementable as written.** It said
relaxing the tester-spawn condition would give a runtime-only project a tester
turn "which drives `run_runtime_test`". The TESTER branch (`runner.py:3487-3588`)
calls **only** `run_test_commands`; `run_runtime_test` (`testing.py:314`) has one
caller in the tree, the `POST .../runtime/{profile_id}/test` route
(`routes/coding.py:3582-3590`). Relaxing the condition alone is actively harmful:
with an empty unit registry the tester either takes the `not_applicable` escape
(marking `tests_passed=True` plus a "tests skipped" alert every round) or calls
`run_test_commands` with empty `command_ids`, which fails closed
(`testing.py:208-212`) and files a bogus `fix tests:` DEV task.

**Design.** Add a real runtime arm to the in-loop gate rather than to the tester:
when the project has a runnable `managed_local` profile, `_run_gate` also drives
`run_runtime_test` via `RuntimeProcessManager` — the same machinery F146 Slice C
already builds for delivery (`runner.py:1853-1900`) — and records the result
alongside the command results. The tester-spawn condition (`runner.py:3448`) is
**left alone**.

This is what catches the black screen: a runtime that starts, passes its health
probe, and renders nothing is still one step from a screenshot
([Spec 14](SPEC-14-grounded-reviewer.md) Item 6), but a runtime that *crashes on
start* is caught here, in-loop, instead of at delivery.

**Acceptance.** A project with a runnable profile and no commands produces an
in-loop gate record from the runtime probe. The tester-spawn condition and the
`not_applicable` path are unchanged (regression lock).

---

## Implementation notes

- **New:** `coding/gate_bootstrap.py` (Item 1) and `coding/gate_state.py`
  (Item 3). Neither imports `runner` — `runner` imports `.topology`/`.schemas` at
  `runner.py:43-45`, so the F159 `coding/paths.py` discipline applies.
- **`ledger.py`** — `scope` in `set_test_commands`'s validation (`:1235-1260`);
  additive, absent → `"unit"`.
- **`runner.py`** — factor the deterministic executor out of `delivery_review`
  (`:3984-4010`) for Item 2; dirty-flag marking in the merge success block
  (`:3106-3175`); gate dispatch at the loop's quiescent point; `gate_output`
  segments (`:1561`, `:1713`, `:2077`); scope filter in `_set_mergeable_if_ready`
  (`:2577-2601`) and the tester-spawn condition (`:3448`); bootstrap at run start
  (`:4285-4288`).
- **`autonomy.py`** — `gate_bootstrap` (bool, True) and `gate_min_merge_interval`
  (int, 3) on `CodingAutonomyPolicy` (`:63`), round-tripped in `policy_to_dict` /
  `policy_from_dict` (`:157/183`). **Per the batch plan these land in the shared
  prep PR**, not on this branch.
- **No new storage.** Test runs use `record_test_run` (`ledger.py:1273`);
  profiles use `upsert_profile` (`runtime.py:303`); the dirty flag is
  `run_state` (`ledger.py:1350/1371`, no migration).
- **No CLI change required** — `errorta gate` (Spec 03), `errorta test-runs`,
  `errorta decisions` already surface everything.

## Edge cases

- **A project with no runnable signal** (a pure library): bootstrap registers
  nothing, the gate no-ops, delivery is unaffected. Byte-identical to today.
- **An operator-registered command with no `scope`**: treated as `"unit"` —
  merge-blocking, exactly as today. The relaxation is opt-in by construction.
- **A gate command that hangs**: bounded by the registry's per-command timeout,
  validated at `set_test_commands`.
- **A dev breaks the gate script itself** (the gravity-golf case — the harness
  *was* the bug): output is verbatim, so the failure names the harness file and
  the dev can fix it. The gate is part of the deliverable and is allowed to be
  wrong; Item 1's smoke check only refuses a command that never executed at all.
- **A flaky gate**: `_gate_fingerprint`'s score is the count of passing commands
  and Spec 04 resets only on a strict increase, so flapping still converges
  toward the stall stop.
- **Sandbox unavailable with `require_sandbox`**: `run_test_commands` fails
  closed (`sandbox_unavailable`, `testing.py:184-191`) → a red gate, not a silent
  skip. Must not be softened.
- **Concurrent merges**: integration is already serial
  (`autonomy.py:1619-1637`); the gate runs outside that section against whatever
  head is current, and records the head it used.

## Testing

- **Item 1**: bootstrap on a fixture tree registers acceptance-scoped commands +
  all runtime proposals; a candidate that cannot execute is refused with a
  decision; a second call is a no-op; an operator registry is never overwritten.
  **Merge-gate lock:** with only acceptance-scoped commands registered, a
  reviewer-approved PR still becomes mergeable and no tester task is spawned —
  this is the regression that would otherwise wedge every run.
- **Item 2**: a gate-relevant merge sets the dirty flag and no
  `run_test_commands` call happens inside the merge turn; the next quiescent
  iteration produces a head-bound `test_run`; a docs-only merge produces none;
  three merges inside the interval coalesce to one run; `should_cancel` fails
  closed.
- **Item 3**: prompt-segment goldens — segment absent with no gate run
  (existing goldens unchanged), verbatim stderr + head present when there is one.
- **Item 4**: a bootstrapped command reaches `run_test_commands` in
  `delivery_review`; a red result blocks `passed` and files the fix task; a
  green one completes.
- **Item 5**: a runtime-only project produces a gate record from the runtime
  probe; the tester-spawn condition and `not_applicable` path are unchanged.
- **Integration (the repro)**: a gravity-golf-shaped fixture — buildless web,
  self-sabotaging acceptance script — produces a red in-loop gate, carries the
  verbatim failure to the next dev, and either goes green or trips Spec 04's
  `gate_not_improving`. Under today's code the same fixture merges everything
  green with zero test runs; that assertion is the regression lock.
- Full coding suite + `ruff`.

## Documentation

- `docs/CLI.md`: the acceptance gate runs during a run; `errorta gate` reflects
  in-loop runs; the `scope` field and what it means for merges.
- `docs/coding/PM_REFERENCE.md`: `gate_bootstrap` / `gate_min_merge_interval`,
  the `gate_bootstrapped` / `gate_bootstrap_refused` decisions, acceptance vs
  unit scope, and the (pre-existing, now documented) rule that `done` requires a
  green delivery gate.

## Out of scope / follow-ups

- Per-branch acceptance runs. v1 is master-scoped by design — see Item 1.
- Browser-level assertions. [Spec 14](SPEC-14-grounded-reviewer.md) Item 6
  attaches a screenshot; real visual assertions are later.
- A dependency-install step (`npm install`) before a bootstrapped command — the
  reason Item 1 smoke-checks instead. Worth doing, and it needs its own sandbox
  and network-policy decision.
- Auto-authoring an acceptance test when the team wrote none. Inventing a gate is
  how gates become fiction.
