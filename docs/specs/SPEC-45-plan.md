# SPEC-45 — capability-blocked → auto-unblock: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop deleting capability-refused tasks — persist them as `blocked (missing_capability:…)`, auto-unblock them the moment the gate opens, and surface the reason so an operator can see why a task is waiting.

**Architecture:** Three seams. (1) A machine-readable **reason foundation** — a reason-code vocabulary + a structured `extra` blob on drop/refuse decisions — shared with [SPEC-46](SPEC-46-drop-count-quarantine.md) and *owned here*. (2) The plan-time capability refusal in `_materialize_pm_tasks` / `control_actions.create_task` changes from "record decision + `continue`" (task never persisted) to "persist as `blocked`", and a per-turn re-evaluation pass unblocks it when `gate_state.gate_available(store)` flips true. (3) Dedupe includes capability-blocked tasks so the PM cannot spawn duplicates while one waits, and the CLI renderers show the reason.

**Tech Stack:** Python 3, `errorta_council.coding` package, `pytest`. CLI renderers use `rich`. No new dependencies.

## Global Constraints

- The "capability" gated at the refusal site is the **execution gate**: the predicate is `gate_state.gate_available(store)` (`gate_state.py:40`). The canonical reason string is `missing_capability:execution_gate`.
- **Additive, no schema migration.** New task fields ride in `Task._extras` (round-trips via `to_dict`/`from_dict`, `ledger.py:423`); new decision data rides in the decision `extra` blob. Older ledger rows must stay readable.
- **Best-effort, never raise into the loop.** Every new read wraps its ledger access so a hiccup degrades the feature, never 500s a route or crashes a turn (match the `# noqa: BLE001` idiom already in these files).
- The general dedupe exemption for `done`/`dropped`/`blocked` (`task_dedupe.OPEN_STATES`, `task_dedupe.py:29`) stays intact for regressions — only capability-blocked tasks become suppressing.
- Tests live in `python/tests/coding/`; construct a store with `LedgerStore(name, root=tmp_path)` then `create_project(north_star="n", definition_of_done="d", target="new", repo_path=None)`.

---

### Task 1: Reason-code vocabulary + structured reason on the capability refusal

Owns the shared foundation's data half. Introduce the reason-code constants and make the capability-refusal decision carry a machine-readable reason, without yet changing task persistence.

**Files:**
- Create: `python/errorta_council/coding/drop_reasons.py`
- Modify: `python/errorta_council/coding/runner.py:3419-3427` (the refuse branch in `_materialize_pm_tasks`)
- Test: `python/tests/coding/test_spec45_capability_reblock.py`

**Interfaces:**
- Produces: `drop_reasons.MISSING_CAPABILITY = "missing_capability"`, `OVER_SCOPED = "over_scoped"`, `DEPENDENCY_UNMET = "dependency_unmet"`, `PM_PRUNED = "pm_pruned"`, `OTHER = "other"`; `drop_reasons.ALL: frozenset[str]`; helper `drop_reasons.reason_blob(code: str, detail: str = "", capability: str | None = None) -> dict[str, Any]` returning `{"reason_code": code, "reason_detail": detail, "capability": capability}`.
- Consumed by: Task 2/4 here, and all of SPEC-46.

- [ ] **Step 1: Write the failing test**

```python
# python/tests/coding/test_spec45_capability_reblock.py
from __future__ import annotations

from pathlib import Path

from errorta_council.coding import drop_reasons
from errorta_council.coding.ledger import LedgerStore


def _store(tmp_path: Path, name: str = "p45") -> LedgerStore:
    s = LedgerStore(name, root=tmp_path)
    s.create_project(north_star="n", definition_of_done="d", target="new",
                     repo_path=None)
    return s


def test_reason_blob_shape():
    blob = drop_reasons.reason_blob(
        drop_reasons.MISSING_CAPABILITY, detail="no executor", capability="execution_gate")
    assert blob == {"reason_code": "missing_capability",
                    "reason_detail": "no executor",
                    "capability": "execution_gate"}
    assert drop_reasons.MISSING_CAPABILITY in drop_reasons.ALL
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && python -m pytest tests/coding/test_spec45_capability_reblock.py::test_reason_blob_shape -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'errorta_council.coding.drop_reasons'`

