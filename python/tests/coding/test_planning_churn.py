"""Spec 07 — progress semantics + the planning-churn stop.

The observed failure: 10+ consecutive ``pm-1 / plan`` turns with ZERO worker
turns and a backlog ballooning to 130 tasks. Neither existing convergence
detector could catch it — ``not_converging`` treats a newly-created task as
motion BY DESIGN, and ``gate_not_improving`` needs a gate signal that only worker
turns produce. Both structurally assume workers are running.

Covered here: the ``plan_streak`` counter (increment on plan turns, reset by
every branch a WORKER turn reaches), the ``_account_planning_churn`` detector
(trips exactly at the limit, ``0`` disables), the policy round-trip, and a
regression for the already-landed half — an all-duplicate plan batch yields
``made_progress=False``, which re-arms the pm_idle / NO_PROGRESS guard.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from errorta_council.coding.autonomy import (
    PLANNING_CHURN,
    CodingAutonomyPolicy,
    LoopCounters,
    TurnOutcome,
    _account_planning_churn,
    _apply_outcome,
    _open_backlog_shape,
    policy_from_dict,
    policy_to_dict,
)
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.runner import _materialize_pm_tasks
from errorta_council.coding.schemas import TurnParseError, parse_coding_turn
from errorta_council.coding.topology import DEV

# --------------------------------------------------------------------------- #
# Stubs. `_apply_outcome` only ever duck-types the reconciler, so a recorder is
# enough to exercise the counter wiring without dragging in the PR machinery.
# --------------------------------------------------------------------------- #

class FakeReconciler:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def complete_dev_task(self, task: Any) -> None:
        self.calls.append("complete_dev_task")

    def complete_review_task(self, task: Any, **kwargs: Any) -> None:
        self.calls.append("complete_review_task")

    def block_task(self, task: Any, **kwargs: Any) -> None:
        self.calls.append("block_task")


class FakeTask:
    def __init__(self, task_id: str = "t-1", title: str = "impl X",
                 state: str = "doing") -> None:
        self.task_id = task_id
        self.title = title
        self.state = state


class FakeAction:
    def __init__(self, role: str = DEV, task_id: str = "t-1") -> None:
        self.role = role
        self.task_id = task_id


class FakeLedger:
    """Exposes only what `_open_backlog_shape` reads."""

    def __init__(self, tasks: list[FakeTask] | None = None) -> None:
        self._tasks = list(tasks or [])

    def list_tasks(self) -> list[FakeTask]:
        return list(self._tasks)


def _planned(made_progress: bool = True) -> TurnOutcome:
    return TurnOutcome(kind="planned", made_progress=made_progress)


def _apply(outcome: TurnOutcome, c: LoopCounters, *,
           rec: FakeReconciler | None = None,
           ledger: Any = None, action: Any = None) -> bool:
    return _apply_outcome(
        rec or FakeReconciler(),  # type: ignore[arg-type]
        ledger if ledger is not None else FakeLedger(),
        action if action is not None else FakeAction(),
        outcome, c,
    )


# --------------------------------------------------------------------------- #
# 1. plan_streak: increments on plan turns, resets on every worker turn.
# --------------------------------------------------------------------------- #

def test_plan_streak_increments_across_consecutive_plan_turns() -> None:
    c = LoopCounters()
    assert c.plan_streak == 0
    for expected in range(1, 6):
        _apply(_planned(), c)
        assert c.plan_streak == expected


def test_plan_streak_increments_even_when_the_plan_turn_made_progress() -> None:
    """A productive-looking plan turn still counts: the pathology is a PM that
    keeps minting dispatchable tasks while no worker ever runs."""
    c = LoopCounters()
    for _ in range(3):
        _apply(_planned(made_progress=True), c)
    assert c.plan_streak == 3 and c.pm_idle == 0


def test_governance_progress_does_not_increment_and_resets_plan_streak() -> None:
    """FIX 1: governance turns (GovernancePlan/Review/Materialize) all emit
    ``governance_progress`` during the design phase when NO worker turn exists to
    reset the streak. Counting them tripped ``planning_churn`` on a legitimate
    light/strict governance run before implementation tasks were ever created.
    Governance advancing is bounded progress toward implementation (guarded by
    max_review_rounds), so it must NOT increment plan_streak, and it RESETS it."""
    c = LoopCounters()
    _apply(TurnOutcome(kind="governance_progress", made_progress=True), c)
    assert c.plan_streak == 0

    # And it actively resets an in-progress plan streak.
    c = LoopCounters()
    for _ in range(4):
        _apply(_planned(), c)
    assert c.plan_streak == 4
    _apply(TurnOutcome(kind="governance_progress", made_progress=True), c)
    assert c.plan_streak == 0


def test_sustained_governance_progress_never_trips_planning_churn() -> None:
    """8+ consecutive governance_progress turns (a strict governance design phase
    with no worker turns yet) must NOT trip planning_churn — governance is bounded
    by its own review-round guard, not by plan_streak."""
    policy = CodingAutonomyPolicy(plan_streak_limit=6)
    led = FakeLedger()
    c = LoopCounters()
    for _ in range(8):
        _apply(TurnOutcome(kind="governance_progress", made_progress=True), c,
               ledger=led)
        assert _account_planning_churn(led, c, policy) is None
    assert c.plan_streak == 0


def test_task_done_resets_plan_streak() -> None:
    c = LoopCounters()
    for _ in range(4):
        _apply(_planned(), c)
    assert c.plan_streak == 4

    rec = FakeReconciler()
    _apply(TurnOutcome(kind="task_done", task=FakeTask()), c,
           rec=rec, action=FakeAction(role=DEV))
    assert rec.calls == ["complete_dev_task"]
    assert c.plan_streak == 0 and c.pm_idle == 0


def test_review_done_resets_plan_streak() -> None:
    c = LoopCounters()
    for _ in range(4):
        _apply(_planned(), c)

    rec = FakeReconciler()
    _apply(TurnOutcome(kind="review_done", task=FakeTask(), approved=True), c,
           rec=rec)
    assert rec.calls == ["complete_review_task"]
    assert c.plan_streak == 0


def test_task_blocked_resets_plan_streak() -> None:
    c = LoopCounters()
    for _ in range(4):
        _apply(_planned(), c)

    rec = FakeReconciler()
    _apply(TurnOutcome(kind="task_blocked", task=FakeTask(), reason="dep"), c,
           rec=rec)
    assert rec.calls == ["block_task"]
    assert c.plan_streak == 0


def test_every_pr_outcome_resets_plan_streak() -> None:
    for kind in ("pr_opened", "pr_reviewed", "pr_tested", "pr_conflict",
                 "pr_skipped", "pr_merged"):
        c = LoopCounters()
        for _ in range(4):
            _apply(_planned(), c)
        assert c.plan_streak == 4, kind
        _apply(TurnOutcome(kind=kind), c)
        assert c.plan_streak == 0, kind


# --------------------------------------------------------------------------- #
# 2. _account_planning_churn: trips exactly at the limit; 0 disables.
# --------------------------------------------------------------------------- #

def test_trips_exactly_at_the_limit_not_before() -> None:
    limit = 6
    policy = CodingAutonomyPolicy(plan_streak_limit=limit)
    led = FakeLedger()
    c = LoopCounters()
    stop = None
    for i in range(1, limit + 1):
        _apply(_planned(), c, ledger=led)
        stop = _account_planning_churn(led, c, policy)
        if i < limit:
            assert stop is None, f"tripped early after {i} plan turn(s)"
    assert stop is not None and stop.stop_reason == PLANNING_CHURN
    assert stop.counters is c and c.plan_streak == limit


def test_a_worker_turn_before_the_limit_prevents_the_trip() -> None:
    """A legitimate decomposition burst that actually dispatches work never trips."""
    limit = 3
    policy = CodingAutonomyPolicy(plan_streak_limit=limit)
    led = FakeLedger()
    c = LoopCounters()
    for _ in range(20):
        for _ in range(limit - 1):
            _apply(_planned(), c, ledger=led)
            assert _account_planning_churn(led, c, policy) is None
        _apply(TurnOutcome(kind="pr_merged"), c, ledger=led)
        assert _account_planning_churn(led, c, policy) is None


def test_limit_zero_never_trips() -> None:
    policy = CodingAutonomyPolicy(plan_streak_limit=0)
    led = FakeLedger()
    c = LoopCounters()
    for _ in range(50):
        _apply(_planned(), c, ledger=led)
        assert _account_planning_churn(led, c, policy) is None


def test_a_bare_ledger_never_breaks_the_detector() -> None:
    class Bare:
        pass

    policy = CodingAutonomyPolicy(plan_streak_limit=2)
    c = LoopCounters(plan_streak=5)
    stop = _account_planning_churn(Bare(), c, policy)
    assert stop is not None and stop.stop_reason == PLANNING_CHURN


# --------------------------------------------------------------------------- #
# 3. The diagnosis pair: backlog depth + DISTINCT open titles.
# --------------------------------------------------------------------------- #

def test_backlog_shape_counts_open_tasks_and_distinct_titles() -> None:
    led = FakeLedger([
        FakeTask("t-1", "Fix the parser harness", "todo"),
        FakeTask("t-2", "Create the parser harness", "todo"),   # same job restated
        FakeTask("t-3", "Consolidate the parser harness", "doing"),
        FakeTask("t-4", "Write the release notes", "todo"),
        FakeTask("t-5", "Fix the parser harness", "done"),      # not open
        FakeTask("t-6", "Something dropped", "dropped"),        # not open
    ])
    depth, distinct = _open_backlog_shape(led)
    assert depth == 4
    assert distinct == 2  # the harness restatements collapse to one


def test_backlog_shape_is_best_effort() -> None:
    class Exploding:
        def list_tasks(self) -> list:
            raise RuntimeError("boom")

    assert _open_backlog_shape(Exploding()) == (0, 0)


# --------------------------------------------------------------------------- #
# 4. Policy round-trip.
# --------------------------------------------------------------------------- #

def test_policy_round_trip_preserves_an_explicit_limit() -> None:
    p = policy_from_dict(policy_to_dict(CodingAutonomyPolicy(plan_streak_limit=3)))
    assert p.plan_streak_limit == 3


def test_absent_key_defaults_to_six() -> None:
    assert CodingAutonomyPolicy().plan_streak_limit == 6
    assert policy_from_dict({}).plan_streak_limit == 6


def test_negative_clamps_to_zero_not_one() -> None:
    """`max(0, …)` — NOT max(1) — so an operator can disable the detector."""
    assert policy_from_dict({"plan_streak_limit": -5}).plan_streak_limit == 0
    assert policy_from_dict({"plan_streak_limit": 0}).plan_streak_limit == 0


def test_policy_dict_exposes_the_knob() -> None:
    assert policy_to_dict(CodingAutonomyPolicy())["plan_streak_limit"] == 6


# --------------------------------------------------------------------------- #
# 5. Regression for the already-landed half (Spec 08 + honest `made_progress`):
#    an all-duplicate plan batch must NOT look like progress, so pm_idle climbs.
# --------------------------------------------------------------------------- #

def _pm_intent(tasks: list[dict]) -> Any:
    envelope = json.dumps({
        "schema_version": "coding_turn.v1", "role": "pm",
        "intent": {"kind": "plan", "done": False, "tasks": tasks},
    })
    parsed = parse_coding_turn("pm", None, envelope)
    assert not isinstance(parsed, TurnParseError), parsed
    return parsed.intent


def test_all_duplicate_plan_batch_increments_pm_idle(tmp_path: Path) -> None:
    store = LedgerStore("pchurn", root=tmp_path)
    store.create_project(north_star="n", definition_of_done="d", target="new",
                         repo_path=None)
    batch = [{"title": "Fix the parser harness", "role": "dev",
              "detail": "Acceptance: the parser harness runs."}]

    first = _materialize_pm_tasks(store, _pm_intent(batch))
    assert len(first) == 1  # the genuine create

    # The same batch again: every task is a duplicate of an OPEN one, so nothing
    # is created -> the runner's `made_progress=len(created) > 0` goes False.
    repeat = _materialize_pm_tasks(store, _pm_intent(batch))
    assert repeat == []

    c = LoopCounters()
    _apply(TurnOutcome(kind="planned", made_progress=len(repeat) > 0), c,
           ledger=store)
    assert c.pm_idle == 1
    _apply(TurnOutcome(kind="planned", made_progress=len(repeat) > 0), c,
           ledger=store)
    assert c.pm_idle == 2  # >= the default pm_idle_limit -> NO_PROGRESS re-armed
    assert c.pm_idle >= CodingAutonomyPolicy().pm_idle_limit
    # ...and the planning-churn counter tracked those same turns.
    assert c.plan_streak == 2
