"""SPEC-30 convergence — the PM can prune its own backlog to reach `done`.

Run 9 produced a working game but never converged: the PM over-planned (backlog
57->144) and, because the completion gate requires every open task resolved and
the plan schema had NO way to drop a task, the backlog was a one-way ratchet.
`PMPlanIntent.cancel_task_ids` + `_apply_pm_cancels` let the PM drop obsolete /
over-scoped todo/blocked tasks (never in-flight work, never a task with a live
PR), so it can prune to the DoD and claim done. A prune counts as progress.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from errorta_council.coding.runner import _apply_pm_cancels, _pm_turn_made_progress
from errorta_council.coding.schemas import PMPlanIntent


class _Task(SimpleNamespace):
    pass


class _FakeStore:
    def __init__(self, tasks, prs=None) -> None:
        self._tasks = tasks
        self._prs = prs or []
        self.decisions: list = []

    def list_tasks(self, state: str | None = None):
        return [t for t in self._tasks if state is None or t.state == state]

    def list_prs(self):
        return list(self._prs)

    def update_task(self, task_id: str, **patch: Any) -> None:
        for t in self._tasks:
            if t.task_id == task_id:
                for k, v in patch.items():
                    setattr(t, k, v)

    def record_decision(self, **kw: Any) -> None:
        self.decisions.append(kw)


def test_schema_allows_cancel_only_turn():
    # A not-done turn that ONLY prunes is legal (Spec 21 + SPEC-30).
    intent = PMPlanIntent(kind="plan", done=False, cancel_task_ids=["t1"])
    assert intent.cancel_task_ids == ["t1"]


def test_apply_cancels_drops_todo_and_blocked_only():
    tasks = [
        _Task(task_id="todo1", state="todo", title="over-scoped polish"),
        _Task(task_id="blk1", state="blocked", title="stale fix"),
        _Task(task_id="doing1", state="doing", title="in-flight work"),
        _Task(task_id="done1", state="done", title="already done"),
    ]
    store = _FakeStore(tasks)
    intent = PMPlanIntent(kind="plan", done=False,
                          cancel_task_ids=["todo1", "blk1", "doing1", "done1"])
    dropped = _apply_pm_cancels(store, intent)
    assert set(dropped) == {"todo1", "blk1"}, "only todo/blocked are prunable"
    assert tasks[0].state == "dropped" and tasks[1].state == "dropped"
    assert tasks[2].state == "doing" and tasks[3].state == "done"


def test_apply_cancels_skips_task_with_live_pr():
    tasks = [_Task(task_id="t1", state="todo", title="has an open PR")]
    prs = [{"task_id": "t1", "status": "open"}]
    store = _FakeStore(tasks, prs)
    dropped = _apply_pm_cancels(store, PMPlanIntent(
        kind="plan", done=False, cancel_task_ids=["t1"]))
    assert dropped == [], "a task with a live PR is real work, not obsolete scope"
    assert tasks[0].state == "todo"


def test_prune_only_turn_counts_as_progress():
    # A turn that dropped a task made progress (drains the backlog toward done),
    # even with no created tasks and no decisions.
    intent = PMPlanIntent(kind="plan", done=False, cancel_task_ids=["t1"])
    assert _pm_turn_made_progress(intent, [], set(), dropped=["t1"]) is True
    # ...but a turn that dropped NOTHING and created nothing is still idle.
    assert _pm_turn_made_progress(intent, [], set(), dropped=[]) is False
