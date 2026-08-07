# Spec 45 — capability-refused tasks persist as `blocked` and auto-unblock when the gate opens

**Source:** GitHub issue [#91](https://github.com/wiggins-j/errorta_app/issues/91)
("Task dropped for a missing capability isn't re-dispatched once the capability is
enabled; run wedges idle with no summary"). Observed on the `errorta` headless CLI,
sidecar `v0.1.0-alpha.17`, macOS, continuing an imported repository.
**Target version:** v0.1 (coding council — `runner._materialize_pm_tasks`,
`control_actions.create_task`, a new per-turn capability re-evaluation pass; CLI
renderers `render/board.py`, `render/status.py`, `render/decisions.py`)
**Relates to:** [SPEC-46](SPEC-46-drop-count-quarantine.md) (companion — the two
share the **machine-readable drop/refuse reason** foundation defined in §Shared
foundation below; SPEC-45 **owns and implements** that foundation, SPEC-46 consumes
it) · [SPEC-15](SPEC-15-capability-aware-planning.md) (authored capability-aware
planning; this fixes its destructive refusal) · [SPEC-26](SPEC-26-role-capability-closure.md)
(role reseat — the analogue this spec adds for *tasks*)
**Status:** proposed · **Owner:** wiggins-j

---

## Problem

When a DEV task is classified `execution` but the required capability gate is not
available, planning **destructively refuses** it: `_materialize_pm_tasks`
(`runner.py:~3416-3427`) and `control_actions.create_task` (`control_actions.py:~376-388`)
record a `task_requires_absent_capability` decision and `continue` — **the task is
never persisted**. There is no backlog record.

The gate predicates (`gate_available`, `get_unit_test_commands()`, `tester_dispatchable`)
are re-read **live every turn** — so the *information* needed to re-dispatch arrives
on its own once the operator enables the capability and re-confirms setup. But because
nothing was persisted, there is nothing to re-materialize. The dropped task stays gone,
downstream tasks gated on it stay undispatchable, and the run goes idle / stops with no
"N tasks blocked on capability X" summary explaining why. The only workaround is
`errorta interject` asking the PM to re-dispatch the specific task by hand.

The refusal decision also carries no machine-readable reason (see §Shared foundation),
so `board`/`status`/`decisions` cannot show that a task is waiting on a specific
capability.

## Principle

> A capability gap is a **pause**, not a **deletion**. Persist the work as `blocked`
> with a machine-readable reason, and let the already-live gate read heal it. The
> system must never silently forget work it refused for a condition it re-checks every
> turn.

## Shared foundation — machine-readable drop/refuse reason

*(Implemented here in SPEC-45; consumed by [SPEC-46](SPEC-46-drop-count-quarantine.md).
Lands with whichever of the two merges first.)*

Today every drop/refuse decision carries a **hard-coded** rationale string
(`_apply_pm_cancels` writes the constant "obsolete / over-scoped" at `runner.py:~3706`;
capability refusals write a fixed `task_requires_absent_capability` choice with no
detail), and `render/decisions.py` **never renders the decision `extra` blob**
(`decisions.py:~4-5,28-33`). So no CLI view can show *why* a task left the backlog.

- Define a small **reason-code vocabulary** keyed by code, not prose:
  `missing_capability`, `over_scoped`, `dependency_unmet`, `pm_pruned`
  (extensible; unknown → `other`). Codes live next to the task/decision model so both
  the council and the renderers import the same constants.
- Every drop/refuse decision writes a structured reason into the decision **`extra`**
  blob: `{"reason_code": <code>, "reason_detail": <short str>, "capability": <cap|null>}`.
  The reason is derived from what is **already in scope** at the drop/refuse site — the
  task's `reason_summary` (`ledger.py:403`), `last_tool_failure` (`:408`), prior `state`
  (`todo` vs `blocked`), and any `block_task(..., reason=...)` value. Capability
  refusals set `reason_code="missing_capability"`, `capability=<cap>`.
- Mirror the same reason onto the **`Task.reason_summary`** field so it survives on the
  task record, not only on the decision stream.
- **Surfacing:**
  - `render/decisions.py`: render `reason_code`/`reason_detail` for the drop/refuse
    choices (`pm_task_cancelled`, `task_requires_absent_capability`, and SPEC-46's
    `task_quarantined`). This is the first consumer of the `extra` blob in that renderer.
  - `render/board.py`: render the reason on `blocked` tasks (currently titles-only,
    `board.py:~66-70`).
  - `render/status.py`: add a one-line rollup — "N blocked on capability X" (this spec)
    and "N quarantined" (SPEC-46).

## What this spec does

- **Persist instead of refuse.** In `_materialize_pm_tasks` (`runner.py:~3416-3427`) and
  `control_actions.create_task` (`control_actions.py:~376-388`), replace
  record-decision-and-`continue` with **creating the task in state `blocked`** carrying
  `_extras["blocked_reason"]="missing_capability:<cap>"` and `reason_summary` set via the
  shared foundation. Still record the (now reason-bearing) decision so the PM's
  `_capability_refusal_note` prompt (`runner.py:~2482-2516`) keeps working.
- **Auto-unblock pass (new, per-turn).** Add a re-evaluation step early in the run loop
  that scans tasks in `state="blocked"` whose `blocked_reason` starts with
  `missing_capability:`. For each, re-read the live gate for that capability; if it is now
  available, `update_task(state="todo")`, clear `blocked_reason`, emit a
  `capability_unblocked` decision (reason-bearing), and resolve the standing
  `capability_gap` alert (`attention.raise_capability_gap_alert` /
  its resolver, `attention.py:~855-883`). The task is now dispatchable with no operator
  interjection.
- **Dedupe interaction (prevents feeding SPEC-46's loop).** A capability-blocked task
  must **suppress re-creates** so the PM does not spawn a duplicate while it waits.
  Narrow the general `blocked`-is-exempt rule: `task_dedupe.build_open_index`
  (`task_dedupe.py:~127-141`) additionally includes `blocked` tasks **whose
  `blocked_reason` starts with `missing_capability:`** in the open index. The general
  exemption for other `blocked`/`dropped`/`done` tasks (regression re-open) is left
  intact — only capability-waiting tasks become suppressing.
- **Surface it** via the shared-foundation renderers (board reason, status rollup,
  decisions reason).

## Non-goals

- Changing *how* a task is classified as `execution` or which capabilities gate it
  (SPEC-15's classifier is unchanged).
- Auto-enabling capabilities. The operator still turns the capability on; this spec only
  ensures the refused task self-heals once they do.
- The per-task **drop-count** damping and quarantine — that is SPEC-46. (A
  capability-blocked task that keeps failing *after* it unblocks is then subject to
  SPEC-46's drop counter like any other task.)

## Regression locks

- **A capability-refused task is persisted, not dropped.** Test: classify a task
  `execution` with its gate unavailable → assert a task exists in `state="blocked"` with
  `blocked_reason` = `missing_capability:<cap>` (was: no task created).
- **Auto-unblock on gate open.** Test: with a capability-blocked task present, enable the
  gate and run one loop turn → assert the task transitions to `todo`, a
  `capability_unblocked` decision is recorded, and the `capability_gap` alert is resolved.
- **No duplicate while waiting.** Test: with a capability-blocked task present and the
  gate still closed, run a PM plan turn that would re-propose the same task → assert
  dedupe suppresses it (no second task record) and the streak does not climb from
  re-creation.
- **Reason is machine-readable and surfaced.** Test: the refusal/unblock decisions carry
  `extra.reason_code`; `render/board.py` shows the reason on the blocked task and
  `render/status.py` shows the "N blocked on capability X" rollup.
