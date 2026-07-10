from __future__ import annotations

from errorta_council.limits import SchedulerPolicy
from errorta_council.state import RunCounters
from errorta_council.topologies.round_robin import (
    RoundRobinTopology,
    RunCompletion,
    TurnProposal,
)


def _members(ids: list[str], disabled: set[str] | None = None) -> list[dict]:
    disabled = disabled or set()
    return [{"id": mid, "enabled": mid not in disabled, "role": "member"} for mid in ids]


def test_first_turn_picks_first_enabled_member() -> None:
    topo = RoundRobinTopology()
    policy = SchedulerPolicy(max_rounds=2, per_turn_timeout_seconds=30)
    state = {
        "members": _members(["m1", "m2"]),
        "counters": RunCounters(),
        "policy": policy,
    }
    proposal = topo.propose_next(state, transcript=[])
    assert isinstance(proposal, TurnProposal)
    assert proposal.member_id == "m1"
    assert proposal.round == 1
    assert proposal.turn_index == 0


def test_round_increments_after_full_pass() -> None:
    topo = RoundRobinTopology()
    policy = SchedulerPolicy(max_rounds=2, per_turn_timeout_seconds=30)
    counters = RunCounters(
        completed_messages_by_member={"m1": 1, "m2": 1},
        total_messages_completed=2,
        round_index=1,
    )
    state = {
        "members": _members(["m1", "m2"]),
        "counters": counters,
        "policy": policy,
    }
    proposal = topo.propose_next(state, transcript=[])
    assert isinstance(proposal, TurnProposal)
    assert proposal.member_id == "m1"
    assert proposal.round == 2


def test_disabled_member_is_skipped() -> None:
    topo = RoundRobinTopology()
    policy = SchedulerPolicy(max_rounds=1, per_turn_timeout_seconds=30)
    state = {
        "members": _members(["m1", "m2", "m3"], disabled={"m2"}),
        "counters": RunCounters(),
        "policy": policy,
    }
    p1 = topo.propose_next(state, transcript=[])
    assert p1.member_id == "m1"
    state["counters"] = RunCounters(
        completed_messages_by_member={"m1": 1}, total_messages_completed=1, round_index=1
    )
    p2 = topo.propose_next(state, transcript=[])
    assert p2.member_id == "m3"


def test_max_rounds_terminates() -> None:
    topo = RoundRobinTopology()
    policy = SchedulerPolicy(max_rounds=1, per_turn_timeout_seconds=30)
    counters = RunCounters(
        completed_messages_by_member={"m1": 1, "m2": 1},
        total_messages_completed=2,
        round_index=1,
    )
    state = {
        "members": _members(["m1", "m2"]),
        "counters": counters,
        "policy": policy,
    }
    result = topo.propose_next(state, transcript=[])
    assert isinstance(result, RunCompletion)
    assert result.reason == "limits_exhausted"


def test_per_member_cap_terminates() -> None:
    topo = RoundRobinTopology()
    policy = SchedulerPolicy(
        max_rounds=3, max_messages_per_member=1, per_turn_timeout_seconds=30
    )
    counters = RunCounters(
        completed_messages_by_member={"m1": 1, "m2": 1},
        total_messages_completed=2,
        round_index=1,
    )
    state = {
        "members": _members(["m1", "m2"]),
        "counters": counters,
        "policy": policy,
    }
    result = topo.propose_next(state, transcript=[])
    assert isinstance(result, RunCompletion)
    assert result.reason == "limits_exhausted"


def test_total_cap_preempts_per_member() -> None:
    topo = RoundRobinTopology()
    policy = SchedulerPolicy(
        max_rounds=10,
        max_messages_per_member=10,
        max_total_member_messages=2,
        per_turn_timeout_seconds=30,
    )
    counters = RunCounters(
        completed_messages_by_member={"m1": 1, "m2": 1},
        total_messages_completed=2,
        round_index=1,
    )
    state = {
        "members": _members(["m1", "m2"]),
        "counters": counters,
        "policy": policy,
    }
    result = topo.propose_next(state, transcript=[])
    assert isinstance(result, RunCompletion)
    assert result.reason == "limits_exhausted"


def test_no_enabled_members_yields_no_eligible_members() -> None:
    topo = RoundRobinTopology()
    policy = SchedulerPolicy(max_rounds=1, per_turn_timeout_seconds=30)
    state = {
        "members": _members(["m1"], disabled={"m1"}),
        "counters": RunCounters(),
        "policy": policy,
    }
    result = topo.propose_next(state, transcript=[])
    assert isinstance(result, RunCompletion)
    assert result.reason == "no_eligible_members"
