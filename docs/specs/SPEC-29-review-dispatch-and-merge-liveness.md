# Spec 29 — Review dispatch and merge liveness under the strict file partition

**Source:** Run 4 (`gravity-golf`, 2026-07-30) — the first autonomous run whose
recovery machinery (SPEC-22–28) all *ran* and none of it could touch the failure,
because the failure lives one layer below every detector, ladder, and prompt: in
dispatch admission. See `scratchpad/run4-evidence.md` and the verified root cause.
**Target version:** v0.1 (engine — `python/errorta_council/coding/topology.py`)
**Depends on:** [SPEC-26](SPEC-26-role-capability-closure.md) (the seated,
capable REVIEWER whose turns never dispatched) · [SPEC-27](SPEC-27-convergence-as-control.md)
(whose `planning_churn` ladder engaged, force-lifted, and could not help)
**Relates to:** [F159](F159-hot-file-serialization.md) (the sibling merge-scoped
gate, same role-blindness) · known-open #3, the missing reviewer-less merge path
([ROADMAP-autonomy.md](ROADMAP-autonomy.md):193-194)
**Status:** PARTIAL (verified 2026-08-06 against the code, not the commit log) — ONLY the GL05 skip predicate; the claim guard, the hot-file exemption, AND the entire test deliverable are absent
**Landed evidence:** landed: topology.py:585 `if partition_on and role == DEV and tp and paths_intersect(tp, owned_unavailable): continue`
**NOT landed:** (a) Item 1 CLAIM half — topology.py:611 is still `if partition_on and tp: claimed |= tp` with NO role guard, so a dispatched REVIEWER/TESTER claims paths it never writes and blocks a later DEV in the SAME batch. (b) Item 3 (F159 hot gate) — topology.py:563 is role-blind too, so a merge-scoped hot hold can deadlock a review one conflict later. (c) Item 2 IS THE STATED DELIVERABLE ("the test is the spec") and NO SPEC-29 test exists anywhere: grep over python/ returns only the source comment at topology.py:573. The one landed predicate is itself untested.
**Tests:** NONE. No test_spec29_*.py; test_gl05_parallelism.py has no dispatch-level reviewer-exemption test.
**Owner:** wiggins-j

---

## Problem

Run 4 delivered a **big square**: `game.js` paints a background gradient and its
`update()` is empty — no ball, no hole, no gravity well, no levels, no input. The
six modules that *are* the game were written by the devs, opened as PRs, and never
merged. The run stopped `planning_churn` at iteration 28 after 10m5s and 25 model
calls.

This is not a run that ran out of work, capability, or ideas. At the moment it
stopped, every precondition for progress was present at once:

- a **seated, capable REVIEWER** — `run_state.json` `role_closure` reports the
  reviewer capable and seated; `reviewer_repo_read=True`;
- **six open PRs awaiting review** — `prs.json`: `pr-a2a3e56b26d7` (ball.js),
  `pr-9b6f1cdeb43c` (gravity-well.js), `pr-a96940fa65c9` (hole.test.js),
  `pr-ac261f5ac1ff` (input.test.js), `pr-bc0a68fbecbe` (levels.js/levels.test.js),
  `pr-6a084df4f678` (game.js integration) — **all six `reviewer_approved: null`**;
- **six ready review tasks** — `backlog.jsonl`: `t-d84b7907e5ea`, `t-8eb2b30a05cc`,
  `t-4eeb0940dbdc`, `t-0dde84d036a2`, `t-3b4d5391c7c2`, `t-545de365be44`, all
  `role=reviewer`, `state=todo`, each `depends_on` a **done** dev task
  (`unmet=0` → dispatchable).

Capable reviewer, open PRs, ready review tasks — and **zero reviews dispatched**
after turn 4. The gradient is what a run looks like when review is structurally
unreachable.

### The turn trace (`turns.jsonl`, 26 turns)

