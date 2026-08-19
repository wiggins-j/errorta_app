# Slack Task Control + Milestone-Only Stream — Design (Slice 6)

**Date:** 2026-08-19
**Status:** Design approved by the operator 2026-08-19. Not yet implemented.
**Depends on:** Slice 5 (studio auto-start + progress stream) — merged.
**Modules:** `errorta_slack/{tools,outbound,concierge}.py`.

---

## 1. Problem

Both findings come from the first live run of Slice 5 (project `tip-calculator`,
2026-08-19 16:42–16:51 UTC).

### F1 — a `completion_blocked` run cannot be resolved from Slack

The run stopped with `stop_reason: completion_blocked` and posted, correctly:

> :red_circle: Run can't complete: open work remains. The team reported done, but
> 2 item(s) are still open: task *Resolve attention problem: Stuck:
> foundation_not_converging* [todo]; task *materialize design system* [blocked]
> (human-required). **Finish or cancel these tasks**, or resolve the blocked merge
> conflict, then start the run again.

The instruction is actionable and the channel cannot act on it. `TOOL_CATALOG`
has no verb that finishes, cancels, or unblocks a task. The operator is told
exactly what to do and given no way to do it without leaving Slack — which
defeats the premise that a project can be driven from its channel.

The engine already has the operation: the runner cancels its own work with
`store.update_task(task_id, state="dropped")` (`runner.py:1603`, `:1662`,
`:1734`, `:3957`, `:6709`). Only the Slack surface is missing.

### F1a — the PM cannot learn a task id (discovered while designing F1)

`project_status` advertises a `tasks` field, but its entries are **team-log
entries**, not task records: the keys are `at`, `kind`, `member`, `message`,
`role`. There is no `task_id` in anything the PM can see.

A `cancel_task(task_id=…)` verb would therefore be **uncallable** — the model
cannot supply an argument it has no way to learn. This is the same defect class
as Slice 5c's missing argument schema: a verb the catalog offers and the model
cannot actually use. Fixing F1 without fixing F1a ships a dead verb.

### F2 — non-blocking alerts flood the channel

`outbound._attention_items` turns EVERY open attention signal into a Slack
message, `kind="fyi"` for the non-blocking ones. The live run produced 28
non-blocking `alert` signals — reviewer nitpicks on the spec, e.g. *"Rounding
method not specified for per-person calculation"*, *"Custom tip field initial
state unspecified"* — each queued as its own message.

The approved Slice 5 design was **artifact + phase milestones, typically 6-12
messages, no filler**. 28 nitpick messages is filler by the operator's own
definition.

They are also **redundant**: `team_log.build_team_log` already emits
`reviewed an artifact (5 finding(s))` as a milestone for the same review round.
The channel already reports that findings landed and how many.

## 2. Design

### 2.1 `list_open_tasks` — [R]

Returns the project's non-terminal tasks as `{task_id, title, state}`, newest
first, capped at 20. Terminal states (`done`, `dropped`, `merged`) are excluded:
the verb exists to answer "what is still open", and a completed backlog would
bury that.

This is what makes §2.2 callable. It is a pure read with no side effect, so [R].

### 2.2 `cancel_task` — [C]

`update_task(task_id, state="dropped")` — the runner's own cancel semantic, so a
Slack cancel and an engine cancel leave identical ledger state.

**[C], not [R].** `queue_bugs` is [R] because it only ADDS work; this SUBTRACTS
it. Cancelling a task destroys queued work and directly changes what the team
will spend on. With autopilot off it stages an Approve button; with autopilot on
it fires immediately, which is the operator's standing choice for every C verb.

### 2.3 `unblock_task` — [C]

`update_task(task_id, state="todo")` on a task currently `blocked` — the
"human-required" half the completion gate names. Refuses with a clean error
result if the task is not blocked, so it cannot be used to rewind arbitrary
state; it is an unblock, not a general state setter.

### 2.4 Argument declarations

All three declare their arguments in `TOOL_CATALOG` per Slice 5c, so the model
is told `task_id` is required rather than guessing it.

### 2.5 Errors

An unknown or malformed `task_id` returns
`{"status": "error", "detail": "unknown task"}`. Never an exception into a live
turn (module invariant), and the honesty rule now forces the PM to report it as
not-done rather than claiming the task was cancelled.

### 2.6 F2 — stop emitting non-blocking attention items

`_attention_items` emits an item only when the signal is `blocking`. Blocking
signals are untouched: still `kind="decision"`, still buttoned, still ignoring
the channel mute.

This is a deletion. Nothing replaces the dropped messages because the review
milestone already carries the count, and the signal detail remains available in
the app where it can be acted on.

## 3. Non-goals

- No general task editing (retitle, reassign, re-prioritise). The operator chose
  "just what the gate demands"; every extra verb is another surface a misread
  instruction can act through.
- No batching/digest layer for alerts — dropping them removes the need.
- No change to how blocking signals are rendered or resolved.

## 4. Test plan

Every path below is currently untested.

**F1a / `list_open_tasks`**
- Returns `task_id`, `title`, `state` for `todo` and `blocked` tasks.
- Excludes `done` / `dropped` / `merged`.
- Caps at 20 on a larger backlog.
- **The regression that would have caught F1a:** every required argument
  declared by any verb in `TOOL_CATALOG` is present in the output of at least
  one [R] verb, so a future uncallable verb fails CI rather than failing live.

**`cancel_task`**
- Drops the named task; the ledger records `state="dropped"`.
- Unknown id → `{"status": "error"}`, no exception.
- Declared `[C]`, and from chat text alone (`confirmed_via=None`) it STAGES
  rather than executes — the injection wall.

**`unblock_task`**
- A `blocked` task becomes `todo`.
- A task that is not blocked is refused, and its state is unchanged.

**F2**
- A non-blocking signal produces NO outbound item.
- A blocking signal still produces a `decision` item.
- A muted channel still receives the blocking one.
