"""SPEC-30 (Fix B) — a moot engine-filed fix task must not wedge the run.

Run 8 produced a working game but stopped planning_churn: a "Fix the failures
reported by the acceptance gate" task was filed when the gate was red, the gate
went green before a DEV worked it, the DEV blocked it as "nothing to fix", and a
blocked/human-required task cannot be auto-closed -> the completion gate refused
`done` forever. `_reconcile_moot_gate_fixes` drops such a task when NO failing
runtime evidence remains at the delivered head; a real remaining failure keeps it.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from errorta_council.coding.runner import (
    _is_engine_filed_fix_title,
    _reconcile_moot_gate_fixes,
)


class _Task(SimpleNamespace):
    pass


class _FakeStore:
    def __init__(self, tasks, test_runs) -> None:
        self._tasks = tasks
        self._test_runs = test_runs
        self.decisions: list = []

    def list_tasks(self, state: str | None = None):
        if state is None:
            return list(self._tasks)
        return [t for t in self._tasks if t.state == state]

    def list_test_runs(self):
        return list(self._test_runs)

    def update_task(self, task_id: str, **patch: Any) -> None:
        for t in self._tasks:
            if t.task_id == task_id:
                for k, v in patch.items():
                    setattr(t, k, v)

    def record_decision(self, **kw: Any) -> None:
        self.decisions.append(kw)


class _FakeWorkspace:
    def __init__(self, head: str) -> None:
        self._head = head

    def head(self) -> str:
        return self._head


def _run(head="h1", passed=True):
    return {"task_id": "gate", "head": head, "passed": passed,
            "results": [{"command_id": "acceptance", "exit_code": 0 if passed else 1}]}


def test_title_matcher():
    assert _is_engine_filed_fix_title("Fix the failures reported by the acceptance gate")
    assert _is_engine_filed_fix_title("fix delivery review findings")
    assert _is_engine_filed_fix_title("fix web artifact runtime behavior")
    # A genuine human/dev task is NOT matched.
    assert not _is_engine_filed_fix_title("Implement the physics engine")
    assert not _is_engine_filed_fix_title("Add level 5")


def test_moot_fix_dropped_when_gate_green():
    task = _Task(task_id="t1", state="blocked",
                title="Fix the failures reported by the acceptance gate")
    store = _FakeStore([task], [_run(head="h1", passed=True)])  # green at head
    _reconcile_moot_gate_fixes(store, _FakeWorkspace("h1"))
    assert task.state == "dropped", "a moot fix task must be dropped, not wedge done"
    assert any(d.get("choice") == "stale_fix_resolved" for d in store.decisions)


def test_real_failure_keeps_the_fix_task():
    task = _Task(task_id="t1", state="blocked",
                title="Fix the failures reported by the acceptance gate")
    store = _FakeStore([task], [_run(head="h1", passed=False)])  # RED at head
    _reconcile_moot_gate_fixes(store, _FakeWorkspace("h1"))
    assert task.state == "blocked", "a real remaining failure must keep the fix task"


def test_genuine_task_untouched_even_when_green():
    task = _Task(task_id="t1", state="todo", title="Implement the HUD overlay")
    store = _FakeStore([task], [_run(head="h1", passed=True)])
    _reconcile_moot_gate_fixes(store, _FakeWorkspace("h1"))
    assert task.state == "todo", "a non-engine-filed task must never be auto-dropped"