```
turn  0        pm  planned
turns 1–2      dev pr_opened   →  reviewer pr_reviewed     ┐ foundation
turns 3–4      dev pr_opened   →  reviewer pr_reviewed     ┘ (sequential loop)
turns 5–10     dev pr_opened ×6                            ← the six modules
turns 11–25    pm  planned ×15  (0 dev, 0 reviewer)        ← the wedge
```

The reviewer was dispatched **exactly twice** — turns 2 and 4, on the two
foundation PRs, which merged (`prs.json`: `pr-08646abcae64` index.html,
`pr-e7d0ed3426bd` game.js, both `reviewer_approved: True`, both `merged`). After
the foundation merged the reviewer was **never dispatched again**, while six PRs
sat open. Fifteen consecutive PM plan turns followed, and `planning_churn` tripped
the stop detector at iteration 28.

### The `planning_churn` stop is a red herring

`run_state.json` at stop: `stop_reason=planning_churn`, `iterations=28`,
`foundation_status=merged`, `integration_only=false`, `planning_clamped=false`,
`narrows_used=1`, `gate_pending_merges=2`, `last_words.count=2`. The 15 PM plan
turns are real, but they are the **symptom of a dispatch deadlock**, not a PM that
ran out of plan. The PM kept planning because the dispatcher kept telling it there
was nothing else to run — while six reviewable PRs sat one gate away from merge.

---

## The root cause

**The GL05 strict-file-partition gate in `plan_next_batch`
(`topology.py:571-574`) has no non-writer-role exemption, so it permanently
serializes every REVIEWER "review PR:" task behind the still-open DEV PR that task
must approve — a deterministic deadlock.** The full chain, each link confirmed
against code and against the stopped run's ledger:

1. **Turns 0–4 ran under the sequential loop.** With the foundation still pending,
   `runtime_cap` returns `1` (`autonomy.py:762`, `foundation_status=="pending"`),
   the loop selects `decide_next`, which has **no partition** and cleanly
   dispatched the reviewer for the two foundation PRs (turns 2, 4). They merged.

2. **After the foundation merged, the run self-healed UP to the concurrent loop.**
   `foundation_status=merged` and two merged PRs make `runtime_cap` return the
   static base `= effective_parallelism = 4` non-PM members
   (`autonomy.py:776-816`, `746-753`; `_feature_merges=2>1` so it skips the
   ease-in and returns `base`; `max_parallel_workers=null`). `base>1`, so the
   sequential loop hands up to `_run_concurrent_loop` (`autonomy.py:3751`) and —
   `runtime_cap` being monotone here — stays there. From ~turn 5 on, dispatch went
   through `plan_next_batch`, **not** `decide_next`. Turns 5–10 fanning out six dev
   PRs across the worker members confirm the concurrent loop was live.

3. **`strict_file_partition=true` turns the partition on.** `autonomy.json`:
   `strict_file_partition=true`, `max_parallel_workers=null`. The concurrent loop
   therefore passes `owned_by_task=inflight_owned_paths_by_task(ledger)` into
   `plan_next_batch` (`autonomy.py:4138-4141`), which sets `partition_on=True`
   (`topology.py:492`).