- [ ] **Step 3: Write the vocabulary module**

```python
# python/errorta_council/coding/drop_reasons.py
"""SPEC-45/46 — the shared machine-readable drop/refuse reason vocabulary.

Every site that drops or refuses a task writes one of these codes into the
decision ``extra`` blob (and mirrors it to ``Task.reason_summary``) so the CLI
can render *why* a task left the backlog instead of a hard-coded prose string.
"""
from __future__ import annotations

from typing import Any

MISSING_CAPABILITY = "missing_capability"   # no role/gate can produce the evidence
OVER_SCOPED = "over_scoped"                  # PM pruned obsolete / over-planned scope
DEPENDENCY_UNMET = "dependency_unmet"        # a prerequisite is not satisfied
PM_PRUNED = "pm_pruned"                       # PM explicitly cancelled the task id
OTHER = "other"

ALL = frozenset({
    MISSING_CAPABILITY, OVER_SCOPED, DEPENDENCY_UNMET, PM_PRUNED, OTHER,
})


def reason_blob(code: str, detail: str = "",
                capability: str | None = None) -> dict[str, Any]:
    """The structured reason recorded on a drop/refuse decision's ``extra``."""
    return {
        "reason_code": code if code in ALL else OTHER,
        "reason_detail": str(detail or ""),
        "capability": capability,
    }
```

- [ ] **Step 4: Thread the reason into the refusal decision**

In `runner.py`, the refuse branch (currently `runner.py:3419-3427`) records `choice="task_requires_absent_capability"` with only `extra={"planned_title": planned.title}`. Add the reason blob. At the top of `runner.py` add the import beside the other `from . import` lines: `from . import drop_reasons as _drop_reasons`. Then change the `store.record_decision(...)` call in the refuse branch to:

```python
                store.record_decision(
                    title=f"task refused (no executor): {planned.title}",
                    context="capability_lint",
                    choice="task_requires_absent_capability",
                    rationale=(f"{planned.title!r} demands execution evidence, but "
                               "no role can run a command and no acceptance gate "
                               "exists to produce it; refused at planning time"),
                    extra={
                        "planned_title": planned.title,
                        **_drop_reasons.reason_blob(
                            _drop_reasons.MISSING_CAPABILITY,
                            detail="no executor and no acceptance gate",
                            capability="execution_gate"),
                    })
```

- [ ] **Step 5: Add a test that the refusal decision carries the reason**

```python
# append to test_spec45_capability_reblock.py
from errorta_council.coding import drop_reasons
from errorta_council.coding.runner import _materialize_pm_tasks


class _Planned:
    def __init__(self, title, detail=""):
        self.title = title
        self.detail = detail
        self.depends_on = []
        self.task_type = "implementation"
        self.difficulty_tier = "mid"
        self.preferred_member_id = ""
        self.preferred_route_id = ""
        self.assignment_rationale = ""


class _Intent:
    def __init__(self, tasks):
        self.tasks = tasks


def _refused_decision(store):
    for d in store.list_decisions():
        if d.get("choice") == "task_requires_absent_capability":
            return d
    return None


def test_capability_refusal_decision_carries_reason(tmp_path):
    store = _store(tmp_path)
    # An execution-imperative task with no gate available -> refused.
    _materialize_pm_tasks(store, _Intent([
        _Planned("run the integration suite and report the failing cases")]))
    dec = _refused_decision(store)
    assert dec is not None
    assert dec["reason_code"] == drop_reasons.MISSING_CAPABILITY
    assert dec["capability"] == "execution_gate"
```

