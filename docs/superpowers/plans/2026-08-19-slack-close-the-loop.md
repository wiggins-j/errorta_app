# Slack Task Control + Milestone-Only Stream — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An operator can resolve a `completion_blocked` run entirely from its Slack channel, and that channel carries only milestones.

**Architecture:** Three new verbs in the existing `errorta_slack.tools` catalog, each reaching the engine through `deps.ledger_factory(project_id)` exactly as the current verbs do — `list_open_tasks` reads, `cancel_task`/`unblock_task` call `update_task`, the same call the runner makes to cancel its own work. F2 is a one-condition deletion in `outbound._attention_items`.

**Tech Stack:** Python 3.14, pytest.

**Spec:** `docs/superpowers/specs/2026-08-19-slack-close-the-loop-design.md`

## Global Constraints

- `errorta_slack` MUST NOT import `slack_sdk` or any optional dependency at module load. Engine imports stay inside functions.
- A verb returns a dict; an engine exception never escapes a live turn.
- Every verb with a required argument declares it in `TOOL_CATALOG` as `("name", True, "description")` — Slice 5c's contract, enforced by `test_every_verb_with_declared_args_renders_them`.
- Task state vocabulary: terminal states are `done`, `dropped`, `merged`. Cancel means `state="dropped"` (`runner.py:1603`). Unblock means `blocked` → `todo`.
- Tests: `cd python && PYTHONPATH=. .venv/bin/python -m pytest <paths> -q`
- Commit after each task, explaining WHY.

---

### Task 1: `list_open_tasks` — make the other two callable

**Files:**
- Modify: `python/errorta_slack/tools.py` (`TOOL_CATALOG`, new impl, `_VERB_IMPLS`)
- Test: `python/tests/slack/test_tools.py`

**Interfaces:**
- Consumes: `deps.ledger_factory(project_id).list_tasks()` → `list[Task]`, each with `.task_id`, `.title`, `.state`.
- Produces: `list_open_tasks(args, *, channel_id, thread_ts, deps) -> {"tasks": [{"task_id": str, "title": str, "state": str}]}`

- [ ] **Step 1: Write the failing tests**

```python
def test_list_open_tasks_returns_id_title_state(tmp_errorta_home) -> None:
    """The regression for F1a: without a task_id in some verb's OUTPUT, the
    cancel/unblock verbs are uncallable -- the model cannot supply an argument
    it has no way to learn."""
    ledger = FakeLedger(tasks=[
        _task("t-1", "todo", "open one"),
        _task("t-2", "done", "finished one"),
    ])
    deps = tools.ToolDeps(store=store, ledger_factory=lambda pid: ledger)
    store.bind_channel("C1", "p1")

    out = tools.dispatch("list_open_tasks", {}, channel_id="C1",
                         thread_ts="t", confirmed_via=None, deps=deps)

    assert out["tasks"] == [{"task_id": "t-1", "title": "open one", "state": "todo"}]


def test_list_open_tasks_excludes_terminal_states(tmp_errorta_home) -> None:
    ledger = FakeLedger(tasks=[
        _task("t-1", "done", "a"), _task("t-2", "dropped", "b"),
        _task("t-3", "merged", "c"), _task("t-4", "blocked", "d"),
    ])
    deps = tools.ToolDeps(store=store, ledger_factory=lambda pid: ledger)
    store.bind_channel("C1", "p1")

    out = tools.dispatch("list_open_tasks", {}, channel_id="C1",
                         thread_ts="t", confirmed_via=None, deps=deps)

    assert [t["task_id"] for t in out["tasks"]] == ["t-4"]


def test_list_open_tasks_caps_at_twenty(tmp_errorta_home) -> None:
    ledger = FakeLedger(tasks=[_task(f"t-{i}", "todo", f"task {i}") for i in range(40)])
    deps = tools.ToolDeps(store=store, ledger_factory=lambda pid: ledger)
    store.bind_channel("C1", "p1")

    out = tools.dispatch("list_open_tasks", {}, channel_id="C1",
                         thread_ts="t", confirmed_via=None, deps=deps)

    assert len(out["tasks"]) == 20
```

- [ ] **Step 2: Run to verify they fail**

Run: `PYTHONPATH=. .venv/bin/python -m pytest tests/slack/test_tools.py -q -k list_open_tasks`
Expected: FAIL — `tool_not_allowed`.