4. **The six done dev tasks still own their files, because their PRs are open.**
   `inflight_owned_paths_by_task` (`autonomy.py:695-703`) treats a task as a live
   owner while it has a PR whose `status` is not in
   `(merged, superseded, abandoned, closed)` (`autonomy.py:654`) — all six module
   PRs are `open`. Each owner's path set is `task_touched_paths(task) ∪ observed
   changed_paths` (`autonomy.py:699`). Reproduced against the ledger:
   `{ball.js}`, `{hole.js, hole.test.js}`, `{gravity-well.js}`,
   `{input.js, input.test.js}`, `{levels.js, levels.test.js}`, `{game.js}`.

5. **The reviews are ready and reach the gate.** `ledger.next_tasks('reviewer', …)`
   returns all six (todo, dep dev task done). Each review task's title —
   *"review PR: Create ball.js module…"* — yields a title-inferred touched-path
   `{ball.js}` via `_paths.task_touched_paths` / `declared_target_paths`
   (`paths.py:51-64`, `TARGET_PATH_RE` `:20-23`).

6. **GL05 skips every one of them.** The gate is
   `if partition_on and tp and paths_intersect(tp, owned_unavailable): continue`
   (`topology.py:573`). Because the review task's `task_id` differs from the owning
   dev task's `task_id`, the module path lands in `owned_unavailable`
   (`_unavailable_for`, `topology.py:571`), `tp` intersects it, and the review is
   skipped **every tick**. Re-running the exact gate over the real
   `prs.json`/`backlog.jsonl` skips all six.

7. **No review ⇒ no approval ⇒ no merge ⇒ the only remaining action is a PM plan.**
   A PR reaches `status="mergeable"` **only** through `_apply_merge_gate`
   (`runner.py:1708-1743`), the sole writer of `mergeable`, gated on
   `reviewer_approved is True` (`:1741`). No review runs → `reviewer_approved`
   stays `null` on all six → no PR is ever mergeable → the merge-first branch
   never fires. With every review skipped and every module dev task terminal,
   `worker_assigned` stays `False`, so the terminal branch
   `if pm_ids and not worker_assigned: batch.append(Plan(pm-1))`
   (`topology.py:610-611`) fires — one PM plan turn per tick, for 15 ticks, until
   `planning_churn`.

**The deadlock, in one line:** the review is blocked by file ownership; the
ownership releases only on merge (`autonomy.py:654`); the merge needs the review
(`runner.py:1741`). Review waits for the PR, the PR waits for the review, and GL05
has no force-lift for this interaction, so it is an **unbounded wedge**.

### The mechanism precision that makes the deadlock total

For four of the six PRs the blocking path is the PR's observed `changed_paths`
(ball, gravity-well, levels, game). For **hole and input it is not**: those PRs
changed `hole.test.js` / `input.test.js`, which do **not** match the review
titles' `hole.js` / `input.js` under `paths_intersect` (`paths.py:67-81`, whose
basename fallback distinguishes `hole.js` from `hole.test.js`). A
changed-paths-only ownership model would fail those two **open** — the reviewer
would have reviewed at least them, and the trace would not show 15 pure PM turns.
What blocks them is the **union at `autonomy.py:699`**: the dev task *title*
"Create hole.js module" / "Create input.js module" injects `hole.js` / `input.js`
into ownership. So all six reviews are blocked — but for hole and input the block
is title-driven, not changed-paths-driven. This is the difference between four-of-six
and six-of-six, and it is why the wedge is complete rather than partial.

### The delivered artifact

Only the two foundation PRs merged (index.html + a scaffold `game.js` with an
empty `update()`). The six real module PRs — ball, hole, gravity-well, input,
levels, integration — were written and never merged. The gradient is exactly the
state the deadlock guarantees.

---

## Why the existing machinery didn't catch it

Every recovery mechanism SPEC-22–28 installed **ran on this run**, and each was
structurally unable to reach a dispatch-admission deadlock:

- **SPEC-27's `planning_churn` ladder engaged and force-lifted.**
  `narrow_clamp_planning_engaged` fired, then `narrow_clamp_planning_force_lifted`
  when it did not release in the drain window (`run_state` `narrows_used=1`,
  `planning_clamped=false` at stop). `CLAMP_PLANNING` only **widens the ready-task
  over-fetch** (`topology.py:534`, the `+32` window) so a head-of-line-blocked
  role finds the task *behind* its gated heads. But behind every reviewer head is
  another GL05-owned path; widening the window changes *how many* review tasks are
  fetched, never whether the `topology.py:573` skip lets one through. The ladder's
  own remedy is upstream of nothing here.

- **SPEC-23's last word was spent twice, on the same answer.** Two
  `last_word_requested` → both `last_word_accepted`; `last_word_limit=2` exhausted.
  Both times the PM proposed *"Prioritize integration over pending reviews"*
  (`run_state.last_words`, `detector=no_progress`). It could not clear the wedge:
  the integration PR it wanted to run (`t-1f0adc8bdfa0`, game.js) is **itself**
  GL05-blocked by its own open PR. The PM tried to route around the stall and the
  same gate closed on the detour.

- **SPEC-26 correctly unseated the TESTER**, which makes the merge gate vacuously
  tests-green (`runner.py:1729-1731`) — so **review approval alone would have
  sufficed to merge every PR**. This confirms, rather than complicates, the
  diagnosis: tests were not the missing link; reviews were.

- **The 17 `duplicate_task_rejected` decisions** are the churning PM re-planning
  reviews/UI/acceptance tasks that already existed; `task_dedupe` correctly no-op'd
  the redundant creates. Dedupe governs **creation**, never **dispatch** — designed
  behaviour, not a contributor.

- **The greenfield title-as-path heuristic is a necessary enabler, not the bug.**
  Review titles name their module file, so the reviews are *not* path-silent and
  GL05's fail-open-on-silence branch (`topology.py:569-570`, empty `tp`) never
  protects them. Fixing path inference is the wrong altitude — a *reviewer* that
  named no file would fall through by accident, not by design. The role is the
  right axis.

---

## Why this is distinct from runs 1–3

The 07-24 batch failed three ways, each a *convergence* pathology — work churning,
conflicts thrashing, a run spinning past its budget — the shapes SPEC-27 and F159
were written against. Run 4 is a different animal:

1. **It presents as healthy.** Nothing is churning at the file level, nothing is
   conflicting, no member is unproductive. Capable reviewer, open PRs, ready tasks
   coexist. The prior runs looked sick; this one looks fine and produces nothing.

2. **The stop reason misattributes it.** Runs 1–3 stopped for the pathology they
   had. Run 4 stopped `planning_churn` for a pathology it *doesn't* have — the PM
   plan turns are downstream of a dispatch deadlock, not a planning problem.

3. **The defect is one layer below everything SPEC-22–28 touches.** Those specs
   act on detectors, ladders, prompts, and role seating — all of which assume the
   dispatcher will run a *ready* task when one exists. Run 4 is the first failure
   where a ready task assigned to a seated capable member is **silently refused
   admission** by a partition gate, so every layer above it is arguing about a run
   that has already been wedged. (Runs 1–3 ledgers were not re-read for this spec;
   the distinction is drawn from their documented convergence shapes in the
   SPEC-22–28 batch plan — treat the run-1–3 specifics as *unverified* here.)

The genuine relationship to F159 is **bounding, not role**. It is tempting to say
F159 "exempts non-writer roles" and GL05 does not — that is inaccurate. F159's
freeze-intersect gate (`topology.py:544`) blocks a reviewer whose `tp` hits a
frozen path exactly as GL05 does; the `topology.py:555-560` exemption is only the
narrower **DEV-only prose-silent teeth** (`if role == DEV and not tp`). Both
intersect gates are role-blind. The real, verifiable difference is that **F159's
freeze force-lifts** via `hot_file_freeze_stall_limit` and so self-heals, whereas
**GL05 ownership releases only on merge** (`autonomy.py:654`) — which a
review-gated PR can never reach. GL05 has no per-interaction force-lift, so it is
the one gate that can wedge forever. That unbounded-wedge property is the correct
distinction to fix against.

---

## Goals

- **A REVIEWER or TESTER task is never refused dispatch by the GL05 partition
  gate.** A non-writer does not *write* the owned file; it reads and approves the
  PR that owns it. Serializing it behind that PR is precisely the deadlock.
- Keep GL05 doing its one real job — **stop two WRITERS fanning out onto the same
  file before the first lands** — completely unchanged.
- Give F159's merge-scoped hot-file gate the **same** non-writer exemption, so the
  identical deadlock cannot resurface the moment a file goes hot.
- Change nothing when `strict_file_partition` is off, and nothing about any stop
  reason or exit code.

## Non-goals

- **Not weakening the writer partition.** Two DEV tasks touching one owned file
  are still serialized, exactly as today. This spec narrows *who* the gate applies
  to, not *what* it does to writers.
- **Not touching path inference.** `paths.py` and the title-as-path heuristic are
  unchanged; the fix is a role check, not a smarter path model.
- **Not adding a reviewer-less merge path** (known-open #3). Once reviews
  dispatch, the existing reviewer-AND-tests gate closes normally. #3 remains the
  deeper follow-up for the *ungrounded-reviewer* false-rejection case, out of
  scope here.
- **Not adding a policy knob, a stop reason, a role, or a schema version.**

---

## Item 1 — the non-writer exemption on the GL05 gate (the fix)

**Design.** The GL05 skip at `topology.py:573` gains a writer-role predicate,
mirroring the DEV-only teeth F159 already carries at `topology.py:559`:

```python
owned_unavailable = _unavailable_for(
    task.task_id, owned, owner_map, claimed)
