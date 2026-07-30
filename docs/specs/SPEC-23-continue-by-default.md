# Spec 23 — Continue-by-default (the last-word turn)

**Source:** the two 2026-07-26 gravity-golf runs, and
[`ROADMAP-autonomy.md`](ROADMAP-autonomy.md) Phase 1 (this is the keystone item).
**Target version:** v0.1 (engine — `errorta_council/coding/autonomy.py`)
**Status:** implemented
**Owner:** wiggins-j

---

## Problem

> An autonomous run should end for exactly two reasons: **the work is done**, or
> **a human/budget said stop.** Every other condition is a signal to *change
> strategy*, not to terminate — and the component best placed to choose the new
> strategy is the PM, which today is never asked.

Two runs on 2026-07-26 ended for neither of those reasons.

**Run #1** stopped `gate_not_improving` at iteration 22 with **6 PRs opened, 6
approved, 6 merged, 0 revises, 0 context requests** — the healthiest trace this
project has produced. The gate's only signal was a runtime probe that *passed*,
and `_account_gate_stall` requires a **strict** score increase to reset its
window (`autonomy.py:1094`): a green gate sits at its maximum forever and can
never strictly increase, so a passing gate read as a stalled one. Spec 21 fixed
that specific bug — `_gate_has_failure` now gates the trip (`autonomy.py:1112`,
docstring at `:1101-1111`). The **class** survives untouched: a detector,
computing between turns from the ledger alone, unilaterally ended a run that was
working, and nothing in the system was in a position to say otherwise.

**Run #2** stopped `no_progress` with 2 PRs still open, because the PM was
locked out of a legal turn shape. It had diagnosed a duplicate task correctly,
tried four times to express "prune these duplicates, add nothing, not done", and
was rejected by the schema each time — and each rejected attempt counted as
idleness, so *trying to fix the problem accelerated its own termination*. Spec
21 fixed that too (`schemas.py:133-150`: a not-done turn with decisions and no
tasks is now legal; `PMDecision._accept_decision_as_title` at `schemas.py:102-122`
accepts the synonym key that killed 3 of the 4 retries). Again: the specific bug
is gone, the shape is not.

The shape is an **asymmetry**. The engine declares **16** stop reasons
(`autonomy.py:40-55`) and roughly **two** recovery mechanisms: the F127
escalate-up ladder (`_handle_unproductive`, `autonomy.py:1513-1664`) and GL04's
convergence clamp (`_account_convergence_clamp`, `autonomy.py:1150-1200`, which
narrows rather than kills and is the model this spec generalises). Twelve of
those reasons are classed FAILURE by the CLI (`runstream.py:67-72`). Every one
of them terminates the process, and none of them asks anybody first.

That asymmetry was earned honestly — the 2026-07-24 run looped for 3h20m, so the
batch that followed added stop conditions. We over-corrected. **For an unattended
product, halting is not the safe default; it is the failure.**