Note: confirm the decision-list accessor name — the store exposes decisions via `list_decisions()` (used across `tests/coding`); if the local helper differs, match the sibling dedupe test's accessor.

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd python && python -m pytest tests/coding/test_spec45_capability_reblock.py -v`
Expected: PASS (both tests)

- [ ] **Step 7: Commit**

```bash
git add python/errorta_council/coding/drop_reasons.py python/errorta_council/coding/runner.py python/tests/coding/test_spec45_capability_reblock.py
git commit -m "feat(coding): SPEC-45 — reason-code vocabulary + machine-readable capability-refusal reason"
```

---

### Task 2: Persist the capability-refused task as `blocked` instead of dropping it

**Files:**
- Modify: `python/errorta_council/coding/runner.py:3416-3427` (refuse branch of `_materialize_pm_tasks`)
- Modify: `python/errorta_council/coding/control_actions.py:376-388` (the mirror lint in `create_task`)
- Test: `python/tests/coding/test_spec45_capability_reblock.py`

**Interfaces:**
- Produces: a persisted `Task` with `state="blocked"`, `_extras["blocked_reason"] == "missing_capability:execution_gate"`, and `reason_summary` set. Consumed by Task 3 (dedupe) and Task 4 (auto-unblock).
- Consumes: `drop_reasons` from Task 1.

- [ ] **Step 1: Write the failing test**

```python
# append to test_spec45_capability_reblock.py
def _tasks_by_state(store, state):
    return [t for t in store.list_tasks() if t.state == state]


def test_refused_task_is_persisted_blocked(tmp_path):
    store = _store(tmp_path)
    _materialize_pm_tasks(store, _Intent([
        _Planned("run the integration suite and report the failing cases")]))
    blocked = _tasks_by_state(store, "blocked")
    assert len(blocked) == 1
    assert blocked[0]._extras.get("blocked_reason") == "missing_capability:execution_gate"
    assert "execution" in blocked[0].reason_summary.lower() or blocked[0].reason_summary
    # It must NOT have been dropped or silently discarded.
    assert not _tasks_by_state(store, "dropped")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && python -m pytest tests/coding/test_spec45_capability_reblock.py::test_refused_task_is_persisted_blocked -v`
Expected: FAIL — `len(blocked) == 1` is `0` (today the task is never created).

- [ ] **Step 3: Replace the refuse-and-continue with persist-as-blocked**

In `_materialize_pm_tasks`, the `else:` refuse branch (`runner.py:3416`) currently sets `title_to_id[planned.title] = ""` and `continue`s with no task. Replace the branch body so it creates the task, then blocks it:

```python
            else:
                # SPEC-45: a capability gap is a PAUSE, not a deletion. Persist the
                # task as `blocked (missing_capability:…)` so it (a) survives to be
                # re-dispatched when the gate opens (the auto-unblock pass) and (b)
                # is visible in board/status. Still record the reason-bearing
                # decision so the PM's `_capability_refusal_note` prompt keeps firing.
                blocked_task = store.add_task(
                    title=planned.title, role="dev", detail=planned.detail,
                    parent_task_id=parent_task.task_id if parent_task is not None else None,
                    task_type=planned.task_type,
                    difficulty_tier=planned.difficulty_tier,
                    preferred_member_id=planned.preferred_member_id,
                    preferred_route_id=planned.preferred_route_id,
                    assignment_rationale=planned.assignment_rationale,
                    target_files=list(paths) or None,
                )
                store.update_task(
                    blocked_task.task_id, state="blocked",
                    blocked_reason="missing_capability:execution_gate",
                    reason_summary=("needs execution evidence but no gate exists yet; "
                                    "waiting on the execution capability"))
                title_to_id[planned.title] = blocked_task.task_id
                store.record_decision(
                    title=f"task blocked (no executor): {planned.title}",
                    context="capability_lint",
                    choice="task_requires_absent_capability",
                    rationale=(f"{planned.title!r} demands execution evidence, but "
                               "no role can run a command and no acceptance gate "
                               "exists to produce it; blocked at planning time"),
                    related_task_ids=[blocked_task.task_id],
                    extra={
                        "planned_title": planned.title,
                        **_drop_reasons.reason_blob(
                            _drop_reasons.MISSING_CAPABILITY,
                            detail="no executor and no acceptance gate",
                            capability="execution_gate"),
                    })
                continue
