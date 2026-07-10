"""F087 Slice 1 — plan_next_batch: concurrent scheduler + same-role fan-out.

The batch planner returns ALL runnable actions for the idle members this tick
(vs decide_next's single action), so multiple workers run at once. Merges stay
serial; one task is never handed to two members.
"""
from pathlib import Path

from errorta_council.coding.ledger import LedgerStore, _atomic_write_json
from errorta_council.coding.topology import (
    DEV,
    PM,
    REVIEWER,
    Assign,
    Complete,
    Merge,
    Plan,
    plan_next_batch,
)

# 2 devs + 1 reviewer + a PM — the room shape this feature targets.
TEAM = [("m-pm", PM), ("m-dev1", DEV), ("m-dev2", DEV), ("m-rev", REVIEWER)]


def _store(tmp_path: Path) -> LedgerStore:
    s = LedgerStore("pb", root=tmp_path)
    s.create_project(north_star="n", definition_of_done="d", target="new", repo_path=None)
    return s


def _assigns(batch) -> list[Assign]:
    return [a for a in batch if isinstance(a, Assign)]


def test_two_idle_devs_two_tasks_two_distinct_assigns(tmp_path: Path) -> None:
    s = _store(tmp_path)
    t1 = s.add_task(title="A", role=DEV)
    t2 = s.add_task(title="B", role=DEV)
    assigns = _assigns(plan_next_batch(s, TEAM))
    assert len(assigns) == 2
    assert {a.member_id for a in assigns} == {"m-dev1", "m-dev2"}  # m-dev2 IS used
    assert {a.task_id for a in assigns} == {t1.task_id, t2.task_id}  # distinct tasks


def test_one_ready_task_two_devs_no_double_assign(tmp_path: Path) -> None:
    s = _store(tmp_path)
    t1 = s.add_task(title="A", role=DEV)
    assigns = _assigns(plan_next_batch(s, TEAM))
    assert len(assigns) == 1 and assigns[0].task_id == t1.task_id


def test_depends_on_withheld_until_dep_done(tmp_path: Path) -> None:
    s = _store(tmp_path)
    t1 = s.add_task(title="A", role=DEV)
    t2 = s.add_task(title="B", role=DEV, depends_on=[t1.task_id])
    first = _assigns(plan_next_batch(s, TEAM))
    assert len(first) == 1 and first[0].task_id == t1.task_id  # t2 withheld
    s.update_task(t1.task_id, state="done")
    after = _assigns(plan_next_batch(s, TEAM))
    assert any(a.task_id == t2.task_id for a in after)  # now ready


def test_three_in_flight_dev_dev_reviewer(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.add_task(title="A", role=DEV)
    s.add_task(title="B", role=DEV)
    s.add_task(title="review X", role=REVIEWER)
    assigns = _assigns(plan_next_batch(s, TEAM))
    assert len(assigns) == 3  # 2 dev + 1 reviewer concurrently
    assert sorted(a.role for a in assigns) == [DEV, DEV, REVIEWER]


def test_mergeable_pr_merge_preempts_plan(tmp_path: Path) -> None:
    s = _store(tmp_path)
    pr = s.record_pr(task_id="t1", branch="task/t1", head="abc", dev_member="m-dev1")
    s.update_pr(pr["pr_id"], status="mergeable")
    batch = plan_next_batch(s, [("m-pm", PM)])  # PM idle, no tasks
    assert any(isinstance(a, Merge) for a in batch)
    assert not any(isinstance(a, Plan) for a in batch)  # Merge OR Plan, not both


def test_mergeable_pr_is_exclusive_batch(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.add_task(title="new dev work", role=DEV)
    s.add_task(title="review work", role=REVIEWER)
    pr = s.record_pr(task_id="t1", branch="task/t1", head="abc", dev_member="m-dev1")
    s.update_pr(pr["pr_id"], status="mergeable")
    batch = plan_next_batch(s, TEAM)
    assert batch == [Merge(member_id="m-pm", pr_id=pr["pr_id"])]


def test_pm_plans_when_pipeline_dry(tmp_path: Path) -> None:
    s = _store(tmp_path)
    batch = plan_next_batch(s, TEAM)  # no tasks, no mergeable PR
    assert len(batch) == 1 and isinstance(batch[0], Plan)


def test_done_project_completes(tmp_path: Path) -> None:
    s = _store(tmp_path)
    raw = s.get_project().to_dict()
    raw["status"] = "done"
    _atomic_write_json(s._project_path, raw)
    batch = plan_next_batch(s, TEAM)
    assert len(batch) == 1 and isinstance(batch[0], Complete)
    assert batch[0].reason == "definition_of_done"


def test_pending_user_message_is_an_exclusive_pm_turn(tmp_path: Path) -> None:
    # In the concurrent scheduler, a pending message preempts worker fan-out: the
    # batch is a single exclusive PM Plan turn so the PM reads + acts on it now.
    s = _store(tmp_path)
    s.add_task(title="dev work a", role=DEV)
    s.add_task(title="dev work b", role=DEV)
    s.record_interjection("change direction: prioritize the CLI")
    batch = plan_next_batch(s, TEAM)
    assert len(batch) == 1
    assert isinstance(batch[0], Plan) and batch[0].member_id == "m-pm"


def test_no_message_still_fans_out_workers(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.add_task(title="dev work a", role=DEV)
    s.add_task(title="dev work b", role=DEV)
    batch = plan_next_batch(s, TEAM)
    assert all(isinstance(a, Assign) for a in batch)
    assert len(batch) >= 2  # both devs dispatched