The research is unambiguous on the fix. The failure report's strongest *positive*
finding is that an Expert reviewer grounded in the failure taxonomy, **intervening
at decision points**, solved **22.2% of previously-unsolvable instances** —
"replanning beats revising when deadlocked"
(`docs/coding/multi-agent-pipeline-failure-report.md:114`, §3 Pathology 4,
"Strategic oversight over more iteration"; the same section's §4 crossover
analysis at `:134` notes that where multi-agent helps at all it is via
*structured oversight*, not symmetric collaboration). We built every mechanism in
that section's list — hard caps (`revise_chain_limit`), progress detection
(`_account_convergence`), anti-oscillation (GL04's clamp), health metrics on
superseded work — and skipped the one with the measured upside.

## Why the existing machinery didn't catch it

Because there is no machinery for this. Each detector is complete, correct, and
*unilateral*:

- Every heuristic detector's contract is `-> Optional[LoopResult]` — its only
  vocabulary is `None` (continue) or a terminal stop. `_account_convergence`
  (`:1048`), `_account_gate_stall` (`:1072`), `_account_revise_livelock`
  (`:1256`), `_account_planning_churn` (`:1327`), `_account_dispatch_wedge`
  (`:1417`) all share that signature. There is no third return value meaning
  *"ask somebody"*.
- The one detector that has a third option — `_account_convergence_clamp`
  (`:1150`) — **never returns a stop at all**: it sets a run-state flag that
  `runtime_cap` honours, records a decision, raises one deduped alert, and lets
  the run drain (its docstring, `:1152-1170`). It is proof the softer shape works
  in this codebase; it is also the only instance of it.
- The PM's prompt (`runner.py:2338-2397`, `_pm_prompt_segments`) contains
  orientation, focus, capabilities, model catalog, and a done-gate — and **no
  detector state whatsoever**. A PM six iterations into an eight-iteration
  `gate_stall_limit` countdown cannot see the countdown. (Closing that half is
  SPEC-24; this spec closes the *authority* half and works without it.)
- The two escalation ladders that do exist are both **task-scoped**, not
  run-scoped. F127 (`_handle_unproductive`, `:1513`) escalates one
  `(member_id, task_id)` pair; F128 (`_handle_completion_refused`, `:1699`)
  re-prompts one false-done claim. Neither can be asked "this run is stuck —
  what now?"
- And the stop is invisible until it lands. `_maybe_raise_monitor` (`:916`)
  raises an attention Problem *at the same moment* it stops, and only for
  governed runs (`state.mode == "off"` returns early, `:929-930`).

The gate-stall bug is the cleanest proof of the class: **a detector's own
evidence was wrong, the detector was the sole judge of its own evidence, and 22
iterations of healthy work were discarded on it.** No amount of threshold
tuning prevents the next one; tuning is what produced this state.

## Goals

- Split the stop reasons into **HARD** (terminate immediately, unchanged) and
  **HEURISTIC** (a detector's *opinion* that the run is stuck), and make that
  split explicit, enumerated, and locked by a test.
- Before a heuristic stop fires, dispatch **one** PM turn carrying the detector,
  its evidence, and the demand: *propose a concrete next action, or confirm the
  halt*. An actionable proposal continues the run and resets that detector's
  window; a confirmation or an abstention stops the run **with the same
  `stop_reason` as today**, plus the PM's rationale on the ledger.
- Be **bounded and non-recursive** by construction. This whole batch exists
  because of a livelock; it must not add one.
- **Compose** with the F127/F128 ladders rather than duplicate them.
- Fire on **both** loop chains — the sequential and the concurrent one.
- Change **no stop-reason strings and no exit codes**. The CLI's fail-closed
  allowlist (`runstream.py:133-149`) and its drift lock
  (`python/tests/cli/test_runctl_mutations.py:547`) stay green with zero edits.

## Non-goals

- **Not removing the detectors.** Each carries real signal, and each was written
  against an observed pathology (`planning_churn`: 130-task backlogs;
  `dispatch_wedged`: 130 todo / 0 dispatchable; `revise_livelock`: the 14-deep
  chain). This spec changes what a threshold *does*, not what it equals — and it
  does not retune a single one.
- **Not making stops advisory.** A confirmed halt halts. An unrefuted detector
  halts. The run still ends with the reason the detector named.
- **Not unbounded retrying.** At most `last_word_limit` interventions per run
  (default 2), and a second intervention for the same detector without
  intervening progress is refused before the turn is dispatched.
- **Not a new agent role.** The last word is a PM turn, on the existing
  `coding_turn.v1` envelope, through the existing `run_turn` seam.
- **Not making a no-op turn "actionable".** A PM that legitimately needs to say
  "I am waiting / I am pruning, add nothing" still cannot express that as a
  *plan*. That is SPEC-25's job, and until it lands such a turn reads as an
  abstention here. Stated so the next reader doesn't think it was missed.
- **Not surfacing detector state in the PM's standing prompt.** That is SPEC-24;
  the last-word prompt carries the evidence for *its* detector only.

---

## Item 1 — The taxonomy: HARD vs HEURISTIC

**Design.** Every constant in `autonomy.py:40-55` gets exactly one class. The
table *is* the spec; the code holds it as two frozensets beside the constants,
and a test asserts they partition the module's stop-reason constants exactly the
way `test_every_engine_stop_reason_is_triaged` already partitions them for the
CLI (`python/tests/cli/test_runctl_mutations.py:547-563`).

| Stop reason | Line | Class | Why | Intervention point |
|---|---|---|---|---|
| `definition_of_done` | `:41` | **DONE** | the work is done — the only outcome we actually want | none |
| `budget_exhausted` | `:40` | **HARD** | budget said stop; an intervention costs a model call it does not have | none |
| `cancelled` | `:44` | **HARD** | a human said stop | none |
| `checkpoint` | `:43` | **HARD** | operator-configured cadence, resumable via `errorta continue`; not a judgement about the run | none |
| `hard_blocker` | `:42` | **HARD** | a *member* declared it, not a detector — the team is asking for a human | none |
| `member_unhealthy` | `:47` | **HARD** | after the F120 classify-aware cap; a provider that cannot authenticate is not a strategy problem | none |
| `no_progress` | `:45` | **HEURISTIC** | `pm_idle_limit` = 2 consecutive non-progressing PM turns — the run-#2 stop | new hook |
| `not_converging` | `:50` | **HEURISTIC** | progress fingerprint unchanged for `convergence_stall_limit` iterations | new hook |
| `gate_not_improving` | `:52` | **HEURISTIC** | gate score not strictly improving for `gate_stall_limit` — the run-#1 stop | new hook |
| `planning_churn` | `:53` | **HEURISTIC** | `plan_streak_limit` PM plan turns with no worker turn | new hook |
| `dispatch_wedged` | `:54` | **HEURISTIC** | `wedge_min_tasks` todo, nothing dispatchable, sustained | new hook |
| `revise_livelock` | `:55` | **HEURISTIC** | broken lineage + no merge for `revise_livelock_limit` | new hook |
| `delivery_review_stalled` | `:51` | **HEURISTIC** | `delivery_review_round_limit` delivery rejections | new hook |
| `worker_unproductive` | `:48` | **HEURISTIC** | but only via the **ladder-exhausted** return (`:1657`), never the except-path return (`:1664`) | after the F127 ladder — see Item 4 |
| `completion_blocked` | `:49` | **HEURISTIC (already intervened)** | F128 already re-prompts the PM with the open item set between claims (`:1699-1719`); a second last word would ask the same party the same question | none — Item 4 |
| `no_actionable_work` | `:46` | **HEURISTIC (deferred)** | it comes from `decide_next` returning `Complete` (`:1811-1812`, `:2181-2186`), not from a detector window, so there is no window to reset; and it is CLI **success**-class (`runstream.py:75-77`), so intervening there risks flipping an exit code | none in this spec — SPEC-27 |

**Δ note — why `worker_unproductive` splits by return site.** `_handle_unproductive`
returns `WORKER_UNPRODUCTIVE` from two places: `:1657` (every rung of the ladder
was tried and the task is genuinely unexecutable — a strategy problem, exactly
what the PM should re-route) and `:1664` (the `except` arm — the escalation code
*itself* threw; the docstring says "stop visibly; never fall back to a silent
loop"). The second is an engine fault, not a run condition. Asking a PM to
propose a strategy for an engine bug is noise, and the honest record is a hard
stop. Implementation must make the two distinguishable (Item 5).

**Δ note — why `checkpoint` is HARD.** It is tempting to read a checkpoint as
"the harness stopped a working run". It is not: it is the operator's own cadence
knob (`_checkpoint_due`, `:861-871`), it is resumable, and `errorta continue`
exists for it. An intervention there would ask the PM to argue against the
operator.

**Acceptance.** `HARD_STOP_REASONS | HEURISTIC_STOP_REASONS | {DEFINITION_OF_DONE,
NO_ACTIONABLE_WORK}` equals the set of stop-reason constants in the module,
with empty pairwise intersections; a new constant added without a class fails
that test. No reason's string value changes, so `runstream.FAILURE_STOP_REASONS`
/ `SUCCESS_STOP_REASONS` and their partition test need no edit.

## Item 2 — The last-word turn

**Design.** One new loop-dispatched PM action, `LastWord`, alongside the existing
loop-scheduled turns in `topology.py` (`Plan:65`, `PMAssist:71`, `GateRun:85`,
`Complete:115`):

```python
@dataclass(frozen=True)
class LastWord:
    member_id: str          # the PM
    detector: str           # the heuristic stop_reason about to fire
    evidence: str           # what the detector observed, its threshold, how long
```

It is **not** returned by `decide_next` — the loop constructs it at the moment a
heuristic stop would have been returned, and runs it through the existing
`run_turn` seam (`runner.py:5680`, which already special-cases each action type
for transcript attribution). No new dispatch path, no new member, no new role.

**The prompt** (`_last_word_prompt`, beside `_pm_assist_prompt` at
`runner.py:2399-2416`, which it deliberately mirrors) carries three things and
nothing else:

1. **The detector and its threshold** — e.g. *"the acceptance gate score has not
   strictly improved for 8 iterations (score=6, best=6); the gate currently has
   failing commands."*
2. **The evidence it computed** — the same string `_maybe_raise_monitor` is
   already handed at each trip site (`:1066-1068`, `:1114-1117`, `:1296-1299`,
   `:1349-1352`, `:1453-1454`), plus the backlog shape from
   `_open_backlog_shape` (`:1303`) and the wedge culprits from
   `_dispatch_wedge_culprits` (`:1356`) where the detector already computes them.
   These strings exist and are discarded into an attention signal today; the
   last word is their first real consumer.
3. **The demand**, stated as a binary: *propose a concrete next action, or
   confirm the halt.* Plus the standing project orientation
   (`_orientation_text`) so the proposal is grounded.

**The response contract** is the unchanged `coding_turn.v1` PM plan envelope
(`PMPlanIntent`, `schemas.py:124-150`). Three outcomes:

| Response | Classification | Effect |
|---|---|---|
| `done: false` + ≥1 task that **materializes** (survives the Spec 08 dedupe gate and the Spec 15 capability lint, i.e. `_materialize_pm_tasks` created a row) | **actionable** | run continues; the detector's window resets (table below); decision `last_word_accepted` |
| `done: true` + `completion_summary` | routed to the normal done path | the F128 completion gate judges it exactly as it judges any done claim — the last word gets no special authority to declare victory |
| decisions-only, zero materialized tasks, or an explicit confirmation | **abstain / confirm** | run stops with the **original** `stop_reason`; decision `last_word_confirmed`; the PM's rationale is recorded |
| unparseable after the existing single repair retry | **unheard** — *not* a confirmation | run stops with the original `stop_reason`; decision `last_word_unparsed`; see Edge cases |

**Δ note — why "materialized task", not "any response".** The reset condition
has to be something the engine can *act on*, or the intervention is a licence to
loop: a PM that emits one decision saying "keep going" would reset the window
every time. Materialization is already gated by machinery that exists — the
duplicate index (`runner.py:2437-2440`) and the capability lint — so a
re-proposal of the same rejected idea produces zero rows and reads as an
abstention automatically. This is deliberately stricter than Spec 21's
"decisions-only turns are legal" rule: legal for a routine turn, not sufficient
for a window reset.

**The reset map.** Resetting the wrong counter is precisely how this becomes a
silent forever-loop, so it is enumerated, not inferred:

| Detector | Reset on `last_word_accepted` | Counter field |
|---|---|---|
| `no_progress` | `c.pm_idle = 0` | `:754` |
| `not_converging` | `c.last_progress_iter = c.iterations` | `:786` |
| `gate_not_improving` | `c.last_gate_iter = c.iterations` (not `last_gate_best` — the best score is a *fact*, not a window) | `:798` |
| `planning_churn` | `c.plan_streak = 0` | `:821` |
| `dispatch_wedged` | `c.wedge_streak = 0` | `:827` |
| `revise_livelock` | `c.last_broken_iter = c.iterations` | `:807` |
| `delivery_review_stalled` | `c.delivery_review_rounds = 0` | `:811` |
| `worker_unproductive` | nothing — the ladder already zeroed `c.unproductive_counts[key]` (`:1599`) and the PM's replacements are new tasks with fresh budgets | `:761` |

**Acceptance.** A heuristic stop with `last_word_limit > 0` dispatches exactly
one PM turn before returning. An actionable response returns `None` from the
hook (the loop keeps going) with exactly the mapped counter reset and no other.
A confirming/abstaining/unparsed response returns a `LoopResult` whose
`stop_reason` is byte-identical to today's.

## Item 3 — Bounded, non-recursive

**Design.** Three independent bounds, all cheap:

1. **Run budget.** `last_word_limit: int = 2` on `CodingAutonomyPolicy`
   (beside the other ladder caps, `:71-110`), mirrored in `policy_to_dict`
   (`:253`) / `policy_from_dict` (`:299`) — `0` disables the whole feature and
   restores today's behaviour exactly, per this module's `0 disables` convention
   (`gate_stall_limit`, `plan_streak_limit`, `wedge_stall_limit`,
   `revise_livelock_limit` all use it). Counter: `c.last_words: int`.
2. **Per-detector, once without progress.** `c.last_word_by_detector:
   dict[str, tuple[int, int]]` — `(iteration, merged_pr_count)` at the time of
   that detector's last intervention. A second intervention for the **same**
   detector is refused *before dispatching the turn* unless the merged-PR count
   has increased since. That is the same progress signal
   `_account_revise_livelock` already trusts to reset its own window
   (`:1286-1292`: "any merge anywhere is progress"), so it introduces no new
   notion of progress. Refused → the stop lands immediately, unchanged.
3. **Non-recursion.** The last-word turn is excluded from all detector
   accounting: `_apply_outcome` (`:2317`) must not increment `c.pm_idle` or
   `c.plan_streak` for a `LastWord` action, and the hook is not re-entered from
   inside itself. An intervention can therefore never manufacture the condition
   for another intervention.

**The boundedness argument, stated plainly.** The worst case is
`last_word_limit` extra PM turns per run — 2 model calls and 2 iterations,
against a default `max_iterations` of 200 (`:67`). Both are counted normally, so
`budget_exhausted` (HARD) still dominates and cannot be deferred by
interventions. Bound 2 makes the pathological case — a detector that keeps
re-tripping because the PM's proposal did nothing — cost exactly **one** turn,
not one per trip. Bound 3 makes the feature acyclic: interventions cannot beget
interventions. And the budget check runs *before* dispatch, so the last iteration
of a run is spent on work, not on asking about work.

**Acceptance.** With `last_word_limit=0`, every trace is byte-identical to
today's. With `last_word_limit=2`, no run can dispatch a third. A detector that
trips, is intervened on, and trips again with no merge in between stops on the
second trip without a turn.

## Item 4 — Where this sits relative to the F127 ladder

**Design.** `_handle_unproductive` (`:1513-1664`) is the existing escalate-up
ladder. Its rungs, in order, for one `(member_id, task_id)`:

| Rung | Lines | What it does |
|---|---|---|
| 1 | `:1531-1532` | let the same member retry, up to `worker_unproductive_limit` |
| 2 | `:1544-1591` | **model escalation** — same member, strictly stronger route (F129), up to `model_escalation_limit` |
| 3 | `:1608-1631` | **member exclusion + reassignment** — a different eligible member, up to `task_reassignment_limit` |
| 4 | `:1633-1650` | **PM assist** — one bounded PM turn to *split or re-scope this task* (`pm_assist_limit`) |
| 5 | `:1652-1657` | terminal: raise the blocking Problem, return `WORKER_UNPRODUCTIVE` |

**The last word is a new rung 5, and rung 5 becomes rung 6.** It fires at the
*loop* level when `_handle_unproductive` returns `WORKER_UNPRODUCTIVE` from
`:1657` — i.e. at the call sites `:1867-1869` (sequential) and `:2235-2240`
(concurrent) — and equally on `pm_assist_exhausted` (`:1873-1878` / `:2243-2248`),
which is the same ladder ending by a different door.

**It does not duplicate rung 4**, and the distinction is the whole reason it
composes:

- **PM assist asks a task question**: *"split or re-scope this task"*
  (`_pm_assist_prompt`, `runner.py:2402-2416` — it is explicitly forbidden to
  declare the project done, and its output is scoped to replacements for one
  parent task, which `_materialize_pm_tasks` excludes from the dedupe index for
  exactly that reason, `runner.py:2432-2440`).
- **The last word asks a run question**: *"every member has failed this task and
  the ladder is exhausted; propose a different route to the North Star, or
  confirm the halt."* Its answer may legitimately be to abandon that task
  entirely and attack the goal another way — a move rung 4 is structurally
  forbidden from making.

Ordering matters: the cheap, mechanical, task-scoped rungs run first and most
often; the expensive, judgement-heavy, run-scoped one runs at most twice per run.
If rung 4 already fixed it, rung 5 never fires.

**F128 gets no new rung.** `_handle_completion_refused` (`:1699-1719`) already
*is* a last-word loop: the PM is re-prompted with the open item set between
claims, `completion_refused_limit` times, and the prompt shows it exactly what is
open (`runner.py:2317-2325`). Adding a second intervention would ask the same
party the same question. It is classed HEURISTIC in the Item 1 table with the
intervention point recorded as "already present" so a future reader can see the
decision rather than assume an oversight.

**GL04's clamp is upstream and untouched.** `_account_convergence_clamp` (`:1150`)
narrows fan-out without stopping and is wired *before* Spec 16's hard stop in
both loops (`:1914`, `:2297`). It stays exactly where it is: it is the
zero-model-call recovery, and it should get its chance before anything spends a
turn.

**Acceptance.** A task that exhausts rungs 1–4 dispatches one last word before
`worker_unproductive` is returned. A task that is fixed at rung 2 or 3 never
reaches it. `pm_assist_limit` and the other ladder caps are unchanged.

## Item 5 — Where it hooks (both chains, or it is dead code)

**Design.** One helper, called from both loops:

```python
def _intervene(ledger, members, policy, c, stop: LoopResult, *,
               run_turn, should_cancel) -> Optional[LoopResult]:
    """Return None to CONTINUE the run (the PM proposed something actionable and
    the detector's window was reset), or the LoopResult to return as-is."""
```

`stop.stop_reason not in HEURISTIC_STOP_REASONS` → return `stop` unchanged,
first line. That makes the hard-stop path a single early return, which is what
the regression lock in Testing asserts.

**The detector chains are duplicated, and a hook in only one is dead code exactly
where it is needed.** Both `_account_gate_stall` and `_account_dispatch_wedge`
have two call sites each — verified:

| Detector | Definition | Sequential call | Concurrent call |
|---|---|---|---|
| `_account_planning_churn` | `:1327` | `:1893-1895` | `:2277-2279` |
| `_account_convergence` | `:1048` | `:1901-1903` | `:2285-2287` |
| `_account_gate_stall` | `:1072` | **`:1907-1909`** | **`:2289-2291`** |
| `_account_convergence_clamp` | `:1150` | `:1914` | `:2297` |
| `_account_revise_livelock` | `:1256` | `:1916-1918` | `:2302-2304` |
| `_account_dispatch_wedge` | `:1417` | **`:1921-1923`** | **`:2306-2308`** |

Plus the non-detector heuristic stops, also duplicated:
`delivery_review_stalled` (`:1837-1838` / `:2210-2212`), `no_progress`
(`:1885-1887` / `:2272-2274`), `worker_unproductive` (`:1866-1869` /
`:2234-2240` and `:1873-1878` / `:2243-2248`).

This is not a hypothetical risk — Spec 16's own comment at `:2298-2301` records
it as a lesson already learned: *"wiring BOTH chains is the dead-code lock — Spec
13 lifts the clamp and real runs go concurrent, so a detector only in the
sequential path would never fire where it's needed."* Every real run of interest
goes concurrent once the foundation merges (`runtime_cap`, `:539`, re-evaluated
each iteration; the loops hand off to each other at `:1802-1806` and
`:2067-2071`).

**The two chains have different return shapes, and the hook must respect both:**

- **Sequential** (`_run_sequential_loop`, `:1778`) returns immediately from each
  detector: `return LoopResult(...)` / `return conv_stop`. Each such site becomes
  `stop = _intervene(...); if stop is not None: return stop`.
- **Concurrent** (`_run_concurrent_loop`, `:2016`) *stages* stops into
  `pending_stop` during the apply phase and returns them at a **quiescent drain
  point** (`:2253-2271`, guarded by `if not in_flight`), and separately at
  `:2178-2180` (`if not in_flight: if pending_stop is not None: return
  pending_stop`) when nothing is running and nothing was dispatched. **Both**
  return points must route through `_intervene`, or a staged
  `delivery_review_stalled` / `worker_unproductive` escapes un-intervened on the
  wedged path. The inline detector chain at `:2272-2308` is inside the same
  quiescent block and hooks the same way as the sequential one.

That quiescence is also the correctness argument for Item 2's "no work in
flight" requirement: both concurrent hook points are already inside
`if not in_flight`, so the PM's proposal can never race a live worker future.
The sequential loop is quiescent by construction.

**`WORKER_UNPRODUCTIVE` return-site split (Item 1's Δ note).** `_handle_unproductive`
must distinguish its ladder-exhausted return (`:1657`) from its `except`-arm
return (`:1664`). Cheapest honest change: the except arm returns a distinct
sentinel the call sites map to a hard `LoopResult(WORKER_UNPRODUCTIVE, …,
detail={"engine_fault": True})`, and `_intervene` treats a stop carrying
`engine_fault` as HARD. The stop reason string does not change (Item 1's
no-new-reasons rule).

**Acceptance.** A test that greps `autonomy.py` for the count of `_intervene(`
call sites and asserts both loop functions contain at least one (the same
technique `test_spec12_18_prep` uses to grep this module for exact strings). A
behavioural test that drives each loop to the same heuristic stop and asserts the
intervention fired in both.

## Item 6 — Observability

**Design.** An intervention that leaves no trace repeats the G3 mistake the
roadmap put SPEC-22 first to fix. Three records, all on existing surfaces:

- **On dispatch** — `ledger.record_decision` (`ledger.py:924-936`) with
  `choice="last_word_requested"`, `context=f"detector {detector}"`, the evidence
  string as `rationale`, and `extra={"detector", "threshold", "window_iters",
  "intervention_index", "merged_prs"}`.
- **On outcome** — one of `last_word_accepted` (with the created task ids in
  `related_task_ids`), `last_word_confirmed`, `last_word_unparsed`, carrying the
  PM's own rationale verbatim.
- **On the stop** — `LoopResult.detail` (`:834`) gains
  `{"last_word": {"detector", "outcome", "pm_rationale"}}` when the run stops
  after an intervention, and `set_run_state(counters=…)` gains `last_words`
  (both writers: `runner.py:6369-6375` and `routes/coding.py:2538-2544`).
- **In the stop summary** — a run that stopped after an intervention must say so.
  `STOP_REASON_GLOSS` (`runstream.py:80-105`) stays byte-identical (no new
  reasons); the stream appends one line derived from the run state, e.g.
  *"the PM was asked and confirmed the halt: <rationale>"* or *"the PM was asked
  and could not be parsed — the halt was not confirmed"*. The second wording is
  load-bearing: an operator must be able to tell "the PM agreed" from "we could
  not hear the PM".
- **Attention** — `_maybe_raise_monitor` (`:916`) keeps firing at each trip site
  exactly as today; the last word's rationale is appended to the reason string so
  the Problem a human reads names both the detector's evidence and the PM's
  answer. (Its `mode == "off"` early return, `:929-930`, means ungoverned runs get
  the decisions but no Problem — unchanged, and out of scope here.)

**Acceptance.** Every intervention leaves exactly two decisions (request +
outcome). `GET /coding/projects/{id}/run` reports `counters.last_words`. A run
that stops after an intervention renders a second summary line naming the
outcome; a run that stops with `last_word_limit=0` renders exactly today's
output.

---

## Implementation notes

- **`python/errorta_council/coding/autonomy.py`** — carries almost all of it:
  the two frozensets beside `:40-55`; `last_word_limit` on
  `CodingAutonomyPolicy` (`:65`) + `policy_to_dict` (`:253`) / `policy_from_dict`
  (`:299`); `c.last_words` and `c.last_word_by_detector` on `LoopCounters`
  (`:748`); `_intervene` + `_last_word_evidence`; the hook at every site listed
  in Item 5; the `_apply_outcome` (`:2317`) exclusion; the
  `_handle_unproductive` except-arm split (`:1658-1664`).
- **`python/errorta_council/coding/topology.py`** — the `LastWord` dataclass
  beside `PMAssist` (`:71`) and in the `CodingAction` union (`:120-130`).
  `decide_next` (`:238`) is **not** touched — this action never comes from it.
- **`python/errorta_council/coding/runner.py`** — `_last_word_prompt` beside
  `_pm_assist_prompt` (`:2399`); a `LastWord` branch in `_execute` beside the
  `PMAssist` branch (`:4658`); the transcript role/task mapping beside `:5692`;
  `last_words` in the terminal `set_run_state` (`:6369-6375`).
- **`python/errorta_app/routes/coding.py`** — `last_words` in the worker
  thread's terminal `set_run_state` (`:2536-2544`).
- **`python/errorta_cli/runstream.py`** — the extra summary line only.
  `FAILURE_STOP_REASONS` (`:67-72`), `SUCCESS_STOP_REASONS` (`:75-77`),
  `STOP_REASON_GLOSS` (`:80-105`) and `classify_exit` (`:133-149`) are
  **unchanged by design**.
- **`docs/coding/PM_REFERENCE.md`** — the F145 anti-drift test asserts its
  embedded JSON equals `policy_to_dict(CodingAutonomyPolicy())`
  (`python/tests/coding/test_f145_pm_reference.py`), so adding
  `last_word_limit` requires updating it in the same PR or CI fails. Feature,
  not friction.
- **No new stop reason, no new role, no new schema version.** The response is
  `coding_turn.v1` `PMPlanIntent`, unchanged.

## Edge cases

- **A PM that proposes the same action repeatedly.** Handled without new code:
  the Spec 08 dedupe gate (`runner.py:2437-2440`) rejects the duplicate, zero
  rows materialize, and the turn reads as an abstention → the run stops. Bound 2
  of Item 3 means it cannot even be asked twice for the same detector without an
  intervening merge. The duplicate-rejection note is already fed back into the PM
  prompt (`_duplicate_rejection_note`, `runner.py:2050`, folded into the done-gate
  block at `:2328`), so the next
  ordinary plan turn sees why.
- **A PM turn that fails to parse during the intervention.** It must **not**
  count as a confirmed halt. This is the Spec 21 lesson in its purest form: three
  of four PM retries in run #2 died on `missing decisions[0].title` while the
  model kept re-emitting a perfectly reasonable shape (`schemas.py:102-121`),
  and the harness read repeated schema rejection as PM idleness. The existing
  single repair retry applies; a still-unparsed turn records
  `last_word_unparsed`, the run stops with the original reason, and both the
  decision and the stop summary say the PM was *not heard* rather than that it
  agreed. It also must not increment `pm_idle`, `plan_streak`, or the F127
  unproductive counters — a synthetic turn the harness initiated cannot be
  allowed to accelerate a different detector.
- **An intervention firing while work is in flight.** Cannot happen: both
  concurrent hook points (`:2178`, `:2253`) are inside `if not in_flight`, and
  the sequential loop is quiescent by construction. `_reconcile_stale`
  (`runner.py:5684`) still runs before the turn, so the PM sees a current
  backlog.
- **Cancel during an intervention.** `should_cancel` is checked at `:1807`
  (sequential) and `:2073` (concurrent), both *before* dispatch; `_intervene`
  re-checks it immediately before spending the model call and returns the
  original stop if a cancel arrived. A cancel must never wait on an intervention.
- **Checkpoint / resume re-arming the budget.** Real, and it must be fixed in
  this spec or Item 3's bound is fiction. `CodingRunner.run` is called with no
  `counters` argument by the only production caller (`routes/coding.py:2534`),
  and `run_coding_loop` does `c = counters or LoopCounters()` (`:900`) — so
  **every start and every resume gets a fresh counter set today**, and would get
  a fresh intervention budget with it. Fix: persist `last_words` in the run-state
  counters (Item 6) and rehydrate it into `LoopCounters` at run start. Note this
  is the *only* counter that needs it — the others are genuinely per-process
  windows, and re-arming them across a checkpoint is correct.
- **Budget exhaustion racing an intervention.** The budget check precedes the
  hook at `:1815-1818` (sequential) and `:2075` / `:2184-2185` (concurrent), and
  `_intervene` re-checks: if the model-call budget cannot cover the turn, the
  stop lands unmodified. `budget_exhausted` is HARD and can never be deferred.
- **A run with no PM seated.** `_handle_unproductive` already guards this
  (`:1633`, `pm_ids` empty → terminal rung). `_intervene` returns the stop
  unchanged when no PM member exists.
- **A detector trip inside the last-word turn's own effects.** The PM's new tasks
  change the progress fingerprint and the backlog shape, which is *intended*
  motion; Item 3's bound 3 keeps that from re-entering the hook in the same
  iteration.

## Testing

Mirrors the ladder tests already in `python/tests/coding/`:

- **The happy path** — a fixture driven to a heuristic stop (`gate_not_improving`
  is the cheapest to synthesize) dispatches **exactly one** PM turn; a stub
  `run_turn` returning a plan with one novel task continues the run and resets
  **only** that detector's counter (assert the other windows are untouched — a
  reset-map regression is silent otherwise).
- **Confirm / abstain** — a decisions-only response, and an explicit
  confirmation, both stop with the **original** `stop_reason` byte-identical to
  the pre-spec value, with the PM's rationale on the ledger and in
  `LoopResult.detail`.
- **The second-intervention lock** — the same detector trips twice with no merge
  in between: the second trip stops with **zero** additional `run_turn` calls
  (assert the call count, not just the outcome — "it stopped" is also true of the
  broken implementation that dispatched a turn and ignored it).
- **The run budget** — `last_word_limit=2` and three distinct heuristic trips:
  two turns, then a stop. `last_word_limit=0` reproduces today's trace exactly
  (this is the "no behaviour change when off" lock).
- **The hard-stop regression lock** — parametrized over every member of
  `HARD_STOP_REASONS`: the stop returns with `run_turn` never called. Explicitly
  includes `cancelled` and `budget_exhausted`, and the `engine_fault`
  `worker_unproductive` variant.
- **The both-chains lock** — the *same* scenario driven through
  `_run_sequential_loop` and `_run_concurrent_loop` (force via
  `max_parallel_workers=1` vs `>1`, as the existing concurrency tests do) must
  produce the same intervention. Plus a static check that both function bodies
  contain a `_intervene(` call, in the `test_spec12_18_prep` grep style — the
  behavioural test can pass on one chain by accident of scheduling; the grep
  cannot.
- **The unparsed-turn lock** — a `TurnParseError` from the intervention stops the
  run, records `last_word_unparsed` (never `last_word_confirmed`), and leaves
  `c.pm_idle` / `c.plan_streak` / `c.unproductive_counts` unchanged.
- **The taxonomy partition test** — Item 1's acceptance, in the style of
  `test_every_engine_stop_reason_is_triaged`
  (`python/tests/cli/test_runctl_mutations.py:547`): every stop-reason constant
  in the module belongs to exactly one class; adding one without a class fails.
- **The two run-#1 / run-#2 replays** — a `gate_not_improving` trip on a green
  gate and a `no_progress` trip with open PRs each end as a *continuation* rather
  than a stop. These are the specific traces that opened this spec and they
  belong in the suite as named regressions, even though Spec 21 already fixed
  their underlying bugs: the point of this spec is that the *class* survives its
  instances.

## Documentation

- `docs/coding/PM_REFERENCE.md` — `last_word_limit` in the policy table (required
  by the F145 drift lock), and a short "what a last-word turn is" note, since it
  is a prompt shape the PM will actually see.
- `docs/CLI.md` — a FAILURE-class stop may now carry a second summary line
  recording that the PM was asked; "the PM confirmed the halt" and "the PM could
  not be parsed" mean different things and an operator should be told which.
- `ROADMAP-autonomy.md` — mark SPEC-23 as specified; success criterion #2 ("no
  run ends on a heuristic stop without a recorded PM intervention that was given
  a real chance to continue") is what this spec is measured against.

## Out of scope / follow-ups

- **SPEC-24 (governance visibility)** — the last-word prompt shows the evidence
  for the *one* detector that fired. Rendering live detector state into every PM
  prompt, so the PM can steer before the axe swings, is the other half of G2 and
  ships separately. This spec is deliberately useful without it.
- **SPEC-25 (expressibility)** — until a typed "blocked / no-op with reason"
  intent exists, a PM whose correct answer is "do nothing, wait for the in-flight
  PRs" cannot express an actionable last word and its turn reads as an
  abstention. That is a known, named gap, not an accident.
- **SPEC-27 (convergence as control)** — generalising *every* detector's default
  action from "stop" to "narrow" (GL04's clamp as the pattern) needs somewhere to
  escalate to; this spec is that somewhere. `no_actionable_work` belongs to that
  work too.
- **A last word for `completion_blocked`** — deliberately not wired (Item 4).
  Revisit only if F128's re-prompt loop is observed failing for a reason its own
  prompt cannot convey.
- **Human-in-the-loop as an intervention target.** The last word goes to the PM
  because the run is unattended by definition. A variant that pages a human
  instead (or as a second rung) is a product question, not this spec's.
- **Persisting the full `LoopCounters` across resume.** This spec persists one
  field because its bound depends on it; the general question — that every
  detector window silently re-arms on `errorta continue` — is a real finding
  surfaced here and left for its own change.
