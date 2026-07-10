from pathlib import Path
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.topology import (
    decide_next, CodingReconciler, Assign, Plan, Complete, PM, DEV, REVIEWER, TESTER,
)


def _store(tmp_path: Path) -> LedgerStore:
    s = LedgerStore("p", root=tmp_path)
    s.create_project(north_star="n", definition_of_done="d", target="new", repo_path=None)
    return s


MEMBERS = [("m-pm", PM), ("m-dev", DEV), ("m-rev", REVIEWER), ("m-test", TESTER)]


def test_decide_plans_when_no_tasks(tmp_path: Path) -> None:
    s = _store(tmp_path)
    action = decide_next(s, MEMBERS)
    assert isinstance(action, Plan) and action.member_id == "m-pm"


def test_decide_assigns_actionable_dev_task(tmp_path: Path) -> None:
    s = _store(tmp_path)
    t = s.add_task(title="impl parser", role=DEV)
    action = decide_next(s, MEMBERS)
    assert isinstance(action, Assign)
    assert action.member_id == "m-dev" and action.task_id == t.task_id


def test_decide_drains_pipeline_review_before_new_dev(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.add_task(title="new dev work", role=DEV)
    s.add_task(title="review X", role=REVIEWER)
    action = decide_next(s, MEMBERS)
    assert isinstance(action, Assign) and action.role == REVIEWER  # reviewer first


def test_pending_user_message_preempts_worker_dispatch(tmp_path: Path) -> None:
    # A message to the PM must be read + acted on its NEXT turn, not deferred
    # until the worker pipeline runs dry. With dev work queued, an unconsumed
    # interjection still routes a PM Plan turn first.
    s = _store(tmp_path)
    s.add_task(title="impl parser", role=DEV)  # would normally dispatch to a dev
    s.record_interjection("optimize for memory over speed")
    action = decide_next(s, MEMBERS)
    assert isinstance(action, Plan) and action.member_id == "m-pm"


def test_no_pending_message_still_drains_pipeline(tmp_path: Path) -> None:
    # Regression: without a pending message, normal worker dispatch is unchanged.
    s = _store(tmp_path)
    t = s.add_task(title="impl parser", role=DEV)
    action = decide_next(s, MEMBERS)
    assert isinstance(action, Assign) and action.task_id == t.task_id


def test_consumed_message_no_longer_preempts(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.add_task(title="impl parser", role=DEV)
    s.record_interjection("do X")
    s.mark_interjections_consumed()  # the PM already read it
    action = decide_next(s, MEMBERS)
    assert isinstance(action, Assign)  # back to normal dispatch


def _mark_done(s: LedgerStore) -> None:
    from errorta_council.coding.ledger import _atomic_write_json
    raw = s.get_project().to_dict()
    raw["status"] = "done"
    _atomic_write_json(s._project_path, raw)


def test_decide_completes_when_project_done(tmp_path: Path) -> None:
    s = _store(tmp_path)
    _mark_done(s)  # no open work
    assert isinstance(decide_next(s, MEMBERS), Complete)


def test_done_project_works_a_newly_added_task(tmp_path: Path) -> None:
    # A finished project with a NEW todo task (a user-added fix, or a steering
    # directive) must re-open and work it — not short-circuit straight to Complete.
    s = _store(tmp_path)
    _mark_done(s)
    t = s.add_task(title="fix the crash", role=DEV)
    action = decide_next(s, MEMBERS)
    assert isinstance(action, Assign), f"expected Assign, got {action!r}"
    assert action.task_id == t.task_id and action.role == DEV


def test_done_project_batch_works_a_newly_added_task(tmp_path: Path) -> None:
    from errorta_council.coding.topology import plan_next_batch
    s = _store(tmp_path)
    _mark_done(s)
    t = s.add_task(title="fix the crash", role=DEV)
    batch = plan_next_batch(s, MEMBERS)
    assigns = [a for a in batch if isinstance(a, Assign)]
    assert assigns and assigns[0].task_id == t.task_id
    assert not any(isinstance(a, Complete) for a in batch)


def test_done_project_replans_an_active_focus_with_no_tasks(tmp_path: Path) -> None:
    # F146 Slice E — Problem 1: a finished project with an active Current Focus but
    # NO tasks of its own must give the PM a Plan turn (re-brainstorm the focus into
    # tasks), not short-circuit to Complete. This is the "Start does nothing" repro.
    s = _store(tmp_path)
    _mark_done(s)
    s.add_focus(title="add graphics for everything", origin="user")
    action = decide_next(s, MEMBERS)
    assert isinstance(action, Plan), f"expected Plan, got {action!r}"
    assert action.member_id == "m-pm"


def test_done_project_batch_replans_an_active_focus_with_no_tasks(tmp_path: Path) -> None:
    from errorta_council.coding.topology import plan_next_batch
    s = _store(tmp_path)
    _mark_done(s)
    s.add_focus(title="add graphics for everything", origin="user")
    batch = plan_next_batch(s, MEMBERS)
    assert any(isinstance(a, Plan) and a.member_id == "m-pm" for a in batch), (
        f"expected a PM Plan in the batch, got {batch!r}"
    )
    assert not any(isinstance(a, Complete) for a in batch)


def test_done_project_no_focus_still_completes(tmp_path: Path) -> None:
    # Guard the unchanged path: done + no active focus + no open work -> Complete.
    s = _store(tmp_path)
    _mark_done(s)
    action = decide_next(s, MEMBERS)
    assert isinstance(action, Complete)


def test_reconciler_dev_done_spawns_review_task(tmp_path: Path) -> None:
    s = _store(tmp_path)
    rec = CodingReconciler(s)
    t = s.add_task(title="impl parser", role=DEV)
    rec.assign(Assign(member_id="m-dev", task_id=t.task_id, role=DEV))
    assert s.list_tasks(state="doing")[0].assignee_member_id == "m-dev"
    rec.complete_dev_task(s.list_tasks(role=DEV)[0])
    reviews = s.list_tasks(role=REVIEWER)
    assert len(reviews) == 1 and reviews[0].title == "review: impl parser"
    assert reviews[0].depends_on == [t.task_id]


def test_reconciler_review_approve_spawns_test_task(tmp_path: Path) -> None:
    s = _store(tmp_path)
    rec = CodingReconciler(s)
    rt = s.add_task(title="review: impl", role=REVIEWER)
    rec.complete_review_task(rt, approved=True, reviewed_task_id="t-dev",
                             reviewed_title="impl")
    tests = s.list_tasks(role=TESTER)
    assert len(tests) == 1 and tests[0].title == "validate: impl"


def test_reconciler_review_reject_spawns_revise_dev_task(tmp_path: Path) -> None:
    s = _store(tmp_path)
    rec = CodingReconciler(s)
    rt = s.add_task(title="review: impl", role=REVIEWER)
    rec.complete_review_task(rt, approved=False, reviewed_task_id="t-dev",
                             reviewed_title="impl")
    revises = [t for t in s.list_tasks(role=DEV) if t.title.startswith("revise")]
    assert len(revises) == 1


def test_reconciler_block_records_decision(tmp_path: Path) -> None:
    s = _store(tmp_path)
    rec = CodingReconciler(s)
    t = s.add_task(title="needs creds", role=DEV)
    rec.block_task(t, reason="needs an API key")
    assert s.list_tasks(state="blocked")[0].task_id == t.task_id
    assert s.list_decisions()[0]["rationale"] == "needs an API key"


def test_decide_is_resumable_from_disk(tmp_path: Path) -> None:
    s = _store(tmp_path)
    t = s.add_task(title="impl", role=DEV)
    # fresh store instance reproduces the same decision
    fresh = LedgerStore("p", root=tmp_path)
    a1 = decide_next(s, MEMBERS); a2 = decide_next(fresh, MEMBERS)
    assert isinstance(a1, Assign) and isinstance(a2, Assign)
    assert a1.task_id == a2.task_id == t.task_id


def test_coding_topology_propose_next_adapter(tmp_path: Path) -> None:
    from errorta_council.coding.topology import CodingTopology
    s = _store(tmp_path)
    t = s.add_task(title="impl", role=DEV)
    run = {
        "coding_ledger": s,
        "members": [
            {"id": "m-pm", "enabled": True, "metadata": {"coding_role": "pm"}},
            {"id": "m-dev", "enabled": True, "metadata": {"coding_role": "dev"}},
        ],
        "counters": None,
    }
    prop = CodingTopology().propose_next(run, [])
    assert prop.member_id == "m-dev"  # actionable dev task -> dev proposed


def test_coding_role_of_defaults_to_dev(tmp_path: Path) -> None:
    from errorta_council.coding.topology import coding_role_of
    assert coding_role_of({"id": "m", "metadata": {"coding_role": "pm"}}) == "pm"
    assert coding_role_of({"id": "m"}) == "dev"
    assert coding_role_of({"id": "m", "coding_role": "tester"}) == "tester"