if partition_on and role == DEV and tp and _paths.paths_intersect(tp, owned_unavailable):
    continue
```

and the corresponding **claim** at `topology.py:598-599` is likewise gated on
`role == DEV`, so a dispatched reviewer never claims a path it does not write and
so never blocks a later DEV in the same batch:

```python
if partition_on and role == DEV and tp:
    claimed |= tp
```

`_WORKER_PRIORITY = (TESTER, REVIEWER, DEV)`, so this exempts exactly the two
non-writer roles. A REVIEWER/TESTER task now dispatches whenever a free member of
its role exists and its dependencies are met — which is the state run 4 was in
from turn 5 onward.

**Why this is the a-priori partition's purpose, fully served.** GL05 exists to
stop two writers racing onto one file from tick 0, before the first PR merges. A
non-writer role never opens a competing PR on that file; exempting it removes zero
protection against the collision GL05 was built for. This is the same reasoning
`topology.py:555-556` records for the freeze teeth — non-writer roles are exempt
so *"the owner's PR can still be reviewed + merged"* — applied to the gate that
was missing it.

**Acceptance.** Replaying run 4's ledger through `plan_next_batch` with
`strict_file_partition=true` yields a batch containing at least one
`Assign(role=REVIEWER)`, not a lone `Plan(PM)`. Two DEV tasks touching one owned
file are still serialized. With `strict_file_partition=false`, the batch is
byte-identical to today.

## Item 2 — the deadlock regression lock (the deliverable)

The fix is one predicate; **the test is the spec.** A deterministic scheduler
fixture reconstructs run 4's stop state — the minimum that reproduces the wedge:

- `foundation_status=merged`, ≥2 merged PRs, `max_parallel_workers=null` so
  `runtime_cap>1` and the concurrent loop is selected (`autonomy.py:3751`);
- `strict_file_partition=true` so `owned_by_task` is threaded and
  `partition_on=True` (`autonomy.py:4138-4141`, `topology.py:492`);
- N module dev tasks `state=done` with **open** PRs (live owners,
  `autonomy.py:654`), including at least one PR whose observed `changed_paths` is a
  `*.test.js` sibling of the review title's `*.js` — so the lock also pins the
  `autonomy.py:699` title-union path that makes hole/input block;
- N reviewer tasks `state=todo`, deps met, titles naming the module files.

**Assertions (all statically checkable against the batch and the ledger):**

1. **Pre-fix:** `plan_next_batch` returns `[Plan(PM)]` — the wedge reproduces
   *today*, on the current tree. This must fail-red before the Item-1 edit lands.
2. **Post-fix:** the batch contains `Assign(role=REVIEWER, …)` for every ready
   review whose only blocker was the owned path.
3. **Loop-level:** driven forward, reviews approve, `_apply_merge_gate`
   (`runner.py:1741`) flips each PR to `mergeable`, the merge-first branch drains
   them, and the run reaches `definition_of_done` (or a genuine blocker) — **not**
   `planning_churn`.

This is the "the test is the deliverable" pattern SPEC-25 S5 used for its
satisfiability invariant: a behavioural end-to-end test can pass by accident of
scheduling; a fixture that pins the exact stop state cannot.

## Item 3 — the same exemption for F159's merge-scoped hot-file gate

F159's hot-file hold (`topology.py:561-564`, fed by `hot_owned_paths_by_task`,
`autonomy.py:718-733`) is the sibling of GL05 and shares the same release
property: hot ownership also releases only on merge. It was **inert on run 4**
(a conflict-free greenfield run has no hot files, so `hot` is empty and the gate
never engages), which is why GL05 is the gate that actually fired. But the moment
a file conflicts twice, F159's gate would block that file's review exactly as GL05
blocked all six — the same deadlock, one conflict later.

**Design.** Gate the hot skip on `role == DEV` too:

```python
hot_unavailable = _unavailable_for(
    task.task_id, blocked, hot_owner_map, hot_claimed)
