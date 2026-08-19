# Slack Studio Auto-Start + Proactive Progress Stream — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A project created from Slack starts building immediately and streams its own progress back into its channel until told to stop.

**Architecture:** Seed a real `Focus` at charter→project translation so the existing `start_gate` passes by construction; reuse `adopt_project`'s existing auto-start seam in `create_project`; schedule the already-built-but-never-called `outbound.run_loop` on the bridge's asyncio loop; add run-state as a fourth outbound content source so run termination becomes visible.

**Tech Stack:** Python 3.14, FastAPI sidecar, `slack_sdk` Socket Mode, pytest.

**Spec:** `docs/superpowers/specs/2026-08-19-slack-autostart-progress-stream-design.md`

## Global Constraints

- `errorta_slack` MUST NOT import `slack_sdk` or any optional dependency at module load time. Heavy engine imports stay inside functions.
- Never log or render a raw token. Operator-facing surfaces use `secrets.mask()`.
- Any text originating outside the operator (model output, repo file content) is escaped with `render.escape_mrkdwn` before it reaches a message body. Escape BEFORE capping — escaping expands.
- Tool results are dicts, never exceptions escaping a live turn.
- Tests run: `cd python && .venv/bin/python -m pytest <paths> -q`
- Commit after each task with a message explaining WHY, not what.

---

## Slice 5a — create → run actually works

### Task 1: Seed an initial Focus at project creation

**Files:** `python/errorta_council/coding/project_factory.py`, `python/tests/coding/test_project_factory.py`

- [ ] Write failing test `test_create_from_charter_seeds_focus_from_north_star`: create via `create_project_from_charter` with no `initial_goal`; assert `store.active_focuses()` has exactly one entry whose `title` equals the charter `north_star` verbatim and whose `origin` is `"studio_charter"`.
- [ ] Write failing test `test_create_from_charter_prefers_initial_goal`: charter carries `initial_goal`; assert the Focus title is the `initial_goal`, not the north star.
- [ ] Write failing test `test_created_project_is_not_refused_by_start_gate` (**the Gap A regression**): create via the factory, then assert `next_goal.start_gate(store) is None`.
- [ ] Write failing test `test_archived_focus_project_is_still_refused` (**gate preservation**): create, archive the seeded Focus via `update_focus`, assert `start_gate` now returns a refusal string.
- [ ] Run all four; confirm they fail for the right reason (no focus seeded), not an import error.
- [ ] Implement: after `CodingWorkspace(...).setup(...)`, add the seed using `charter.get("initial_goal") or charter["north_star"]`, `store.add_focus(title=goal, body="", origin="studio_charter")`.
- [ ] Update the module docstring: state that this deliberately diverges from `wizard_create` and why, so nobody "restores" the mirror by deleting the seed.
- [ ] Run the tests; all four pass.
- [ ] Run the full `tests/coding/` suite to catch a `wizard_create` parity assertion elsewhere.
- [ ] Commit.

### Task 2: `create_project` auto-starts the run

**Files:** `python/errorta_slack/studio_tools.py`, `python/tests/slack/test_studio_tools.py`

- [ ] Write failing test `test_create_project_starts_the_run`: fake `start_run_fn`; assert it is called exactly once with `resume=False, continue_=False` and the result carries `started: True`.
- [ ] Write failing test `test_create_project_start_failure_is_reported_not_raised`: `start_run_fn` raises; assert result has `started: False`, a non-empty `start_refused`, and that the project id and channel id are still present (creation survives).
- [ ] Write failing test `test_create_project_start_is_unconditional`: no `start` arg in args at all; assert the run still starts (this is the behavioural difference from `adopt_project`).
- [ ] Run; confirm failures.
- [ ] Implement: after `deps.store.bind_channel(...)`, mirror `adopt_project`'s block but WITHOUT the `if bool(args.get("start"))` guard. Keep the `start_gate` call. Add `started` / `start_refused` to the returned dict.
- [ ] Run tests; pass.
- [ ] Commit.

### Task 3: Honest confirmation, posted into the new channel

**Files:** `python/errorta_slack/connection.py`, `python/tests/slack/test_connection.py`

- [ ] Write failing test `test_create_outcome_reports_started_with_north_star`: result dict with `started: True`; assert the rendered text names the north star and says the run started.
- [ ] Write failing test `test_create_outcome_never_claims_success_when_not_started`: `started: False, start_refused: "..."`; assert the text contains the refusal and does NOT contain a started/success phrase.
- [ ] Write failing test `test_create_outcome_posts_into_the_project_channel`: assert `poster.post_message` is called with `result["channel_id"]`, in addition to the confirmation channel.
- [ ] Write failing test `test_adopt_outcome_surfaces_start_refused`: adopt result with `start_refused` set; assert the rendered text carries it (today it falls through to the generic `verb: status.` renderer and drops it).
- [ ] Run; confirm failures.
- [ ] Implement: extend `_create_project_outcome_text`; add an `adopt_project` entry to the outcome renderer table; add the second `post_message` in `_post_studio_outcome`.
- [ ] Run `tests/slack/` in full — `connection.py` is broadly covered and this touches a shared renderer path.
- [ ] Commit.

