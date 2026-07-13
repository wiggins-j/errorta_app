# F155 — Delivery-review round cap (truthful stop instead of livelock)

## Problem

Audit G4, and the failure observed live: under `--autonomous` the council looped
"fix delivery review findings" for over an hour. Root cause, grounded:

When `delivery_review` rejects the integrated head it files a `fix delivery review
findings` dev task (runner.py:3692-3697). In `_apply_outcome`'s `project_done`
branch (autonomy.py:1189-1196):

```python
if not result.passed:
    if result.filed_findings:
        c.pm_idle = 0        # <-- counts as PROGRESS
    else:
        c.pm_idle += 1
    return False
```

Because a filed finding resets `pm_idle`, the `NO_PROGRESS` stop
(`pm_idle_limit`) never trips. Each round also adds a task and changes the head, so
`_progress_fingerprint` (autonomy.py:203-241) changes and `NOT_CONVERGING`
(`convergence_stall_limit`) never trips. `delivery_reviewed_head` caches only the
*same* head (runner.py:3560), so a new head always re-reviews. The reject→fix→
re-review cycle therefore burns the full `max_iterations` (default 200) and ends as
`BUDGET_EXHAUSTED` — an untruthful "ran out of budget" instead of "delivery review
kept failing." There is **no** `delivery_review_round_limit` anywhere (grep-confirmed).

## Goal

Cap the number of **consecutive failed** delivery-review rounds. When the cap is
hit, stop with a new terminal reason (`delivery_review_stalled`) that names the
real problem, instead of silently churning to budget exhaustion. A passing delivery
review is terminal (`done`) as today; the counter resets on a genuinely fresh start,
not on every failed round.

## Non-goals

- Not changing what delivery review *checks* (F152/F153/F154 do that).
- Not lowering `max_iterations` or other caps.
- Not a per-finding cap — it bounds *rounds* (a round = one `project_done` claim
  whose delivery review rejected and filed findings).

## Design

Three small edits in `python/errorta_council/coding/autonomy.py`.

### 1. New stop reason

```python
DELIVERY_REVIEW_STALLED = "delivery_review_stalled"   # F155: too many failed delivery-review rounds
```

Register it wherever the terminal reasons are enumerated for reporting/exit-code
mapping (same treatment as `NOT_CONVERGING` / `NO_PROGRESS` — a truthful
non-success terminal, not a crash).

### 2. Policy field + counter

- `CodingAutonomyPolicy` (autonomy.py:56-94): add
  `delivery_review_round_limit: int = 3` and thread it through `to_dict`
  (:99-110) and `from_dict` (:119-138, clamp `max(1, …)`).
- `LoopCounters` (autonomy.py:286-320): add `delivery_review_rounds: int = 0`.

`3` is the default: a first rejection + two genuine fix attempts is a reasonable
ceiling before "this isn't converging" is the honest verdict. Configurable via
`setup --confirm` (the run-setup confirm already merges policy fields).

### 3. Count + cap in `_apply_outcome`

In the `project_done` branch (autonomy.py:1189-1196), when the review fails **with
filed findings**:

```python
if not result.passed:
    if result.filed_findings:
        c.pm_idle = 0
        c.delivery_review_rounds += 1
        if c.delivery_review_rounds >= policy.delivery_review_round_limit:
            return LoopResult(DELIVERY_REVIEW_STALLED, c)   # truthful terminal stop
    else:
        c.pm_idle += 1
    return False
```

(`_apply_outcome` currently returns a `bool` milestone flag; the loop must be able
to turn a hit cap into a `LoopResult` stop. Implementation detail: either give
`_apply_outcome` access to the policy + a way to signal a terminal stop — a small
signature addition mirroring how `delivery_review` was threaded in — or perform the
increment/cap check at the `_apply_outcome` call site in the loop where `policy` and
the stop machinery are already in scope. Prefer the call-site check to keep
`_apply_outcome`'s `-> bool` contract.)

Reset semantics: `delivery_review_rounds` resets to `0` on a **passing** delivery
review (the run is about to finish anyway) and is **not** reset by ordinary progress
— it is a monotonic count of failed delivery rounds within the run, which is exactly
what we want to bound. (It does not reset on a checkpoint resume unless the operator
resets the run state; a resumed run that was already stalling should still stall.)

### 4. When the cap trips

`DELIVERY_REVIEW_STALLED` is a terminal, human-facing stop. The run stops with a
clear reason; the last delivery-review findings remain filed so the operator (or a
later resume with a raised limit) sees exactly what kept failing. This composes with
F152/F153/F154: once those make delivery review *actually* fail on a broken app, the
cap ensures the failing run ends truthfully rather than looping.

## Edge cases

- **A legitimately hard project** that needs >3 fix rounds: the operator raises
  `delivery_review_round_limit` via `setup --confirm --delivery-review-round-limit N`
  (add the flag to the CLI `setup` params + the run-setup confirm passthrough) or
  resumes after fixing. Default 3 is a floor for the common demo case, not a hard
  ceiling on ambition.
- **Interaction with checkpoints**: if `checkpoint_cadence` pauses the run before the
  cap, the count persists in run state across the resume (it's a `LoopCounter`
  seeded from run state on resume — confirm the counter is persisted/rehydrated like
  the others; if `LoopCounters` are rebuilt fresh each resume, persist
  `delivery_review_rounds` in run state so the cap survives a resume — otherwise a
  checkpointed livelock could reset each resume).
- **filed_findings False path** (review couldn't run / queued nothing): unchanged —
  that already increments `pm_idle` toward `NO_PROGRESS`; the round cap only counts
  *findings-filed* rejections (a real reject→fix cycle).

## Testing

`python/tests/coding/` (extend the autonomy/F146 suite):

- `test_delivery_review_rounds_increment_on_reject` — each findings-filed rejection
  bumps `delivery_review_rounds`.
- `test_stops_delivery_review_stalled_at_cap` — with `delivery_review_round_limit=2`,
  two failed rounds → loop returns `DELIVERY_REVIEW_STALLED` (not `BUDGET_EXHAUSTED`,
  not a further iteration).
- `test_passing_review_resets_and_completes` — a pass before the cap → `done`, no
  stall.
- `test_round_limit_from_dict_clamped` — `from_dict` clamps `<1` to `1` and round-trips.
- `test_filed_findings_false_still_counts_pm_idle_not_rounds` — the no-findings path
  is unchanged (pm_idle path, not the round counter).
- `test_rounds_persist_across_resume` (if counters rehydrate from run state) — a
  resumed stalling run does not reset its round count.

## Documentation

- `docs/CLI.md` setup section: document `--delivery-review-round-limit` (default 3)
  and the `delivery_review_stalled` stop reason ("delivery review kept rejecting the
  integrated result — the run stopped truthfully instead of burning budget").

## Out of scope

- Auto-raising the limit or auto-escalating to a human mid-run (a checkpoint/attention
  Problem could later surface a stall; not needed for the truthful-stop goal).
