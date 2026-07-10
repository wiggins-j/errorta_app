"""F078 — CredibilityTopology: round-robin across configured rounds, then
hand off to the credibility finalizer (reason="finalized")."""
from __future__ import annotations

from errorta_council.limits import SchedulerPolicy
from errorta_council.state import RunCounters
from errorta_council.topologies.credibility import CredibilityTopology
from errorta_council.topologies.round_robin import RunCompletion, TurnProposal


def _members(ids: list[str]) -> list[dict]:
    return [{"id": mid, "enabled": True, "role": "member"} for mid in ids]


def _state(counters: RunCounters, max_rounds: int = 3) -> dict:
    return {
        "members": _members(["m1", "m2"]),
        "counters": counters,
        "policy": SchedulerPolicy(max_rounds=max_rounds, per_turn_timeout_seconds=30),
    }


def test_first_turn_is_a_proposal() -> None:
    topo = CredibilityTopology()
    p = topo.propose_next(_state(RunCounters()), transcript=[])
    assert isinstance(p, TurnProposal)
    assert p.member_id == "m1" and p.round == 1


def test_gives_members_multiple_rounds() -> None:
    topo = CredibilityTopology()
    # After round 1 both spoke once → round 2 proposed (research, claim, review
    # need more than 2 turns; the room configures the headroom).
    counters = RunCounters(
        completed_messages_by_member={"m1": 1, "m2": 1},
        total_messages_completed=2, round_index=1,
    )
    p = topo.propose_next(_state(counters, max_rounds=3), transcript=[])
    assert isinstance(p, TurnProposal) and p.round == 2


def test_finalizes_when_rounds_exhausted() -> None:
    topo = CredibilityTopology()
    counters = RunCounters(
        completed_messages_by_member={"m1": 3, "m2": 3},
        total_messages_completed=6, round_index=3,
    )
    c = topo.propose_next(_state(counters, max_rounds=3), transcript=[])
    assert isinstance(c, RunCompletion)
    assert c.reason == "finalized"
    assert c.detail and c.detail["topology"] == "credibility"
    assert "underlying_reason" in c.detail