```

Note: `update_task(**patch)` routes any key that is not a declared `Task` field into `_extras` (`ledger.py:764-808` builds `cur = prior.to_dict(); cur.update(patch)` and `Task.from_dict` splits unknowns back into `_extras`), so `blocked_reason=...` lands in `_extras` with no schema change. Set `title_to_id` to the real id (not `""`) so a sibling's `depends_on` resolves onto the blocked task instead of being dropped.

- [ ] **Step 4: Mirror the change in `control_actions.create_task`**

`control_actions.py:376-388` raises `ControlActionError("task_requires_absent_capability", …)` for the same lint on the interactive control path. Replace the raise with the same persist-as-blocked behavior so an operator `create_task` of an execution task with no gate yields a blocked task, not an error. Read the surrounding function first; keep its return contract (return the created `Task`). Record the reason-bearing decision exactly as in Step 3.

- [ ] **Step 5: Run tests**

Run: `cd python && python -m pytest tests/coding/test_spec45_capability_reblock.py -v`
Expected: PASS (all tests so far)

- [ ] **Step 6: Run the SPEC-15 regression suite (this changes its refusal path)**

Run: `cd python && python -m pytest tests/coding/test_spec15_capability_aware.py -v`
Expected: PASS, or a small number of assertions that asserted "no task created" — update those to assert "task is blocked with `missing_capability` reason" (the behavior SPEC-45 intentionally changes). Do not weaken any assertion about *authoring* tasks (non-execution) staying untouched.

- [ ] **Step 7: Commit**

```bash
git add python/errorta_council/coding/runner.py python/errorta_council/coding/control_actions.py python/tests/coding/test_spec45_capability_reblock.py python/tests/coding/test_spec15_capability_aware.py
git commit -m "feat(coding): SPEC-45 — persist capability-refused tasks as blocked, not dropped"
```

---

### Task 3: Dedupe suppresses re-creates of a capability-blocked task

Without this, the PM re-proposes the same execution task every plan turn (blocked is exempt from dedupe), spawning duplicate blocked tasks — and feeding SPEC-46's churn. Narrow the exemption for capability-waiting tasks only.

**Files:**
- Modify: `python/errorta_council/coding/task_dedupe.py:127-141` (`build_open_index`)
- Test: `python/tests/coding/test_task_dedupe.py`

**Interfaces:**
- Consumes: a Task carrying `_extras["blocked_reason"]` starting with `missing_capability:`.
- Produces: such tasks appear in the open index, so `find_duplicate` matches a re-proposal.

- [ ] **Step 1: Write the failing test**

```python
# append to python/tests/coding/test_task_dedupe.py
from errorta_council.coding.ledger import Task


def test_capability_blocked_task_suppresses_recreate():
    waiting = Task(
        task_id="t-cap", title="run the integration suite and report",
        role="dev", state="blocked",
        _extras={"blocked_reason": "missing_capability:execution_gate"})
    index = task_dedupe.build_open_index([waiting])
    assert any(e.task_id == "t-cap" for e in index)
    match = task_dedupe.find_duplicate(
        index, title="run the integration suite and report", role="dev", paths=[])
    assert match is not None and match.task_id == "t-cap"


def test_plain_blocked_task_still_exempt():
    # A normal blocked task (regression candidate) stays OUT of the index.
    plain = Task(task_id="t-b", title="add pagination", role="dev", state="blocked")
    index = task_dedupe.build_open_index([plain])
    assert all(e.task_id != "t-b" for e in index)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd python && python -m pytest tests/coding/test_task_dedupe.py::test_capability_blocked_task_suppresses_recreate -v`
Expected: FAIL — the capability-blocked task is not in the index (blocked is excluded).

- [ ] **Step 3: Include capability-blocked tasks in the open index**

In `task_dedupe.build_open_index` (`task_dedupe.py:127-141`), replace the state filter so a `blocked` task that carries a `missing_capability` reason counts as open:

```python
def _is_capability_waiting(task: Any) -> bool:
    """A `blocked` task waiting on a capability is still 'live' for dedupe: it will
    auto-unblock, so a re-proposal is a duplicate, not a regression re-open."""
    if str(getattr(task, "state", "") or "") != "blocked":
        return False
    extras = getattr(task, "_extras", {}) or {}
    return str(extras.get("blocked_reason", "") or "").startswith("missing_capability:")


