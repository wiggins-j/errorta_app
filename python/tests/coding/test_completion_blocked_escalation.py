"""F128 slice 4 — a repeated false done-claim escalates to a blocking
completion_blocked Problem and a truthful stop, never a silent no_progress."""
from __future__ import annotations

from pathlib import Path

from errorta_council.coding import attention
from errorta_council.coding.autonomy import (
    CADENCE_OFF,
    COMPLETION_BLOCKED,
    CodingAutonomyPolicy,
    LoopCounters,
    TurnOutcome,
    _completion_streak_reset_by,
    _handle_completion_refused,
    policy_from_dict,
    policy_to_dict,
    run_coding_loop,
)
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.topology import DEV, PM, REVIEWER, Plan

MEMBERS = [("m-pm", PM), ("m-dev", DEV), ("m-reviewer", REVIEWER)]


def _store(name: str, root: Path | None = None) -> LedgerStore:
    s = LedgerStore(name, root=root)
    s.create_project(north_star="n", definition_of_done="d", target="new", repo_path=None)
    return s


def test_streak_below_limit_does_not_stop(tmp_path: Path) -> None:
    store = _store("cb-below", tmp_path)
    store.add_task(title="open thing", role="dev")
    c = LoopCounters()
    policy = CodingAutonomyPolicy(completion_refused_limit=2)

    assert _handle_completion_refused(store, c, policy) is None
    assert c.false_done_streak == 1
    # no Problem yet
    assert not [s for s in attention.list_open("cb-below", store=store)
                if s.source == "completion_blocked"]


def test_streak_at_limit_raises_blocking_problem_and_stops(tmp_path: Path) -> None:
    store = _store("cb-stop", tmp_path)
    store.add_task(title="Main Loop Integration", role="dev")
    c = LoopCounters()
    policy = CodingAutonomyPolicy(completion_refused_limit=2)

    assert _handle_completion_refused(store, c, policy) is None
    stop = _handle_completion_refused(store, c, policy)

    assert stop == COMPLETION_BLOCKED
    problems = [s for s in attention.list_open("cb-stop", store=store)
                if s.source == "completion_blocked"]
    assert len(problems) == 1
    assert problems[0].blocking is True
    assert "still open" in problems[0].summary


def test_problem_is_deduped_across_repeated_escalations(tmp_path: Path) -> None:
    store = _store("cb-dedup", tmp_path)
    store.add_task(title="x", role="dev")
    c = LoopCounters()
    policy = CodingAutonomyPolicy(completion_refused_limit=1)

    assert _handle_completion_refused(store, c, policy) == COMPLETION_BLOCKED
    # streak keeps climbing on further false claims, but only ONE Problem exists
    assert _handle_completion_refused(store, c, policy) == COMPLETION_BLOCKED
    problems = [s for s in attention.list_open("cb-dedup", store=store)
                if s.source == "completion_blocked"]
    assert len(problems) == 1


def test_human_required_blocked_task_is_flagged(tmp_path: Path) -> None:
    store = _store("cb-human", tmp_path)
    task = store.add_task(title="resolve conflict", role="dev")
    store.update_task(task.task_id, state="blocked")
    c = LoopCounters()
    policy = CodingAutonomyPolicy(completion_refused_limit=1)

    _handle_completion_refused(store, c, policy)
    problem = [s for s in attention.list_open("cb-human", store=store)
               if s.source == "completion_blocked"][0]
    assert "need you" in problem.summary
    assert problem.context.get("human_required_count") == 1


def test_policy_knob_round_trips() -> None:
    p = CodingAutonomyPolicy(completion_refused_limit=4)
    assert policy_to_dict(p)["completion_refused_limit"] == 4
    assert policy_from_dict(policy_to_dict(p)).completion_refused_limit == 4
    # clamped to >= 1
    assert policy_from_dict({"completion_refused_limit": 0}).completion_refused_limit == 1
    # default when absent
    assert policy_from_dict({}).completion_refused_limit == 2


def test_only_productive_turns_reset_the_streak() -> None:
    assert not _completion_streak_reset_by(
        TurnOutcome(kind="planned", made_progress=False)
    )
    assert not _completion_streak_reset_by(TurnOutcome(kind="noop"))
    assert _completion_streak_reset_by(TurnOutcome(kind="planned", made_progress=True))
    assert _completion_streak_reset_by(TurnOutcome(kind="task_done"))


def test_nonproductive_turn_does_not_postpone_loop_escalation(tmp_path: Path) -> None:
    store = _store("cb-nonproductive", tmp_path)
    task = store.add_task(title="blocked", role=DEV)
    store.update_task(task.task_id, state="blocked")
    calls = 0

    def run_turn(action, ledger):
        nonlocal calls
        assert isinstance(action, Plan)
        calls += 1
        if calls == 2:
            return TurnOutcome(kind="planned", made_progress=False)
        return TurnOutcome(kind="completion_refused", made_progress=False)

    result = run_coding_loop(
        store,
        MEMBERS,
        CodingAutonomyPolicy(
            checkpoint_cadence=CADENCE_OFF,
            completion_refused_limit=2,
            max_parallel_workers=1,
            pm_idle_limit=10,
        ),
        run_turn=run_turn,
    )

    assert calls == 3
    assert result.stop_reason == COMPLETION_BLOCKED
    assert store.get_project().status != "done"


def test_concurrent_loop_escalates_refused_completion(tmp_path: Path) -> None:
    store = _store("cb-concurrent", tmp_path)
    task = store.add_task(title="blocked", role=DEV)
    store.update_task(task.task_id, state="blocked")

    result = run_coding_loop(
        store,
        MEMBERS,
        CodingAutonomyPolicy(
            checkpoint_cadence=CADENCE_OFF,
            completion_refused_limit=2,
            max_parallel_workers=3,
        ),
        run_turn=lambda action, ledger: TurnOutcome(
            kind="completion_refused", made_progress=False
        ),
    )

    assert result.stop_reason == COMPLETION_BLOCKED
    assert result.counters.false_done_streak == 2
    assert store.get_project().status != "done"


def test_accepting_completion_problem_does_not_create_meta_task(tmp_path: Path) -> None:
    store = _store("cb-accept", tmp_path)
    item = store.add_task(title="blocked", role=DEV)
    store.update_task(item.task_id, state="blocked")
    assert _handle_completion_refused(
        store, LoopCounters(), CodingAutonomyPolicy(completion_refused_limit=1)
    ) == COMPLETION_BLOCKED
    signal = next(
        sig for sig in attention.list_open("cb-accept", store=store)
        if sig.source == "completion_blocked"
    )

    _updated, created_task_id = attention.resolve(
        "cb-accept", signal.id, "accept", suggestion_id="stop", store=store
    )

    assert created_task_id is None
    assert [task.title for task in store.list_tasks()] == ["blocked"]
