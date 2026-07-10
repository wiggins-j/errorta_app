"""F087-14 WS-2 — backlog compaction keeps the projection bounded + correct."""
from __future__ import annotations

from pathlib import Path

from errorta_council.coding.ledger import LedgerStore, _read_jsonl


def _store(tmp_path: Path) -> LedgerStore:
    s = LedgerStore("comp", root=tmp_path)
    s.create_project(north_star="n", definition_of_done="d", target="new", repo_path=None)
    return s


def test_compact_backlog_collapses_versions_preserving_state(tmp_path: Path) -> None:
    s = _store(tmp_path)
    a = s.add_task(title="A", role="dev")
    b = s.add_task(title="B", role="dev")
    for st in ("doing", "blocked", "todo", "done"):
        s.update_task(a.task_id, state=st)
    raw_before = len(_read_jsonl(s._backlog_path))
    assert raw_before >= 6  # 2 adds + 4 updates

    dropped = s.compact_backlog()
    assert dropped > 0
    raw_after = _read_jsonl(s._backlog_path)
    assert len(raw_after) == 2  # one record per task

    # projection unchanged: A is done, B is todo, order preserved
    tasks = s.list_tasks()
    by_id = {t.task_id: t for t in tasks}
    assert by_id[a.task_id].state == "done"
    assert by_id[b.task_id].state == "todo"
    assert [t.task_id for t in tasks] == [a.task_id, b.task_id]


def test_auto_compaction_triggers_on_bloat(tmp_path: Path) -> None:
    s = _store(tmp_path)
    t = s.add_task(title="A", role="dev")
    # many updates to one task -> raw lines >> task count -> auto-compaction
    for i in range(300):
        s.update_task(t.task_id, state="doing" if i % 2 else "todo")
    raw = _read_jsonl(s._backlog_path)
    # auto-compaction kept it bounded (well under the 301 raw appends)
    assert len(raw) < 50
    assert s.list_tasks()[0].task_id == t.task_id


def test_compact_empty_backlog_is_noop(tmp_path: Path) -> None:
    s = _store(tmp_path)
    assert s.compact_backlog() == 0
