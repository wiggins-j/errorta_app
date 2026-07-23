"""Spec 10 — dispatch fairness (head-of-line) + the wedged-graph probe.

The observed failure: 130 todo tasks, 6 healthy members, ZERO worker turns for
10+ iterations, and nothing anywhere said why. The operator had to hand-inspect
`backlog.jsonl` and the topology source to discover the graph was wedged.

Covered here:
* Head-of-line fix (`decide_next`): a poisoned HEAD task that excludes every
  member of a role no longer starves the whole role — dispatch advances to the
  next dispatchable TASK.
* The `_account_dispatch_wedge` detector: trips (naming the culprit dep) only
  after the sustained window, never on a healthy backlog, and `0` disables it.
* `_dispatch_wedge_culprits` culprit-naming: `blocked` + stranded `doing` deps
  are named with their block counts; `done`/`dropped` (satisfied) are not.
* Policy round-trip for both new knobs (defaults, clamps).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from errorta_council.coding.autonomy import (
    DISPATCH_WEDGED,
    CodingAutonomyPolicy,
    LoopCounters,
    _account_dispatch_wedge,
    _dispatch_wedge_culprits,
    policy_from_dict,
    policy_to_dict,
)
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.topology import (
    Assign,
    Complete,
    Plan,
    decide_next,
)


def _store(tmp_path: Path, name: str = "wedge-proj") -> LedgerStore:
    s = LedgerStore(name, root=tmp_path)
    s.create_project(
        north_star="n", definition_of_done="d", target="new", repo_path=None)
    return s


# --------------------------------------------------------------------------- #
# 1. Head-of-line: a poisoned head task no longer starves the whole role.
# --------------------------------------------------------------------------- #

def test_head_of_line_excluded_head_does_not_starve_the_role(tmp_path: Path) -> None:
    """The HEAD dev task excludes every dev member; a LATER todo dev task does
    not. Previously `decide_next` abandoned the whole role at the head (returning
    a PM plan turn); now it advances to the later dispatchable task."""
    store = _store(tmp_path)
    head = store.add_task(title="poisoned head", role="dev", detail="a")
    later = store.add_task(title="workable task", role="dev", detail="b")
    # Bar BOTH dev members from the head only (F127 escalate-up exhausted).
    store.update_task(head.task_id, excluded_member_ids=["m-1", "m-2"])

    members = [("m-1", "dev"), ("m-2", "dev"), ("pm-1", "pm")]
    action = decide_next(store, members)

    assert isinstance(action, Assign), action
    assert action.task_id == later.task_id  # the later task, NOT the head
    assert action.role == "dev"


def test_head_of_line_all_tasks_excluded_falls_through_to_pm(tmp_path: Path) -> None:
    """When EVERY dev task bars every dev member the role legitimately yields —
    the PM gets its plan turn (the attention ladder handles the barred tasks)."""
    store = _store(tmp_path)
    t1 = store.add_task(title="poisoned 1", role="dev", detail="a")
    t2 = store.add_task(title="poisoned 2", role="dev", detail="b")
    store.update_task(t1.task_id, excluded_member_ids=["m-1"])
    store.update_task(t2.task_id, excluded_member_ids=["m-1"])

    action = decide_next(store, [("m-1", "dev"), ("pm-1", "pm")])
    assert isinstance(action, Plan)


def test_head_of_line_unexcluded_head_still_dispatches_first(tmp_path: Path) -> None:
    """Regression guard: with no exclusions the HEAD is still dispatched first —
    the fix must not reorder normal dispatch."""
    store = _store(tmp_path)
    head = store.add_task(title="head", role="dev", detail="a")
    store.add_task(title="later", role="dev", detail="b")

    action = decide_next(store, [("m-1", "dev")])
    assert isinstance(action, Assign) and action.task_id == head.task_id


# --------------------------------------------------------------------------- #
# 2. The wedge probe: N todo tasks stranded behind one `doing` id.
# --------------------------------------------------------------------------- #

def _wedged_store(tmp_path: Path, n: int = 12) -> tuple[LedgerStore, str]:
    """`n` todo dev tasks, each depending on one stranded `doing` prerequisite."""
    store = _store(tmp_path)
    stuck = store.add_task(title="the stranded prerequisite", role="dev", detail="x")
    store.update_task(stuck.task_id, state="doing", assignee_member_id="m-1")
    for i in range(n):
        store.add_task(title=f"waiter {i}", role="dev", detail="w",
                       depends_on=[stuck.task_id])
    return store, stuck.task_id


def test_wedge_trips_after_the_sustained_window_and_names_the_culprit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, stuck_id = _wedged_store(tmp_path, n=12)
    policy = CodingAutonomyPolicy(wedge_min_tasks=10, wedge_stall_limit=5)
    c = LoopCounters()

    raised: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "errorta_council.coding.autonomy._maybe_raise_monitor",
        lambda ledger, detector, reason: raised.append((detector, reason)),
    )

    stop = None
    for i in range(1, policy.wedge_stall_limit + 1):
        stop = _account_dispatch_wedge(store, c, policy)
        if i < policy.wedge_stall_limit:
            assert stop is None, f"tripped early after {i} iteration(s)"
    assert stop is not None and stop.stop_reason == DISPATCH_WEDGED
    assert c.wedge_streak == policy.wedge_stall_limit
    # The monitor was raised, naming the stranded prerequisite id + its block count.
    assert raised and raised[-1][0] == "dispatch_wedged"
    detail = raised[-1][1]
    assert stuck_id in detail
    assert "blocks 12" in detail
    # The stop result also carries the summary for the CLI triage line.
    assert stuck_id in stop.detail.get("summary", "")


def test_wedge_never_trips_on_a_healthy_dispatchable_backlog(tmp_path: Path) -> None:
    """A large backlog with genuinely dispatchable work must never trip."""
    store = _store(tmp_path)
    for i in range(20):
        store.add_task(title=f"ready {i}", role="dev", detail="r")  # no deps -> ready
    policy = CodingAutonomyPolicy(wedge_min_tasks=10, wedge_stall_limit=5)
    c = LoopCounters()
    for _ in range(50):
        assert _account_dispatch_wedge(store, c, policy) is None
        assert c.wedge_streak == 0


def test_wedge_does_not_trip_below_the_task_floor(tmp_path: Path) -> None:
    """Fewer than `wedge_min_tasks` todo tasks (even if wedged) never trips —
    a legitimately small/empty backlog is not a wedge."""
    store, _ = _wedged_store(tmp_path, n=3)  # < default floor of 10
    policy = CodingAutonomyPolicy(wedge_min_tasks=10, wedge_stall_limit=2)
    c = LoopCounters()
    for _ in range(10):
        assert _account_dispatch_wedge(store, c, policy) is None


def test_wedge_streak_resets_when_work_becomes_dispatchable(tmp_path: Path) -> None:
    """A stranded `doing` prerequisite that finally lands makes the waiters
    dispatchable — the streak resets and the run does not stop."""
    store, stuck_id = _wedged_store(tmp_path, n=12)
    policy = CodingAutonomyPolicy(wedge_min_tasks=10, wedge_stall_limit=5)
    c = LoopCounters()
    # Build up a partial streak.
    for _ in range(policy.wedge_stall_limit - 1):
        assert _account_dispatch_wedge(store, c, policy) is None
    assert c.wedge_streak == policy.wedge_stall_limit - 1
    # The prerequisite lands -> waiters are dispatchable now.
    store.update_task(stuck_id, state="done")
    assert _account_dispatch_wedge(store, c, policy) is None
    assert c.wedge_streak == 0


def test_wedge_stall_limit_zero_disables_the_detector(tmp_path: Path) -> None:
    store, _ = _wedged_store(tmp_path, n=20)
    policy = CodingAutonomyPolicy(wedge_min_tasks=10, wedge_stall_limit=0)
    c = LoopCounters()
    for _ in range(50):
        assert _account_dispatch_wedge(store, c, policy) is None
        assert c.wedge_streak == 0


def test_wedge_detector_is_best_effort_on_a_bare_ledger() -> None:
    class Bare:
        pass

    policy = CodingAutonomyPolicy(wedge_min_tasks=10, wedge_stall_limit=2)
    c = LoopCounters(wedge_streak=5)
    assert _account_dispatch_wedge(Bare(), c, policy) is None
    assert c.wedge_streak == 0  # reset, never stops


# --------------------------------------------------------------------------- #
# 3. Culprit naming: blocked + stranded doing are named; done/dropped are not.
# --------------------------------------------------------------------------- #

def test_culprits_name_blocked_and_doing_not_satisfied_deps(tmp_path: Path) -> None:
    store = _store(tmp_path)
    blocked = store.add_task(title="blocked dep", role="dev", detail="b")
    store.update_task(blocked.task_id, state="blocked")
    doing = store.add_task(title="doing dep", role="dev", detail="d")
    store.update_task(doing.task_id, state="doing", assignee_member_id="m-1")
    dropped = store.add_task(title="dropped dep", role="dev", detail="dr")
    store.update_task(dropped.task_id, state="dropped")
    done = store.add_task(title="done dep", role="dev", detail="dn")
    store.update_task(done.task_id, state="done")

    # A todo task depending on ALL of them.
    waiter = store.add_task(
        title="waiter", role="dev", detail="w",
        depends_on=[blocked.task_id, doing.task_id, dropped.task_id, done.task_id])
    todo = store.list_tasks(state="todo")
    assert todo and todo[0].task_id == waiter.task_id

    summary = _dispatch_wedge_culprits(store, todo)
    # blocked + doing are non-satisfiable culprits (Spec 09: dropped/done are NOT).
    assert blocked.task_id in summary
    assert doing.task_id in summary
    assert dropped.task_id not in summary
    assert done.task_id not in summary


def test_culprits_walk_transitively_to_the_root_strand(tmp_path: Path) -> None:
    """A -> B(todo) -> C(doing): the root stranded C is named, not the todo B."""
    store = _store(tmp_path)
    root = store.add_task(title="root strand", role="dev", detail="c")
    store.update_task(root.task_id, state="doing", assignee_member_id="m-1")
    mid = store.add_task(title="mid todo", role="dev", detail="b",
                         depends_on=[root.task_id])
    store.add_task(title="head todo", role="dev", detail="a",
                   depends_on=[mid.task_id])
    todo = store.list_tasks(state="todo")

    summary = _dispatch_wedge_culprits(store, todo)
    assert root.task_id in summary          # the reachable stranded root
    assert mid.task_id not in summary       # a plain todo dep is not a culprit


def test_culprits_report_role_invisible_backlog_when_no_bad_dep(tmp_path: Path) -> None:
    """No non-satisfiable dep but nothing dispatchable (e.g. a role-invisible
    backlog) still yields a legible summary rather than an empty string."""
    store = _store(tmp_path)
    # A pm-role task with no deps: never dispatchable via the worker path, and no
    # non-satisfiable dependency exists.
    for i in range(3):
        store.add_task(title=f"resolve attention {i}", role="pm", detail="p")
    todo = store.list_tasks(state="todo")
    summary = _dispatch_wedge_culprits(store, todo)
    assert "3 todo task(s)" in summary
    assert "role-invisible" in summary


# --------------------------------------------------------------------------- #
# 4. Policy round-trip for both new knobs.
# --------------------------------------------------------------------------- #

def test_policy_defaults() -> None:
    p = CodingAutonomyPolicy()
    assert p.wedge_min_tasks == 10
    assert p.wedge_stall_limit == 5
    assert policy_from_dict({}).wedge_min_tasks == 10
    assert policy_from_dict({}).wedge_stall_limit == 5


def test_policy_round_trip_preserves_explicit_values() -> None:
    p = policy_from_dict(policy_to_dict(
        CodingAutonomyPolicy(wedge_min_tasks=25, wedge_stall_limit=8)))
    assert p.wedge_min_tasks == 25
    assert p.wedge_stall_limit == 8


def test_policy_dict_exposes_both_knobs() -> None:
    d = policy_to_dict(CodingAutonomyPolicy())
    assert d["wedge_min_tasks"] == 10
    assert d["wedge_stall_limit"] == 5


def test_policy_clamps_negatives_to_zero_not_one() -> None:
    """`max(0, …)` — so `wedge_stall_limit=0` disables the detector."""
    assert policy_from_dict({"wedge_stall_limit": -5}).wedge_stall_limit == 0
    assert policy_from_dict({"wedge_stall_limit": 0}).wedge_stall_limit == 0
    assert policy_from_dict({"wedge_min_tasks": -3}).wedge_min_tasks == 0


# --------------------------------------------------------------------------- #
# 5. The `no_actionable_work` case (legitimately idle) must NOT be a wedge.
# --------------------------------------------------------------------------- #

def test_empty_backlog_is_not_a_wedge(tmp_path: Path) -> None:
    store = _store(tmp_path)
    policy = CodingAutonomyPolicy(wedge_min_tasks=10, wedge_stall_limit=2)
    c = LoopCounters()
    for _ in range(10):
        assert _account_dispatch_wedge(store, c, policy) is None
    # And decide_next with no PM completes cleanly, unrelated to the wedge.
    assert isinstance(decide_next(store, [("m-1", "dev")]), Complete)
