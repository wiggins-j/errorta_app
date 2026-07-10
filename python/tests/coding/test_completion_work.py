"""F128 slice 1 — pending_completion_work is the read-only backlog truth source.

Non-terminal tasks + open PRs block completion; terminal ones don't; a read
error fails closed (non-empty sentinel)."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from errorta_council.coding.completion import (
    OpenItem,
    pending_completion_work,
    summarize_open_items,
)
from errorta_council.coding.ledger import LedgerStore


class _FakeLedger:
    def __init__(self, tasks, prs=None, raise_tasks=False, raise_prs=False):
        self._tasks = tasks
        self._prs = prs or []
        self._raise_tasks = raise_tasks
        self._raise_prs = raise_prs

    def list_tasks(self):
        if self._raise_tasks:
            raise RuntimeError("boom")
        return self._tasks

    def list_prs(self):
        if self._raise_prs:
            raise RuntimeError("boom")
        return self._prs


def _task(tid, state, title="t"):
    return SimpleNamespace(task_id=tid, title=title, state=state)


def test_todo_task_blocks_completion():
    items = pending_completion_work(_FakeLedger([_task("a", "todo")]))
    assert [i.id for i in items] == ["a"]
    assert items[0].kind == "task" and not items[0].human_required


def test_blocked_task_blocks_and_is_human_required():
    # The exact ARK-Login-Sentinel case: a blocked task is open AND human-required.
    items = pending_completion_work(_FakeLedger([_task("a", "blocked")]))
    assert len(items) == 1
    assert items[0].state == "blocked" and items[0].human_required is True


def test_done_and_dropped_tasks_are_terminal():
    led = _FakeLedger([_task("a", "done"), _task("b", "dropped"),
                       _task("c", "superseded"), _task("d", "cancelled")])
    assert pending_completion_work(led) == []


def test_open_pr_blocks_completion():
    led = _FakeLedger(
        [_task("a", "done")],
        prs=[{"pr_id": "p1", "status": "changes_requested", "branch": "feat/x"}],
    )
    items = pending_completion_work(led)
    assert len(items) == 1
    assert items[0].kind == "pr" and items[0].state == "changes_requested"


def test_conflict_pr_is_human_required():
    led = _FakeLedger([], prs=[{"pr_id": "p1", "status": "conflict", "branch": "b"}])
    items = pending_completion_work(led)
    assert items[0].human_required is True


def test_merged_and_superseded_prs_are_terminal():
    led = _FakeLedger(
        [],
        prs=[{"pr_id": "p1", "status": "merged"},
             {"pr_id": "p2", "status": "superseded"},
             {"pr_id": "p3", "status": "abandoned"},
             {"pr_id": "p4", "status": "dropped"}],
    )
    assert pending_completion_work(led) == []


def test_drained_backlog_is_empty():
    led = _FakeLedger([_task("a", "done")], prs=[{"pr_id": "p1", "status": "merged"}])
    assert pending_completion_work(led) == []


def test_fail_closed_on_task_read_error():
    items = pending_completion_work(_FakeLedger([], raise_tasks=True))
    assert len(items) == 1 and items[0].kind == "unknown"
    assert items[0].human_required is True  # treated as work-remaining


def test_fail_closed_on_pr_read_error():
    items = pending_completion_work(_FakeLedger([_task("a", "done")], raise_prs=True))
    assert len(items) == 1 and items[0].kind == "unknown"


def test_ledger_without_list_prs_fails_closed():
    class _TasksOnly:
        def list_tasks(self):
            return [_task("a", "todo")]

    items = pending_completion_work(_TasksOnly())
    assert len(items) == 1 and items[0].kind == "unknown"


def _real_store(tmp_path: Path, name: str) -> LedgerStore:
    store = LedgerStore(name, root=tmp_path)
    store.create_project(
        north_star="n", definition_of_done="d", target="new", repo_path=None
    )
    return store


def test_real_ledger_corrupt_pr_file_fails_closed(tmp_path: Path):
    store = _real_store(tmp_path, "corrupt-prs")
    store._prs_path.write_text("{not-json", encoding="utf-8")

    items = pending_completion_work(store)

    assert len(items) == 1 and items[0].kind == "unknown"


def test_real_ledger_corrupt_backlog_line_fails_closed(tmp_path: Path):
    store = _real_store(tmp_path, "corrupt-backlog")
    store._backlog_path.write_text("{not-json\n", encoding="utf-8")

    items = pending_completion_work(store)

    assert len(items) == 1 and items[0].kind == "unknown"


def test_summarize_caps_and_notes_overflow():
    items = [OpenItem("task", f"t{i}", f"task {i}", "todo", False) for i in range(12)]
    summary = summarize_open_items(items, cap=8)
    assert "+4 more" in summary
    assert summary.count(";") == 8  # 8 shown + the overflow note


def test_summarize_flags_human_required():
    items = [OpenItem("task", "t1", "Stuck thing", "blocked", True)]
    assert "(human-required)" in summarize_open_items(items)


def test_summarize_empty():
    assert summarize_open_items([]) == "no open items"