---

## Slice 5b — the progress stream exists

### Task 4: Run state as a fourth outbound source

**Files:** `python/errorta_slack/outbound.py`, `python/tests/slack/test_outbound.py`

- [ ] Write failing test `test_run_state_item_emitted_on_transition`: run state goes `running` → `stopped`; assert exactly one item, marker `run:stopped:<ended_at>`.
- [ ] Write failing test `test_run_state_item_not_repeated`: poll twice with unchanged state; assert the second poll emits nothing.
- [ ] Write failing test `test_run_failed_emits_an_item`: status `failed` emits an item.
- [ ] Run; confirm failures.
- [ ] Implement `_run_state_items(deps, project_id)` reading `get_run_state()`; add it to `_current_items`. Marker must be stable across polls (status + ended_at, never a wall-clock read).
- [ ] Run tests; pass.
- [ ] Commit.

### Task 5: Per-channel mute, and what ignores it

**Files:** `python/errorta_slack/store.py`, `python/errorta_slack/outbound.py`, `python/errorta_slack/tools.py`, `python/tests/slack/test_store.py`, `test_outbound.py`

- [ ] Write failing test `test_updates_muted_defaults_false` and `test_set_updates_round_trips` in `test_store.py`.
- [ ] Write failing test `test_mute_suppresses_ordinary_milestones`: muted channel; a team-log item produces no post.
- [ ] Write failing test `test_mute_does_not_suppress_mandatory_events`: muted channel; a run-state item and a blocking attention signal BOTH still post.
- [ ] Run; confirm failures.
- [ ] Implement store get/set, an `is_mandatory` flag on the internal item type, and the filter in `poll_once`.
- [ ] Add the `set_updates` verb ([R] trust) to `TOOL_CATALOG` + `_VERB_IMPLS` with an `on: bool` argument.
- [ ] Run tests; pass.
- [ ] Commit.

### Task 6: Schedule the loop; seed adopt's cursor

**Files:** `python/errorta_app/slack_lifecycle.py`, `python/errorta_slack/studio_tools.py`, `python/tests/slack/test_slack_lifecycle.py`, `test_studio_tools.py`

- [ ] Write failing test `test_bridge_start_schedules_the_outbound_loop`: assert a task is scheduled on the loop.
- [ ] Write failing test `test_bridge_stop_cancels_the_outbound_loop` and `test_two_syncs_do_not_leave_two_loops`.
- [ ] Write failing test `test_adopt_seeds_the_cursor_at_bind_time`: adopt a project with existing history; assert the first `poll_once` posts nothing.
- [ ] Run; confirm failures.
- [ ] Implement the `run_coroutine_threadsafe` schedule in `_start_locked`, store the future, cancel it in `_stop_locked`.
- [ ] Implement cursor seeding in `adopt_project` (current marker set at bind time).
- [ ] Run tests; pass.
- [ ] Commit.

---

## Slice 5c — the model can actually call the tools

### Task 7: Render argument names into the TOOLS block

**Files:** `python/errorta_slack/tools.py`, `studio_tools.py`, `concierge.py`, `studio_concierge.py`, `python/tests/slack/test_catalog_canary.py`, `test_studio_catalog_canary.py`

- [ ] Write failing test `test_catalog_line_carries_args`: the rendered line for `set_next_goal` names `title` and marks it required.
- [ ] Write failing test asserting every verb with a required argument declares it in `TOOL_CATALOG` (a completeness check, so a new verb cannot silently regress).
- [ ] Run; confirm failures.
- [ ] Add `args` tuples to both catalogs; extend both renderers.
- [ ] Update `_CATALOG_LINE` in both canaries to the new shape — same commit, purpose preserved.
- [ ] Run tests; pass.
- [ ] Commit.

### Task 8: Reply reconciliation

**Files:** `python/errorta_slack/concierge.py`, `studio_concierge.py`, `python/tests/slack/test_concierge.py`

- [ ] Write failing test `test_error_result_is_not_reported_as_success`: tool result `{"status":"error"}`; assert the final reply does not claim the action was done.
- [ ] Write failing test `test_parse_failure_with_error_result_does_not_keep_optimistic_reply`.
- [ ] Run; confirm failures.
- [ ] Add the reconciliation clause to the follow-up prompt; change the parse-failure `break` to fall back to a rendered result summary when any result is an error.
- [ ] Run `tests/slack/` in full.
- [ ] Commit.

---

## Done when

- [ ] `cd python && .venv/bin/python -m pytest tests/slack tests/coding -q` is green.
- [ ] A studio-created project starts without a second message.
- [ ] Its channel receives the confirmation naming the north star, then milestones.
- [ ] Muting stops milestones and does not stop stopped/roadblock/finished.
