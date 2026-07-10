from pathlib import Path

from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.run_recovery import (
    reclaim_stranded_inflight,
    recover_orphaned_run,
    scan_and_recover,
)


def _project(tmp_errorta_home: Path, project_id: str = "prec") -> LedgerStore:
    store = LedgerStore(project_id)
    store.create_project(north_star="n", definition_of_done="d", target="new", repo_path=None)
    return store


def test_orphan_running_run_is_interrupted_and_requeues_doing_tasks(
    tmp_errorta_home: Path,
) -> None:
    store = _project(tmp_errorta_home)
    task = store.add_task(title="impl", role="dev")
    store.update_task(task.task_id, state="doing", assignee_member_id="m-dev")
    store.set_run_state(status="running", started_at="2026-06-17T00:00:00Z")

    result = recover_orphaned_run(store, live=False, reason="unit_test")

    assert result.recovered is True
    assert result.requeued_task_ids == [task.task_id]
    state = store.get_run_state()
    assert state["status"] == "interrupted"
    assert state["recoverable"] is True
    assert state["can_resume"] is True
    assert state["recovery_reason"] == "unit_test"
    reloaded = LedgerStore("prec").list_tasks()[0]
    assert reloaded.state == "todo"
    assert reloaded.assignee_member_id is None
    decisions = LedgerStore("prec").list_decisions()
    assert decisions[-1]["choice"] == "run_interrupted"
    assert decisions[-1]["related_task_ids"] == [task.task_id]


def test_recovery_is_idempotent_after_interrupted(tmp_errorta_home: Path) -> None:
    store = _project(tmp_errorta_home, "pidem")
    task = store.add_task(title="impl", role="dev")
    store.update_task(task.task_id, state="doing", assignee_member_id="m-dev")
    store.set_run_state(status="running")

    recover_orphaned_run(store, live=False)
    second = recover_orphaned_run(store, live=False)

    assert second.recovered is False
    assert store.get_run_state()["status"] == "interrupted"
    assert len(store.list_decisions()) == 1


def test_live_thread_is_not_recovered(tmp_errorta_home: Path) -> None:
    store = _project(tmp_errorta_home, "plive")
    task = store.add_task(title="impl", role="dev")
    store.update_task(task.task_id, state="doing", assignee_member_id="m-dev")
    store.set_run_state(status="running")

    result = recover_orphaned_run(store, live=True)

    assert result.recovered is False
    assert store.get_run_state()["status"] == "running"
    assert store.list_tasks()[0].state == "doing"
    assert store.list_decisions() == []


def test_reclaim_requeues_tasks_stranded_by_a_terminal_stop(
    tmp_errorta_home: Path,
) -> None:
    # The real freeze case: a run ended cleanly in a TERMINAL state (a
    # member_unhealthy 'stopped'), leaving in-flight tasks wedged in 'doing'.
    # recover_orphaned_run ignores non-'running' status, so these would be frozen
    # forever — reclaim_stranded_inflight must pick them back up at the next start.
    store = _project(tmp_errorta_home, "pstop")
    t1 = store.add_task(title="impl", role="dev")
    t2 = store.add_task(title="review", role="reviewer")
    t3 = store.add_task(title="queued", role="dev")  # already todo — untouched
    store.update_task(t1.task_id, state="doing", assignee_member_id="m-dev-1")
    store.update_task(t2.task_id, state="doing", assignee_member_id="m-review-2")
    store.set_run_state(status="stopped", stop_reason="member_unhealthy")

    # recover_orphaned_run does NOTHING for a terminal 'stopped' status.
    assert recover_orphaned_run(store, live=False).recovered is False
    assert store.list_tasks(state="doing")  # still wedged

    requeued = reclaim_stranded_inflight(store, reason="unit_test")

    assert set(requeued) == {t1.task_id, t2.task_id}
    reloaded = LedgerStore("pstop")
    by_id = {t.task_id: t for t in reloaded.list_tasks()}
    assert by_id[t1.task_id].state == "todo"
    assert by_id[t1.task_id].assignee_member_id is None
    assert by_id[t2.task_id].state == "todo"
    assert by_id[t3.task_id].state == "todo"  # the already-todo task is untouched
    decision = reloaded.list_decisions()[-1]
    assert decision["choice"] == "inflight_reclaimed"
    assert set(decision["related_task_ids"]) == {t1.task_id, t2.task_id}


def test_reclaim_is_a_noop_when_nothing_is_doing(tmp_errorta_home: Path) -> None:
    store = _project(tmp_errorta_home, "pnoop")
    store.add_task(title="impl", role="dev")  # stays todo
    store.set_run_state(status="stopped")

    assert reclaim_stranded_inflight(store) == []
    # No spurious decision record when there was nothing to reclaim.
    assert all(d["choice"] != "inflight_reclaimed" for d in store.list_decisions())


def test_scan_and_recover_marks_all_orphaned_projects(tmp_errorta_home: Path) -> None:
    root = tmp_errorta_home / ".errorta" / "council" / "coding-projects"
    running = _project(tmp_errorta_home, "scan-run")
    idle = _project(tmp_errorta_home, "scan-idle")
    running.set_run_state(status="running")
    idle.set_run_state(status="idle")

    summary = scan_and_recover(root=root)

    assert summary.scanned == 2
    assert summary.interrupted_projects == ["scan-run"]
    assert LedgerStore("scan-run").get_run_state()["status"] == "interrupted"
    assert LedgerStore("scan-idle").get_run_state()["status"] == "idle"