- [ ] **Step 3: Implement**

```python
_TERMINAL_TASK_STATES = frozenset({"done", "dropped", "merged"})
_OPEN_TASK_CAP = 20


def list_open_tasks(args: dict[str, Any], *, channel_id: str, thread_ts: str,
                     deps: "ToolDeps") -> dict[str, Any]:
    """The project's non-terminal tasks, with their ids.

    Exists because `cancel_task`/`unblock_task` take a `task_id` and NOTHING
    the PM can see carried one: `project_status`'s "tasks" are team-log entries
    (`at`/`kind`/`member`/`message`/`role`), not task records. Without this the
    other two verbs are advertised and uncallable.
    """
    project_id = _bound_project_id(deps, channel_id)
    tasks = deps.ledger_factory(project_id).list_tasks()
    open_tasks = [t for t in tasks if str(getattr(t, "state", "")) not in _TERMINAL_TASK_STATES]
    return {"tasks": [
        {"task_id": str(t.task_id), "title": str(t.title), "state": str(t.state)}
        for t in open_tasks[:_OPEN_TASK_CAP]
    ]}
```

Catalog entry:

```python
    "list_open_tasks": {
        "trust": "R",
        "summary": (
            "List the project's still-open tasks with their ids — the ids "
            "cancel_task and unblock_task need."
        ),
    },
```

- [ ] **Step 4: Run tests** — Expected: PASS.
- [ ] **Step 5: Commit**

```bash
git add python/errorta_slack/tools.py python/tests/slack/test_tools.py
git commit -m "feat(slack): list_open_tasks — task ids the PM can actually see"
```

---

### Task 2: `cancel_task` and `unblock_task`

**Files:**
- Modify: `python/errorta_slack/tools.py`
- Test: `python/tests/slack/test_tools.py`

**Interfaces:**
- Consumes: `deps.ledger_factory(project_id).update_task(task_id, state=...)`, raises `LedgerError` on unknown id.
- Produces: `{"status": "cancelled"|"unblocked", "task_id": str}` or `{"status": "error", "detail": str}`

- [ ] **Step 1: Write the failing tests**

```python
def test_cancel_task_drops_it(tmp_errorta_home) -> None:
    ledger = FakeLedger(tasks=[_task("t-1", "todo", "open one")])
    deps = tools.ToolDeps(store=store, ledger_factory=lambda pid: ledger)
    store.bind_channel("C1", "p1")

    out = tools.dispatch("cancel_task", {"task_id": "t-1"}, channel_id="C1",
                         thread_ts="t", confirmed_via="block_actions", deps=deps)

    assert out == {"status": "cancelled", "task_id": "t-1"}
    assert ledger.updates == [("t-1", {"state": "dropped"})]


def test_cancel_task_unknown_id_is_an_error_result_not_a_raise(tmp_errorta_home) -> None:
    ledger = FakeLedger(tasks=[], raise_on_update=True)
    deps = tools.ToolDeps(store=store, ledger_factory=lambda pid: ledger)
    store.bind_channel("C1", "p1")

    out = tools.dispatch("cancel_task", {"task_id": "nope"}, channel_id="C1",
                         thread_ts="t", confirmed_via="block_actions", deps=deps)

    assert out["status"] == "error"
    assert "unknown task" in out["detail"]


def test_cancel_task_from_chat_text_only_stages(tmp_errorta_home) -> None:
    """The injection wall: [C] never executes from chat text alone."""
    ledger = FakeLedger(tasks=[_task("t-1", "todo", "open one")])
    deps = tools.ToolDeps(store=store, ledger_factory=lambda pid: ledger)
    store.bind_channel("C1", "p1")

    out = tools.dispatch("cancel_task", {"task_id": "t-1"}, channel_id="C1",
                         thread_ts="t", confirmed_via=None, deps=deps)

    assert out["status"] == "needs_confirmation"
    assert ledger.updates == []


def test_unblock_task_moves_blocked_to_todo(tmp_errorta_home) -> None:
    ledger = FakeLedger(tasks=[_task("t-1", "blocked", "stuck one")])
    deps = tools.ToolDeps(store=store, ledger_factory=lambda pid: ledger)
    store.bind_channel("C1", "p1")

    out = tools.dispatch("unblock_task", {"task_id": "t-1"}, channel_id="C1",
                         thread_ts="t", confirmed_via="block_actions", deps=deps)

    assert out == {"status": "unblocked", "task_id": "t-1"}
    assert ledger.updates == [("t-1", {"state": "todo"})]


def test_unblock_task_refuses_a_task_that_is_not_blocked(tmp_errorta_home) -> None:
    """It is an unblock, not a general state setter -- it must not be usable to
    rewind a done task back into the queue."""
    ledger = FakeLedger(tasks=[_task("t-1", "done", "finished")])
    deps = tools.ToolDeps(store=store, ledger_factory=lambda pid: ledger)
    store.bind_channel("C1", "p1")

    out = tools.dispatch("unblock_task", {"task_id": "t-1"}, channel_id="C1",
                         thread_ts="t", confirmed_via="block_actions", deps=deps)

    assert out["status"] == "error"
    assert ledger.updates == []
```