def build_open_index(tasks: Iterable[Any]) -> list[OpenTask]:
    """Project the OPEN tasks of a backlog into comparison form. Closed tasks are
    dropped here, which is what makes "duplicate of a done task" legal.

    SPEC-45: a capability-waiting `blocked` task is also projected — it is not a
    finished-work regression candidate but pending work that will auto-unblock, so
    a re-proposal must be suppressed, not treated as a new job."""
    index: list[OpenTask] = []
    for task in tasks:
        state = str(getattr(task, "state", "") or "")
        if state not in OPEN_STATES and not _is_capability_waiting(task):
            continue
        index.append(OpenTask(
            task_id=str(getattr(task, "task_id", "") or ""),
            title=str(getattr(task, "title", "") or ""),
            role=str(getattr(task, "role", "") or ""),
            tokens=normalized_tokens(getattr(task, "title", "")),
            paths=task_paths(task),
        ))
    return index
```

- [ ] **Step 4: Run tests**

Run: `cd python && python -m pytest tests/coding/test_task_dedupe.py -v`
Expected: PASS (new + existing dedupe tests)

- [ ] **Step 5: Commit**

```bash
git add python/errorta_council/coding/task_dedupe.py python/tests/coding/test_task_dedupe.py
git commit -m "feat(coding): SPEC-45 — capability-blocked tasks suppress re-creates in dedupe"
```

---

### Task 4: Per-turn auto-unblock pass

When `gate_state.gate_available(store)` becomes true, move capability-blocked tasks back to `todo`, record a `capability_unblocked` decision, and resolve the standing capability alert.

**Files:**
- Modify: `python/errorta_council/coding/runner.py` (add helper `_reeval_capability_blocked`; call it at run start after `reclaim_stranded_inflight` at `runner.py:8419-8420` and once per loop iteration before task selection)
- Test: `python/tests/coding/test_spec45_capability_reblock.py`

**Interfaces:**
- Produces: `_reeval_capability_blocked(store) -> list[str]` — the ids unblocked this call (empty when the gate is still closed). Idempotent: a second call after an unblock returns `[]`.
- Consumes: `gate_state.gate_available` (`gate_state.py:40`), `store.update_task`, `store.record_decision`, `attention.resolve_closed_capability` (`attention.py:622`).

- [ ] **Step 1: Write the failing test**

```python
# append to test_spec45_capability_reblock.py
from errorta_council.coding.runner import _reeval_capability_blocked