if role == DEV and hot_unavailable and _paths.paths_intersect(tp, hot_unavailable):
    continue
```

Note F159's freeze-intersect gate (`topology.py:543-544`) is deliberately left
alone: the freeze is bounded by `hot_file_freeze_stall_limit` and force-lifts, so
it cannot wedge — its role-blindness is safe. It is the two **merge-scoped,
release-on-merge-only** gates (GL05 at `:573`, F159 hot at `:563`) that need the
exemption, because they are the ones with no force-lift.

**Acceptance.** With a hot file owned by an open PR and a ready review task for
that PR, the review dispatches; two DEV tasks touching the hot file still
serialize. No hot files → dispatch byte-identical to today.

## Item 4 — boundedness

The fix **deletes** a `continue` for non-writer roles; it adds no
continue-enabling branch, no fan-out mechanism, no new dispatch. It is
strictly dispatch-narrowing-*removing*. The worst case is therefore trivial to
bound.

**The worst case, stated explicitly.** Let *C* = `runtime_cap` (=4 for this
four-member team, `autonomy.py:816`).

- Extra **model calls per iteration**: bounded above by the number of idle
  non-writer members that now get a turn — and strictly by the concurrent loop's
  hard ceiling `while model_in_flight < cap` (`autonomy.py:4156`). Total in-flight
  worker turns per iteration remain `≤ C`; the fix only **reclassifies** which of
  those `≤ C` turns are reviewer-vs-skipped. It never raises the ceiling.
- Extra **re-dispatch**: none. An exempted review task transitions `todo→doing` on
  assignment and drops out of `next_tasks`, so each is dispatched **at most once**.
  There is no path by which the exemption re-dispatches a task it already ran.
- Extra **iterations**: none from the fix itself. On the contrary, replacing PM
  plan turns with review turns *shortens* the run — the reviews it dispatches are
  turns the run should always have spent. In run 4's shape, six reviews clear in
  `⌈6/C⌉ = 2` fully-packed reviewer ticks, interleaved with the merges they
  unblock.
- **The forever-loop is unreachable by construction.** The fix introduces no new
  narrowing flag, no new release condition, no new counter. Every existing budget
  — `max_iterations`, model-call budget, `planning_churn` itself — is untouched
  and still dominates. A run that genuinely has no reviewable work still falls to
  the same PM-plan branch and stops with the same reason.

## Item 5 — backward compatibility

**Verified, not assumed:**

- **The escape hatch is the existing knob.** `strict_file_partition=false` makes
  the concurrent loop pass `owned_by_task=None` (`autonomy.py:4139-4141`), so
  `partition_on=False` (`topology.py:492`) and neither the GL05 skip nor its claim
  is evaluated. The fix lives entirely inside the `partition_on` branch, so with
  the partition off the trace is byte-identical to today — this is the "0-knob
  reproduces today" lock.
- **No stop-reason string or exit code changes.** No constant in
  `autonomy.py:40-55` is added, removed, or renamed; `FAILURE_STOP_REASONS` /
  `SUCCESS_STOP_REASONS` / `STOP_REASON_GLOSS` / `_TERMINAL_BAD` need zero edits;
  `classify_exit`'s fail-closed allowlist is untouched. A run that genuinely
  wedges still stops with today's reason and today's exit code. The drift lock
  `test_every_engine_stop_reason_is_triaged` passes unmodified.
- **Both loop chains.** GL05 lives only in `plan_next_batch` (the concurrent
  chain); `decide_next` (the sequential chain, `topology.py:265`) has no
  partition and dispatched reviews correctly in run 4 (turns 2, 4). The
  review-dispatch liveness invariant must hold in **both**: the sequential path
  unchanged and already-correct, the concurrent path fixed. A test asserts a
  review dispatches under `_run_sequential_loop` (`max_parallel_workers=1`, as it
  did) *and* under `_run_concurrent_loop` (`>1`, where it did not).
- **The writer partition is unchanged.** A per-`role` fixture pins that two DEV
  tasks touching one owned path still serialize under `strict_file_partition=true`
  — the a-priori partition's actual purpose, regression-locked so the exemption
  cannot be over-widened into disabling GL05.

---

## Regression locks worth stating up front

Write these before the fix; they are its contract with run 4.

1. **`strict_file_partition=false` reproduces today's trace byte-for-byte** — the
   fix is inert with the partition off.
2. **`strict_file_partition=true` with two DEV writers to one owned file still
   serializes them** — GL05's real job, preserved.
3. **The run-4 stop state reproduces the wedge on the current tree** (Item 2,
   assertion 1) and the fix breaks it (assertions 2–3). The lock must fail-red
   before the edit.
4. **Both loop chains dispatch reviews** — sequential unchanged, concurrent fixed.
5. **No stop-reason string, no exit code, no policy field changes.**
6. **The hot-file sibling gate (Item 3) exempts non-writers**, so the deadlock
   cannot re-enter one conflict later.

---

## Definition of done

An acceptance test can assert every clause:

1. Replaying run 4's ledger (`~/.errorta/council/coding-projects/gravity-golf`)
   through `plan_next_batch` with `strict_file_partition=true` produces a batch
   with ≥1 `Assign(role=REVIEWER)` — **not** `[Plan(PM)]`.
2. Driven to completion from that state, all six module PRs receive a review, reach
   `mergeable` via `runner.py:1741`, and merge; the run reaches
   `definition_of_done` (or a real, non-review blocker), not `planning_churn`.
3. The delivered `game.js` `update()` is non-empty — the modules are integrated,
   not a bare gradient. (End-to-end tier; the mechanical locks above are the
   unit-level proof.)
4. With `strict_file_partition=false`, the batch and stop trace are byte-identical
   to pre-spec.
5. Two DEV writers to one owned file remain serialized under the strict partition.
6. `test_every_engine_stop_reason_is_triaged` and the full coding suite
   (`test_dispatch_wedge.py`, `test_planning_churn.py`, F159 hot-file tests) pass
   with no edits to the stop-reason machinery, plus `ruff`.

---

## Out of scope / follow-ups

- **Known-open #3 — a reviewer-less merge path.** This spec makes the review
  *dispatch*; it does not remove review from the merge gate. The ungrounded
  reviewer's 26–92% false-rejection remains a separate risk that #3
  ([ROADMAP-autonomy.md](ROADMAP-autonomy.md):193-194) addresses. The two compose:
  once reviews dispatch, a reviewer-less merge path would be the belt-and-suspenders
  for the case where the seated reviewer is *present but wrong*, not *never asked*.
- **A GL05 force-lift.** The deeper structural cure would give GL05 the same
  bounded force-lift F159's freeze has (`hot_file_freeze_stall_limit`), so even a
  writer-writer partition cannot wedge indefinitely. The role exemption fixes the
  observed deadlock at lower cost and risk; a general force-lift is a larger change
  with its own boundedness proof, deferred.
- **Detecting the dispatch deadlock as a first-class stop reason.** Run 4 stopped
  `planning_churn`, which misnames it. A `review_starved` / `dispatch_deadlock`
  detector that names *"ready review tasks exist, capable reviewer seated, zero
  dispatched"* would make the next instance legible even before this fix. Its own
  spec — this one removes the cause; that one would diagnose any residue.
- **Runs 1–3 re-verification.** The "distinct from runs 1–3" claim leans on the
  documented convergence shapes in the batch plan; a ledger-grounded confirmation
  is a cheap follow-up, left unverified here.