- [ ] **Step 2: Run to verify they fail** — Expected: FAIL, `tool_not_allowed`.

- [ ] **Step 3: Implement**

```python
def cancel_task(args: dict[str, Any], *, channel_id: str, thread_ts: str,
                 deps: "ToolDeps") -> dict[str, Any]:
    """Drop a task. [C] because it SUBTRACTS work -- queue_bugs is [R] only
    because it adds. `state="dropped"` is the runner's own cancel semantic
    (runner.py:1603), so a Slack cancel and an engine cancel are indistinguishable
    in the ledger."""
    from errorta_council.coding.ledger import LedgerError  # noqa: PLC0415

    task_id = str(args.get("task_id") or "").strip()
    if not task_id:
        return {"status": "error", "detail": "task_id is required"}
    try:
        deps.ledger_factory(_bound_project_id(deps, channel_id)).update_task(
            task_id, state="dropped")
    except LedgerError:
        return {"status": "error", "detail": f"unknown task: {task_id}"}
    return {"status": "cancelled", "task_id": task_id}


def unblock_task(args: dict[str, Any], *, channel_id: str, thread_ts: str,
                  deps: "ToolDeps") -> dict[str, Any]:
    """Move a BLOCKED task back to todo -- the 'human-required' half of the
    completion gate. Refuses any other state: this is an unblock, not a general
    state setter, and must not be usable to rewind finished work."""
    from errorta_council.coding.ledger import LedgerError  # noqa: PLC0415

    task_id = str(args.get("task_id") or "").strip()
    if not task_id:
        return {"status": "error", "detail": "task_id is required"}
    ledger = deps.ledger_factory(_bound_project_id(deps, channel_id))
    current = next(
        (t for t in ledger.list_tasks() if str(t.task_id) == task_id), None)
    if current is None:
        return {"status": "error", "detail": f"unknown task: {task_id}"}
    if str(current.state) != "blocked":
        return {"status": "error",
                "detail": f"task {task_id} is {current.state}, not blocked"}
    try:
        ledger.update_task(task_id, state="todo")
    except LedgerError:
        return {"status": "error", "detail": f"unknown task: {task_id}"}
    return {"status": "unblocked", "task_id": task_id}
```

Catalog entries:

```python
    "cancel_task": {
        "trust": "C",
        "args": (("task_id", True, "id from list_open_tasks"),),
        "summary": "Cancel (drop) an open task so a blocked run can complete.",
    },
    "unblock_task": {
        "trust": "C",
        "args": (("task_id", True, "id from list_open_tasks"),),
        "summary": "Move a blocked task back to todo so the team can pick it up.",
    },
```

- [ ] **Step 4: Run tests** — Expected: PASS.
- [ ] **Step 5: Commit**

```bash
git add python/errorta_slack/tools.py python/tests/slack/test_tools.py
git commit -m "feat(slack): cancel and unblock a task from the channel"
```

---

### Task 3: The anti-dead-verb canary

**Files:**
- Test: `python/tests/slack/test_catalog_canary.py`

- [ ] **Step 1: Write the failing test**

