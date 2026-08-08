# Spec 46 — per-task drop-count quarantine damps the create→drop→re-create loop

**Source:** GitHub issue [#93](https://github.com/wiggins-j/errorta_app/issues/93)
("planning_churn: undamped create→drop→re-create loop on a repeatedly-dropped task stops
the whole run; drop reason not surfaced"). Observed on the `errorta` headless CLI,
sidecar `v0.1.0-alpha.17`, macOS, continuing an imported repository with a multi-task
backlog; the churn concentrated on the most integration-heavy (high fan-in) task while
self-contained tasks completed normally.
**Target version:** v0.1 (coding council — a run-scoped drop ledger, `_apply_pm_cancels`,
`_materialize_pm_tasks`, `CodingAutonomyPolicy`, `attention` escalation, run stop-reason
classification; CLI renderers via the SPEC-45 shared foundation)
**Relates to:** [SPEC-45](SPEC-45-capability-blocked-auto-unblock.md) (companion — **owns**
the machine-readable drop/refuse reason foundation this spec consumes) ·
[SPEC-27](SPEC-27-convergence-as-control.md) (authored the `planning_churn` guard this
spec keeps as the outer backstop) · [SPEC-30](SPEC-30-execution-gate-and-grounded-review.md)
(authored `_apply_pm_cancels` convergence pruning)
**Status:** proposed · **Owner:** wiggins-j

---

## Problem

On a coding run an integration-heavy task can enter a **create → drop → re-create** loop
that never produces a worker turn. The PM creates the task; `_apply_pm_cancels`
(`runner.py:~3676-3716`) drops it (`pm_task_cancelled`, state → `dropped`) before a dev
executes it; and because task dedupe **intentionally** exempts `done`/`dropped`/`blocked`
from suppression (`task_dedupe.py:27-29`, so a regression can re-open one), the PM
re-creates the same task on its next plan turn — which drops again.

No interleaved worker turn ever resets the plan streak, so `plan_streak` climbs
(`autonomy.py:4800-4801`) to `plan_streak_limit` (default 6, `autonomy.py:219`) and
`planning_churn` (`autonomy.py:2848-2879`) halts the **entire run** with CLI exit 7
(`runstream.py:66-149`, `errors.py:32`).

`planning_churn` is the guard, not the cause — it correctly stops an infinite loop. But
the create↔drop loop has **no damping**, so the run burns budget re-planning the same
task and then stops *wholesale* instead of isolating the one pathological task. Two
things make it hard to escape or diagnose:

1. **No per-task drop damping.** There is no per-task (normalized title + declared
   `target_files`) drop counter that would stop re-creating a task after N drops and
   escalate it. The only backstop is `planning_churn`, which stops the whole run rather
   than quarantining the one task and continuing the rest of the backlog.
2. **Drop reason is not surfaced.** `pm_task_cancelled` writes a hard-coded rationale
   (`runner.py:~3706`), so an operator watching `decisions`/`log` cannot see what is
   driving the loop.

The loop persisted even when the task was a fully-specified concrete dev task (explicit
`--detail`, declared `target_files`) with the capability enabled — so it is **not** a
missing-capability symptom (that is [SPEC-45](SPEC-45-capability-blocked-auto-unblock.md));
the create↔drop dynamic is self-sustaining once a task starts getting dropped.

## Principle

> Quarantine the pathological task, don't stop the run. A single task that cannot pass
> should be isolated and escalated to the operator by name-and-reason, while the rest of
> the backlog keeps moving. `planning_churn` remains the outer backstop, not the primary
> control.

## Dependency — machine-readable drop reason

This spec consumes the reason foundation **defined and implemented in
[SPEC-45](SPEC-45-capability-blocked-auto-unblock.md) §Shared foundation**: the
reason-code vocabulary, the structured `extra` reason blob on drop decisions, the
`Task.reason_summary` mirror, and the `render/decisions.py` / `render/board.py` /
`render/status.py` surfacing. SPEC-46 adds the `task_quarantined` reason-bearing decision
and the "N quarantined" status rollup on top of that foundation. If SPEC-46 lands first,
it carries the foundation instead; the two must not double-implement it.

## What this spec does

- **Drop counter keyed by identity, not task id.** Because dedupe deliberately lets a
  dropped task be re-created as a **new** record, the counter cannot live on the task
  record. Introduce a **run-scoped drop ledger**: `identity_key → drop_count`, where
  `identity_key` reuses the normalization `task_dedupe` already computes (filler-stripped
  title tokens + declared `target_files`; expose a `task_dedupe.identity_key(task)` helper
  so the ledger and dedupe cannot diverge). The ledger is persisted with run state so it
  survives across the create→drop cycles within a run.
- **Increment at the drop.** In `_apply_pm_cancels` (`runner.py:~3706`), on the
  `dropped` transition, increment `drop_ledger[identity_key]` and write the machine-readable
  reason (SPEC-45 foundation) onto both the `pm_task_cancelled` decision and the task's
  `reason_summary`.
- **Quarantine at threshold.** Add policy knob
  `CodingAutonomyPolicy.task_drop_quarantine_limit: int = 3` (next to
  `plan_streak_limit`, `autonomy.py:219`), deliberately **`< plan_streak_limit`** (3 < 6)
  so quarantine fires **before** `planning_churn`. In `_materialize_pm_tasks`
  (`runner.py:~3378-3427`), before creating a task, look up its `identity_key` in the drop
  ledger; if `drop_count >= task_drop_quarantine_limit`, **suppress creation** and record a
  reason-bearing `task_quarantined` decision instead of the task.
- **Escalate, don't halt.** On quarantine, raise a **deduped Problem** via the existing
  `attention` primitive — a new `source="task_pathology"` deduped by `identity_key`
  (mirror the `raise_monitor_problem` dedupe pattern, `attention.py:~365-390`) — with a
  message naming the task and its drop reason: *"task ‹title› dropped N× — reason: ‹code›;
  needs operator input."* The Problem flags for the operator; it does **not** stop dispatch
  of unrelated tasks — so it MUST be raised `blocking=False`, overriding `raise_signal`'s
  `kind == "problem"` default. A blocking signal at `stage="development"` makes
  `governance_scheduler`'s `blocks_stage` gate halt the whole run with `blocked_on_problem`
  (`block_on_problems` defaults on), which is the wholesale stop this spec exists to
  prevent. Because the pathological task is no longer re-created, worker /
  governance-progress turns on the rest of the backlog reset `plan_streak`, so
  `planning_churn` no longer trips on this loop.
- **Clean terminal when quarantine is the last work.** If, after quarantine, the only
  remaining backlog is quarantined (nothing else dispatchable), the run ends on a
  **distinct, triaged stop reason** `quarantined_task_needs_input` rather than the generic
  `planning_churn`/exit-7. Add it to the stop-reason classification
  (`runstream.py:~66-149`) with its own exit semantics and a `render/status.py` message,
  so the operator sees the real cause and the specific task, not "planning churn."
- **Dedupe exemption untouched.** The `dropped`/`blocked`-is-exempt rule stays as-is (it
  is correct for genuine regression re-opens); the drop-count damping is the missing
  pairing that keeps the exemption from feeding an unbounded loop.

## Non-goals

- Removing or weakening the dedupe exemption (explicitly kept; damping is the fix).
- Removing `planning_churn` — it stays as the outer backstop for loops this damping does
  not anticipate.
- Auto-repairing the pathological task (rewriting scope, splitting it). Quarantine +
  escalate hands it to the operator; automatic repair is out of scope.
- Cross-run persistence of the drop ledger. The counter is per-run; a fresh run starts
  clean. (Revisit only if churn is observed to survive restarts.) The ledger lives in
  `run_state.json`, which SURVIVES a run, so this non-goal is only real if the key joins
  the fresh-start hygiene clear in `CodingRunner.run` alongside `last_words` /
  `narrow_ladder` / `tests_not_applicable_count`. Uncleared, a task pruned once in each of
  three separate runs is silently quarantined on run 4.

## Regression locks

- **Quarantine before churn.** Test: force a task into a create→drop loop; assert it is
  quarantined at `task_drop_quarantine_limit` (3) drops — a `task_quarantined` decision is
  recorded, the task is not re-created, and the run does **not** stop with
  `planning_churn`/exit 7. Assert `task_drop_quarantine_limit < plan_streak_limit` holds so
  the ordering is guaranteed.
- **Rest of the backlog keeps running.** Test: with one pathological task and several
  self-contained ones, the self-contained tasks complete while the pathological one is
  quarantined; the run does not halt on the pathological task while other work is
  dispatchable.
- **Per-run ledger.** Test pair: a fresh run (`counters is None`) starts with an empty drop
  ledger, and a carried-counters resume — the same run — keeps its counts.
- **Escalation does not halt.** Test: after quarantine, `attention.blocks_stage(pid,
  "development")` is False, so the governance gate cannot convert the escalation into a
  whole-run `blocked_on_problem` stop.
- **Deduped escalation.** Test: a quarantined identity raises exactly **one** open
  `task_pathology` Problem regardless of how many plan turns re-encounter it (dedupe by
  `identity_key`), and the Problem message contains the drop reason code.
- **Identity key parity.** Test: `task_dedupe.identity_key` and the ledger use the same
  normalization, so a re-created task with a filler-verb-only title change
  (e.g. "fix X" ↔ "implement X") maps to the **same** ledger entry and its count carries
  across cycles.
- **Distinct terminal reason.** Test: when a quarantined task is the only remaining work,
  the run stops with `quarantined_task_needs_input` (not `planning_churn`), and
  `render/status.py` names the task and reason.
- **Reason surfaced.** Test: `pm_task_cancelled` and `task_quarantined` decisions carry
  `extra.reason_code`, and `render/status.py` shows the "N quarantined" rollup.
