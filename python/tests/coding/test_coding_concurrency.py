"""F087-13 WS-3 — concurrency / state-integrity locks."""
from __future__ import annotations

import threading
from pathlib import Path

from errorta_council.coding.ledger import LedgerStore, _append_jsonl
from errorta_council.coding.run_recovery import recover_orphaned_run


def _store(tmp_path: Path, pid: str = "c") -> LedgerStore:
    s = LedgerStore(pid, root=tmp_path)
    s.create_project(north_star="n", definition_of_done="d", target="new", repo_path=None)
    return s


def test_read_jsonl_tolerates_torn_trailing_line(tmp_path: Path) -> None:
    s = _store(tmp_path)
    t = s.add_task(title="impl", role="dev")
    # simulate a crash-torn final append: a partial JSON line with no newline.
    with open(s._backlog_path, "a", encoding="utf-8") as fh:
        fh.write('{"task_id": "torn", "title": "half')  # no closing brace/newline
    tasks = s.list_tasks()
    # the good record survives; the torn tail is skipped, not raised.
    assert any(x.task_id == t.task_id for x in tasks)
    assert all(x.task_id != "torn" for x in tasks)


def test_set_run_state_concurrent_writes_do_not_lose_fields(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.set_run_state(status="running")
    keys = [f"k{i}" for i in range(40)]
    barrier = threading.Barrier(len(keys))

    def writer(k: str) -> None:
        barrier.wait()
        # each thread reads-modifies-writes a DISTINCT field; without the lock,
        # last-writer-wins on the whole doc would drop most of them.
        LedgerStore("c", root=tmp_path).set_run_state(**{k: True})

    threads = [threading.Thread(target=writer, args=(k,)) for k in keys]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    final = LedgerStore("c", root=tmp_path).get_run_state()
    missing = [k for k in keys if final.get(k) is not True]
    assert missing == [], f"lost fields under concurrent writes: {missing}"


def test_recover_orphaned_run_is_idempotent_under_concurrency(tmp_path: Path) -> None:
    s = _store(tmp_path, "rec")
    task = s.add_task(title="impl", role="dev")
    s.update_task(task.task_id, state="doing", assignee_member_id="m-dev")
    s.set_run_state(status="running")

    barrier = threading.Barrier(16)
    results = []
    lock = threading.Lock()

    def recover() -> None:
        barrier.wait()
        r = recover_orphaned_run(LedgerStore("rec", root=tmp_path),
                                 live=False, reason="status_race")
        with lock:
            results.append(r.recovered)

    threads = [threading.Thread(target=recover) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # exactly ONE caller flipped running->interrupted.
    assert sum(1 for r in results if r) == 1
    final = LedgerStore("rec", root=tmp_path)
    assert final.get_run_state()["status"] == "interrupted"
    # the in-flight task was requeued exactly once, no duplicate decisions.
    assert final.list_tasks()[0].state == "todo"
    interrupts = [d for d in final.list_decisions() if d["choice"] == "run_interrupted"]
    assert len(interrupts) == 1


def test_recover_skips_when_worker_is_live(tmp_path: Path) -> None:
    s = _store(tmp_path, "live")
    s.set_run_state(status="running")
    r = recover_orphaned_run(s, live=True, reason="x")
    assert r.recovered is False
    assert s.get_run_state()["status"] == "running"