```python
def test_every_required_arg_is_obtainable_from_some_read_verb() -> None:
    """F1a's regression, generalised: a verb whose required argument appears in
    no [R] verb's output is UNCALLABLE -- the model cannot invent a task_id or a
    change_id. list_open_tasks exists precisely because cancel_task's task_id
    was unobtainable. This fails CI instead of failing live in Slack."""
    from errorta_slack import tools

    # Keys any [R] verb puts into its result, by inspection of the impls.
    obtainable = {
        "task_id": "list_open_tasks",
        "project_id": "list_projects",
        "change_id": "project_status",
        "limit": None, "on": None, "question": None, "bugs": None,
        "title": None, "body": None, "north_star": None,
        "definition_of_done": None, "role_routes": None, "amount": None,
        "reason": None, "decision": None,
    }
    for verb, spec in tools.TOOL_CATALOG.items():
        for name, required, _desc in spec.get("args", ()):
            if not required:
                continue
            assert name in obtainable, (
                f"{verb} requires {name!r}, which no verb's output provides — "
                "the model cannot supply it, so the verb is uncallable"
            )
```

- [ ] **Step 2: Run** — Expected: PASS once Tasks 1-2 land (it is a guard, not a driver).
- [ ] **Step 3: Commit**

```bash
git add python/tests/slack/test_catalog_canary.py
git commit -m "test(slack): a verb whose required arg is unobtainable is uncallable"
```

---

### Task 4: Drop non-blocking attention items from the stream

**Files:**
- Modify: `python/errorta_slack/outbound.py` (`_attention_items`)
- Test: `python/tests/slack/test_outbound.py`

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.asyncio
async def test_non_blocking_alert_produces_no_message() -> None:
    """28 reviewer nitpicks became 28 Slack messages on the live run. The team
    log already posts 'reviewed an artifact (5 finding(s))' for the same round,
    so the channel loses nothing."""
    store.advance_cursor("C-nb", "")
    deps = _deps(signals=[
        _signal("s1", created_at="2026-01-01T00:00:00",
                title="Rounding method not specified", blocking=False)])
    poster = SyncFakePoster()

    assert outbound.poll_once("C-nb", "p1", deps=deps, poster=poster) == []
    assert poster.messages == []


@pytest.mark.asyncio
async def test_blocking_signal_still_posts_as_a_decision() -> None:
    store.advance_cursor("C-b", "")
    deps = _deps(signals=[
        _signal("s2", created_at="2026-01-01T00:00:00",
                title="Run can't complete", blocking=True)])
    poster = SyncFakePoster()

    assert outbound.poll_once("C-b", "p1", deps=deps, poster=poster) == ["attn:s2"]
    assert poster.messages[0]["blocks"]


@pytest.mark.asyncio
async def test_blocking_signal_still_ignores_the_mute() -> None:
    store.advance_cursor("C-bm", "")
    store.set_updates("C-bm", enabled=False)
    deps = _deps(signals=[
        _signal("s3", created_at="2026-01-01T00:00:00",
                title="Run can't complete", blocking=True)])
    poster = SyncFakePoster()

    assert outbound.poll_once("C-bm", "p1", deps=deps, poster=poster) == ["attn:s3"]
```

- [ ] **Step 2: Run** — Expected: the first test FAILS (the alert posts today); the other two PASS.

- [ ] **Step 3: Implement — a one-condition deletion**

```python
    for sig in deps.attention_list_open(project_id, store=ledger_store):
        blocking = bool(_get(sig, "blocking", False))
        # Non-blocking signals are NOT channel messages. The live run produced
        # 28 reviewer nitpicks on one spec; each would have been its own Slack
        # message, against an approved design of 6-12 milestone messages. The
        # team log already reports "reviewed an artifact (N finding(s))" for the
        # same review round, and the detail stays in the app where it is
        # actionable. Blocking signals are untouched -- they carry a button the
        # run is waiting on.
        if not blocking:
            continue
```

(Delete the now-dead `kind="decision" if blocking else "fyi"` ternary; the item is always a decision.)

- [ ] **Step 4: Run the full slack suite** — `PYTHONPATH=. .venv/bin/python -m pytest tests/slack -q`. Expect pre-existing tests that assert non-blocking alerts post to now fail; update them to assert the new contract, since the contract deliberately changed.
- [ ] **Step 5: Commit**

```bash
git add python/errorta_slack/outbound.py python/tests/slack/test_outbound.py
git commit -m "fix(slack): reviewer nitpicks are not milestones"
```

---

## Done when

- [ ] `PYTHONPATH=. .venv/bin/python -m pytest tests -q` is green.
- [ ] `cancel_task` + `unblock_task` can clear the two tasks that blocked the live run, using ids from `list_open_tasks`.
- [ ] A non-blocking alert posts nothing; a blocking one still posts a button, muted or not.
