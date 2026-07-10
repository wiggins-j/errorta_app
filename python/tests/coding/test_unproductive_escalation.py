"""F127 Workstream B — escalate-up reassignment + exhausted-ladder Problem.

A worker that keeps producing unusable turns must route its task to a different
(preferably stronger) member; if every member of the role fails it, a blocking
attention Problem is raised and the run stops — never a silent no_progress."""
from __future__ import annotations

from pathlib import Path

from errorta_council.coding import attention
from errorta_council.coding import model_tier as mt
from errorta_council.coding.autonomy import (
    CADENCE_OFF,
    DEFINITION_OF_DONE,
    CodingAutonomyPolicy,
    LoopCounters,
    TurnOutcome,
    _handle_unproductive,
    run_coding_loop,
)
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.runner import build_run_turn, members_by_coding_role
from errorta_council.coding.topology import (
    DEV,
    PM,
    REVIEWER,
    Assign,
    CodingReconciler,
    PMAssist,
    decide_next,
)


def _store(tmp_path: Path) -> LedgerStore:
    s = LedgerStore("punprod", root=tmp_path)
    s.create_project(north_star="n", definition_of_done="d", target="new", repo_path=None)
    return s


def _outcome(member_id: str, route: str = "claude_cli.haiku") -> TurnOutcome:
    return TurnOutcome(kind="noop", unproductive=True, member_id=member_id,
                       member_role=DEV, member_route=route, reason="turn_tool_markup_only")


MEMBERS = [("m-dev-1", DEV), ("m-dev-2", DEV), ("m-rev", REVIEWER), ("m-pm", PM)]
POLICY = CodingAutonomyPolicy(worker_unproductive_limit=2)


def test_reassigns_after_limit_excluding_failed_member(tmp_path: Path) -> None:
    s = _store(tmp_path)
    task = s.add_task(title="impl X", role=DEV)
    c = LoopCounters()
    act = Assign(member_id="m-dev-1", task_id=task.task_id, role=DEV)

    # First unproductive turn: under the limit -> let the same member retry.
    assert _handle_unproductive(s, act, _outcome("m-dev-1"), c, POLICY, MEMBERS) is None
    assert not _excluded(s, task.task_id)

    # Second: hits the limit -> exclude m-dev-1, reassign (m-dev-2 still eligible).
    assert _handle_unproductive(s, act, _outcome("m-dev-1"), c, POLICY, MEMBERS) is None
    assert _excluded(s, task.task_id) == {"m-dev-1"}
    assert any(d["choice"] == "worker_excluded" for d in s.list_decisions())
    # The task is back to todo with no assignee, ready for a different member.
    t = next(t for t in s.list_tasks() if t.task_id == task.task_id)
    assert t.state == "todo" and t.assignee_member_id is None


def test_scheduler_routes_excluded_task_to_a_different_higher_tier_member(tmp_path: Path) -> None:
    s = _store(tmp_path)
    task = s.add_task(title="impl X", role=DEV)
    s.update_task(task.task_id, state="todo", excluded_member_ids=["m-dev-1"])
    tiers = {"m-dev-1": mt.tier_rank(mt.LIGHT), "m-dev-2": mt.tier_rank(mt.STRONG)}
    action = decide_next(s, MEMBERS, tiers)
    assert isinstance(action, Assign)
    assert action.member_id == "m-dev-2"  # excluded m-dev-1 skipped, stronger picked