def test_auto_unblock_when_gate_appears(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _materialize_pm_tasks(store, _Intent([
        _Planned("run the integration suite and report the failing cases")]))
    assert len(_tasks_by_state(store, "blocked")) == 1

    # Gate still closed -> no unblock.
    from errorta_council.coding import gate_state
    monkeypatch.setattr(gate_state, "gate_available", lambda s: False)
    assert _reeval_capability_blocked(store) == []
    assert len(_tasks_by_state(store, "blocked")) == 1

    # Gate now available -> the task returns to todo, a decision is recorded.
    monkeypatch.setattr(gate_state, "gate_available", lambda s: True)
    unblocked = _reeval_capability_blocked(store)
    assert len(unblocked) == 1
    assert len(_tasks_by_state(store, "todo")) == 1
    assert len(_tasks_by_state(store, "blocked")) == 0
    assert any(d.get("choice") == "capability_unblocked" for d in store.list_decisions())
    # Idempotent: nothing left to unblock.
    assert _reeval_capability_blocked(store) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && python -m pytest tests/coding/test_spec45_capability_reblock.py::test_auto_unblock_when_gate_appears -v`
Expected: FAIL — `ImportError: cannot import name '_reeval_capability_blocked'`

- [ ] **Step 3: Implement the pass**

Add near `_materialize_pm_tasks` in `runner.py`:

```python
def _reeval_capability_blocked(store: LedgerStore) -> list[str]:
    """SPEC-45: re-dispatch tasks blocked for a now-satisfied capability.

    The gate predicate (`gate_state.gate_available`) is re-read live; when it flips
    true, every task blocked on `missing_capability:<cap>` returns to `todo` so the
    scheduler picks it up — no operator interjection. Best-effort and idempotent."""
    try:
        if not _gate_state.gate_available(store):
            return []
        blocked = [t for t in store.list_tasks(state="blocked")
                   if str((t._extras or {}).get("blocked_reason", "") or "")
                   .startswith("missing_capability:")]
    except Exception:  # noqa: BLE001 — a read failure means "unblock nothing"
        return []
    unblocked: list[str] = []
    for task in blocked:
        try:
            store.update_task(task.task_id, state="todo", blocked_reason="",
                              reason_summary="")
            store.record_decision(
                title=f"capability now available: {task.title}",
                context="capability_lint", choice="capability_unblocked",
                rationale=("the execution gate is now available; the task blocked "
                           "for it is re-dispatched"),
                related_task_ids=[task.task_id],
                extra=_drop_reasons.reason_blob(
                    _drop_reasons.MISSING_CAPABILITY, detail="gate now available",
                    capability="execution_gate"))
            unblocked.append(task.task_id)
        except Exception:  # noqa: BLE001 — best-effort per task
            pass
    if unblocked:
        try:
            attention.resolve_closed_capability(
                store.project_id, "dev", "execution_gate", store=store)
        except Exception:  # noqa: BLE001 — resolution is advisory
            pass
    return unblocked
```

- [ ] **Step 4: Wire it into the run lifecycle**

In `CodingRunner.run` (`runner.py:8401`), immediately after `reclaim_stranded_inflight(self.store, reason="run_start")` (`runner.py:8420`) add:

```python
        # SPEC-45: a capability enabled between runs (or mid-run) re-dispatches the
        # tasks that were blocked waiting for it — on start and every iteration.
        _reeval_capability_blocked(self.store)
```

Then find the per-iteration turn boundary (the loop body that selects/executes a turn — locate it by searching for the `_materialize_pm_tasks`/dispatch dispatch within `run`) and call `_reeval_capability_blocked(self.store)` once at the top of each iteration, before task selection, guarded so it never raises into the loop. Read the loop body first and match its existing per-iteration best-effort calls.

- [ ] **Step 5: Run tests**

Run: `cd python && python -m pytest tests/coding/test_spec45_capability_reblock.py -v`
Expected: PASS (all)

- [ ] **Step 6: Commit**

```bash
git add python/errorta_council/coding/runner.py python/tests/coding/test_spec45_capability_reblock.py
git commit -m "feat(coding): SPEC-45 — per-turn auto-unblock of capability-blocked tasks"
```

---

### Task 5: Surface the reason in decisions / board / status

**Files:**
- Modify: `python/errorta_cli/render/decisions.py` (render `reason_code`/`reason_detail` for drop/refuse choices)
- Modify: `python/errorta_cli/render/board.py` (show the blocked reason)
- Modify: `python/errorta_app/routes/coding.py:2900-2915` (`_run_backlog_shape` — add `blocked_on_capability`)
- Modify: `python/errorta_cli/render/status.py:172-180` (render the rollup)
- Test: `python/tests/coding/test_spec45_render.py`

**Interfaces:**
- Consumes: decision dicts with `reason_code`; task dicts with `blocked_reason`/`reason_summary`; `run.backlog` dict.
- Produces: `_run_backlog_shape` returns `{"todo": N, "dispatchable": M, "blocked_on_capability": K}`.

- [ ] **Step 1: Write the failing render tests**

```python
# python/tests/coding/test_spec45_render.py
from errorta_cli.render.decisions import render_decisions
from errorta_cli.render.board import render_board


def test_decisions_render_reason_code():
    payload = {"decisions": [{
        "at": "2026-08-07T00:00:00Z", "choice": "task_requires_absent_capability",
        "title": "task blocked (no executor): run suite",
        "reason_code": "missing_capability", "capability": "execution_gate"}]}
    out = render_decisions(payload, None)
    assert "missing_capability" in out


def test_board_renders_blocked_reason():
    payload = {"tasks": [{
        "task_id": "t1", "title": "run suite", "role": "dev", "state": "blocked",
        "blocked_reason": "missing_capability:execution_gate"}]}
    out = render_board(payload, None)
    assert "run suite" in out
    assert "capability" in out.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd python && python -m pytest tests/coding/test_spec45_render.py -v`
Expected: FAIL — reason not present in output.

- [ ] **Step 3: Render the reason in `decisions.py`**

Add a fourth column that shows the reason for reason-bearing choices. Replace the table build in `render_decisions`:

```python
    table.add_column("time", style="cli.muted", no_wrap=True)
    table.add_column("choice", style="cli.key", no_wrap=True)
    table.add_column("title")
    table.add_column("reason", style="cli.muted", no_wrap=True)
    _REASON_CHOICES = {
        "task_requires_absent_capability", "pm_task_cancelled", "task_quarantined",
        "capability_unblocked",
    }
    for d in decisions:
        choice = str(d.get("choice") or "")
        reason = ""
        if choice in _REASON_CHOICES:
            code = str(d.get("reason_code") or "")
            cap = str(d.get("capability") or "")
            reason = f"{code}:{cap}" if cap else code
        table.add_row(ts(d.get("at")), choice, str(d.get("title") or ""), reason)
```

Update the module docstring line that says "never surface the free-form `extra` blob" — SPEC-45 surfaces the enumerated `reason_code`/`reason_detail`/`capability` keys (still not the arbitrary rest of `extra`).

- [ ] **Step 4: Render the blocked reason in `board.py`**

In `render_board`, when building each blocked item's body, append its reason. Change the per-item append loop (`board.py:66-70`) so a blocked task shows its reason under the title:

```python
        for i, t in enumerate(items):
            if i:
                body.append("\n")
            body.append("• ", style=_STATE_STYLE.get(col, "white"))
            body.append(truncate(t.get("title"), 28))
            reason = str(t.get("blocked_reason") or t.get("reason_summary") or "")
            if col == "blocked" and reason:
                body.append(f"\n  ↳ {truncate(reason, 26)}", style="cli.muted")
```

- [ ] **Step 5: Add the capability count to `_run_backlog_shape` and render it**

In `routes/coding.py` `_run_backlog_shape` (`:2900`), add the count before the return:

```python
        blocked_cap = sum(
            1 for t in store.list_tasks(state="blocked")
            if str((getattr(t, "_extras", {}) or {}).get("blocked_reason", "") or "")
            .startswith("missing_capability:"))
        return {"todo": todo_n, "dispatchable": len(seen),
                "blocked_on_capability": blocked_cap}
```

In `status.py`, after the `todo:` line (`status.py:180`) add:

```python
        blocked_cap = backlog.get("blocked_on_capability")
        if blocked_cap:
            lines.append(Text(
                f"blocked: {blocked_cap} on a missing capability (auto-retry when enabled)",
                style="cli.warn"))
```

- [ ] **Step 6: Run tests**

Run: `cd python && python -m pytest tests/coding/test_spec45_render.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add python/errorta_cli/render/decisions.py python/errorta_cli/render/board.py python/errorta_app/routes/coding.py python/errorta_cli/render/status.py python/tests/coding/test_spec45_render.py
git commit -m "feat(cli): SPEC-45 — surface drop/refuse reason in decisions, board, and status"
```

---

### Task 6: Full-suite regression + spec close-out

- [ ] **Step 1: Run the coding suite**

Run: `cd python && python -m pytest tests/coding -q`
Expected: PASS. Investigate any failure that asserts the old refuse-and-drop behavior; update it to the SPEC-45 blocked-and-unblock behavior (never weaken a genuine invariant).

- [ ] **Step 2: Flip the spec Status to landed**

Edit `docs/specs/SPEC-45-capability-blocked-auto-unblock.md` — change `**Status:** proposed` to `**Status:** landed (merged in PR #…)` after the PR merges.

- [ ] **Step 3: Commit**

```bash
git add docs/specs/SPEC-45-capability-blocked-auto-unblock.md
git commit -m "docs(spec): SPEC-45 landed"
```

---

## Self-Review

- **Spec coverage:** §Shared foundation → Task 1 (vocab + reason blob) & Task 5 (rendering); "persist instead of refuse" → Task 2; auto-unblock pass → Task 4; dedupe interaction → Task 3; surfacing (board/status/decisions) → Task 5. All spec sections map to a task.
- **Type consistency:** `_reeval_capability_blocked(store) -> list[str]`, `drop_reasons.reason_blob(...) -> dict`, and the `blocked_reason` string `missing_capability:execution_gate` are used identically in Tasks 2/3/4/5.
- **Known verification points flagged inline:** the store decision accessor (`list_decisions()`) and the exact per-iteration call site in `CodingRunner.run` must be confirmed against the code when implementing (both noted in-task), because those two are the only names not quoted verbatim from a file already read.