def test_tiers_do_not_change_normal_room_order(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.add_task(title="impl X", role=DEV)
    tiers = {"m-dev-1": mt.tier_rank(mt.LIGHT), "m-dev-2": mt.tier_rank(mt.STRONG)}
    action = decide_next(s, MEMBERS, tiers)
    assert isinstance(action, Assign)
    assert action.member_id == "m-dev-1"


def test_exhausted_same_role_routes_to_pm_assist_before_attention(tmp_path: Path) -> None:
    s = _store(tmp_path)
    task = s.add_task(title="impl X", role=DEV)
    c = LoopCounters()
    a1 = Assign(member_id="m-dev-1", task_id=task.task_id, role=DEV)
    a2 = Assign(member_id="m-dev-2", task_id=task.task_id, role=DEV)
    # Exhaust m-dev-1 (2 turns), then m-dev-2 (2 turns).
    _handle_unproductive(s, a1, _outcome("m-dev-1"), c, POLICY, MEMBERS)
    _handle_unproductive(s, a1, _outcome("m-dev-1"), c, POLICY, MEMBERS)
    _handle_unproductive(s, a2, _outcome("m-dev-2"), c, POLICY, MEMBERS)
    stop = _handle_unproductive(s, a2, _outcome("m-dev-2"), c, POLICY, MEMBERS)

    assert stop is None
    assert _excluded(s, task.task_id) == {"m-dev-1", "m-dev-2"}
    action = decide_next(s, MEMBERS)
    assert isinstance(action, PMAssist)
    assert action.task_id == task.task_id
    assert not [sig for sig in attention.list_open("punprod", store=s)
                if sig.source == "worker_unproductive"]


def test_pm_assist_re_scopes_task_into_smaller_children(tmp_path: Path) -> None:
    s = _store(tmp_path)
    task = s.add_task(title="impl X", role=DEV, detail="Change src/x.py")
    s.update_task(
        task.task_id,
        excluded_member_ids=["m-dev-1"],
        excluded_member_routes={"m-dev-1": "claude_cli.haiku"},
        pm_assist_pending=True,
        pm_assist_limit=1,
    )
    members = [
        {"id": "m-pm", "enabled": True, "metadata": {"coding_role": PM}},
        {"id": "m-dev-1", "enabled": True, "metadata": {"coding_role": DEV}},
    ]

    def caller(_member, _prompt):
        return (
            '{"schema_version":"coding_turn.v1","role":"pm","intent":'
            '{"kind":"plan","done":false,"tasks":['
            '{"title":"Define X contract","role":"dev",'
            '"detail":"Acceptance: contract documented. File: src/x.py",'
            '"depends_on":[]}]}}'
        )

    run_turn = build_run_turn(
        s, None, members_by_coding_role(members), caller, guardrail_enabled=True
    )
    outcome = run_turn(PMAssist("m-pm", task.task_id), s)

    assert outcome.kind == "planned"
    original = next(item for item in s.list_tasks() if item.task_id == task.task_id)
    children = [item for item in s.list_tasks() if item.parent_task_id == task.task_id]
    assert original.state == "dropped"
    assert len(children) == 1
    assert "Acceptance:" in children[0].detail


def test_failed_pm_assist_raises_blocking_problem(tmp_path: Path) -> None:
    s = _store(tmp_path)
    task = s.add_task(title="impl X", role=DEV)
    s.update_task(
        task.task_id,
        excluded_member_ids=["m-dev-1"],
        excluded_member_routes={"m-dev-1": "claude_cli.haiku"},
        pm_assist_pending=True,
        pm_assist_limit=1,
    )
    members = [
        {"id": "m-pm", "enabled": True, "metadata": {"coding_role": PM}},
        {"id": "m-dev-1", "enabled": True, "metadata": {"coding_role": DEV}},
    ]
    run_turn = build_run_turn(
        s,
        None,
        members_by_coding_role(members),
        lambda _member, _prompt: "still not JSON",
        guardrail_enabled=True,
    )
    outcome = run_turn(PMAssist("m-pm", task.task_id), s)

    assert outcome.kind == "pm_assist_exhausted"
    problems = [sig for sig in attention.list_open("punprod", store=s)
                if sig.source == "worker_unproductive"]
    assert len(problems) == 1


def test_sequential_loop_retries_weak_member_then_reassigns_up(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.add_task(title="impl X", role=DEV)
    members = [("weak", DEV), ("strong", DEV), ("m-pm", PM)]
    tiers = {"weak": mt.tier_rank(mt.LIGHT), "strong": mt.tier_rank(mt.STRONG)}
    calls: list[str] = []

    def run_turn(action, ledger):
        if isinstance(action, Assign):
            calls.append(action.member_id)
            if action.member_id == "weak":
                ledger.update_task(action.task_id, state="todo")
                return _outcome("weak")
            ledger.set_project_status("done")
            return TurnOutcome(kind="project_done", member_id="strong")
        return TurnOutcome(kind="noop", made_progress=False)

    result = run_coding_loop(
        s,
        members,
        CodingAutonomyPolicy(
            checkpoint_cadence=CADENCE_OFF,
            max_parallel_workers=1,
            worker_unproductive_limit=2,
        ),
        run_turn=run_turn,
        reconciler=CodingReconciler(s),
        member_tiers=tiers,
    )

    assert result.stop_reason == DEFINITION_OF_DONE
    assert calls == ["weak", "weak", "strong"]
    assert result.counters.task_reassignments == 1
    reassignment = next(
        decision for decision in s.list_decisions()
        if decision["choice"] == "task_reassigned"
    )
    assert reassignment["from_member_id"] == "weak"
    assert reassignment["to_member_id"] == "strong"


def test_model_change_clears_exclusion_and_stale_problem(tmp_path: Path) -> None:
    s = _store(tmp_path)
    task = s.add_task(title="impl X", role=DEV)
    s.update_task(
        task.task_id,
        excluded_member_ids=["m-dev-1"],
        excluded_member_routes={"m-dev-1": "claude_cli.haiku"},
        pm_assist_pending=True,
        pm_assist_attempts=1,
    )
    attention.raise_worker_unproductive_problem(
        "punprod",
        task_id=task.task_id,
        task_title=task.title,
        members_tried=["m-dev-1"],
        last_member="m-dev-1",
        last_route="claude_cli.haiku",
        last_error="turn_tool_markup_only",
        store=s,
    )

    dismissed = attention.resolve_stale_worker_unproductive(
        "punprod",
        [{
            "id": "m-dev-1",
            "enabled": True,
            "metadata": {"coding_role": DEV},
            "gateway_route_id": "claude_cli.opus",
        }],
        store=s,
    )

    refreshed = next(item for item in s.list_tasks() if item.task_id == task.task_id)
    assert dismissed
    assert not refreshed._extras.get("excluded_member_ids")
    assert refreshed._extras.get("pm_assist_pending") is False
    assert not [sig for sig in attention.list_open("punprod", store=s)
                if sig.source == "worker_unproductive"]


def test_accepting_worker_problem_does_not_create_meta_task(tmp_path: Path) -> None:
    s = _store(tmp_path)
    signal = attention.raise_worker_unproductive_problem(
        "punprod",
        task_id="t-stuck",
        task_title="impl X",
        members_tried=["m-dev-1"],
        last_member="m-dev-1",
        last_route="claude_cli.haiku",
        last_error="turn_tool_markup_only",
        store=s,
    )
    assert signal is not None

    _updated, created_task_id = attention.resolve(
        "punprod", signal.id, "accept", suggestion_id="stop", store=s
    )

    assert created_task_id is None
    assert not s.list_tasks()


def _excluded(store: LedgerStore, task_id: str) -> set[str]:
    t = next(t for t in store.list_tasks() if t.task_id == task_id)
    ex = (getattr(t, "_extras", {}) or {}).get("excluded_member_ids") or []
    return set(ex)
