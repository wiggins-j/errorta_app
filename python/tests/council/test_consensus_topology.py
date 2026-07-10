"""ConsensusDeliberationTopology — unit tests."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from errorta_council.limits import SchedulerPolicy
from errorta_council.state import RunCounters
from errorta_council.topologies.consensus import (
    ConsensusDeliberationTopology,
    _is_no_change_signal,
)
from errorta_council.topologies.round_robin import RunCompletion, TurnProposal


@dataclass(frozen=True)
class _FakeEv:
    type: str
    member_id: str | None = None
    round: int | None = None
    sequence: int = 0
    payload: dict = field(default_factory=dict)


def _state(
    *, members, counters, policy, events,
):
    return {"members": members, "counters": counters, "policy": policy, "events": events}


def _policy(*, max_rounds=3, max_messages_per_member=3, max_total=20):
    return SchedulerPolicy(
        max_rounds=max_rounds,
        max_messages_per_member=max_messages_per_member,
        max_total_member_messages=max_total,
        per_turn_timeout_seconds=120,
    )


def _members(n: int):
    return [{"id": f"m-{i}", "enabled": True} for i in range(1, n + 1)]


def _counters(*, attempts=None, completed=None, totals=0, round_index=1):
    return RunCounters(
        round_index=round_index,
        total_messages_completed=totals,
        completed_messages_by_member=dict(completed or {}),
        attempts_by_member=dict(attempts or {}),
    )


# ---- _is_no_change_signal ------------------------------------------------


def test_no_change_digest_delta_null():
    content = '{"v":"digest_v1","position":"P","claims":[],"agree":[],"dispute":[],"delta":null,"open":[],"answer_fragment":""}'
    assert _is_no_change_signal(content) is True


def test_no_change_digest_delta_string():
    content = '{"v":"digest_v1","position":"P","claims":[],"agree":[],"dispute":[],"delta":"no_changed_views","open":[],"answer_fragment":""}'
    assert _is_no_change_signal(content) is True


def test_change_digest_delta_nonempty():
    content = '{"v":"digest_v1","position":"P","claims":[],"agree":[],"dispute":[],"delta":"I revised my answer because m-1 was right","open":[],"answer_fragment":""}'
    assert _is_no_change_signal(content) is False


def test_no_change_prose_marker():
    assert _is_no_change_signal("Paris. No changed views.") is True


def test_no_change_plain_prose_no_marker():
    assert _is_no_change_signal("Paris is the capital of France.") is False


def test_no_change_empty_content():
    assert _is_no_change_signal("") is False


def test_no_change_non_json_garbage():
    assert _is_no_change_signal("not json {neither this") is False


def test_thinking_burn_does_not_count_as_consensus():
    """A thinking-burn (no visible answer) is NOT agreement. Counting silence
    as 'no change' produced hollow consensus where a member never actually
    voiced a position — so it must read as not-no-change (still deliberating).
    The fix for a chronically-burning model is a larger output budget."""
    assert _is_no_change_signal(
        "(reasoning trace, no visible answer) Thinking Process: blah blah"
    ) is False


# ---- topology propose_next -----------------------------------------------


def test_round_one_dispatches_first_member_with_cursor_zero_freeze():
    topo = ConsensusDeliberationTopology()
    events = [_FakeEv(type="run_started", sequence=0)]
    state = _state(
        members=_members(3),
        counters=_counters(),
        policy=_policy(),
        events=events,
    )
    p = topo.propose_next(state, transcript=[])
    assert isinstance(p, TurnProposal)
    assert p.member_id == "m-1"
    assert p.round == 1
    # round-1 cursor freeze sits AFTER run_started
    assert p.transcript_cursor == 1


def test_round_one_serves_all_members_at_frozen_cursor():
    topo = ConsensusDeliberationTopology()
    events = [_FakeEv(type="run_started", sequence=0)]
    counters = _counters(attempts={"m-1": 1})
    state = _state(members=_members(3), counters=counters, policy=_policy(), events=events)
    p = topo.propose_next(state, transcript=[])
    assert isinstance(p, TurnProposal)
    assert p.member_id == "m-2"
    assert p.round == 1
    assert p.transcript_cursor == 1


def test_round_two_drops_cursor_freeze():
    topo = ConsensusDeliberationTopology()
    events = [
        _FakeEv(type="run_started", sequence=0),
        _FakeEv(type="member_message", member_id="m-1", round=1, sequence=1,
                payload={"content": '{"v":"digest_v1","position":"a","claims":[],"delta":"revised"}'}),
        _FakeEv(type="member_message", member_id="m-2", round=1, sequence=2,
                payload={"content": '{"v":"digest_v1","position":"b","claims":[],"delta":"refined"}'}),
        _FakeEv(type="member_message", member_id="m-3", round=1, sequence=3,
                payload={"content": '{"v":"digest_v1","position":"c","claims":[],"delta":"refined"}'}),
    ]
    counters = _counters(attempts={"m-1": 1, "m-2": 1, "m-3": 1}, totals=3)
    state = _state(members=_members(3), counters=counters, policy=_policy(), events=events)
    p = topo.propose_next(state, transcript=[])
    assert isinstance(p, TurnProposal)
    assert p.round == 2
    assert p.transcript_cursor is None  # round 2 sees full transcript


def test_consensus_reached_when_all_signal_no_change():
    topo = ConsensusDeliberationTopology()  # default threshold = all enabled
    events = [
        _FakeEv(type="run_started", sequence=0),
        _FakeEv(type="member_message", member_id="m-1", round=1, sequence=1,
                payload={"content": '{"v":"digest_v1","position":"a","claims":[],"delta":null}'}),
        _FakeEv(type="member_message", member_id="m-2", round=1, sequence=2,
                payload={"content": '{"v":"digest_v1","position":"a","claims":[],"delta":"no_changed_views"}'}),
        _FakeEv(type="member_message", member_id="m-3", round=1, sequence=3,
                payload={"content": '{"v":"digest_v1","position":"a","claims":[],"delta":null}'}),
    ]
    counters = _counters(attempts={"m-1": 1, "m-2": 1, "m-3": 1}, totals=3)
    state = _state(members=_members(3), counters=counters, policy=_policy(), events=events)
    p = topo.propose_next(state, transcript=[])
    assert isinstance(p, RunCompletion)
    assert p.reason == "consensus_reached"


def test_no_consensus_advances_round_2():
    topo = ConsensusDeliberationTopology()  # threshold = all 3
    events = [
        _FakeEv(type="run_started", sequence=0),
        _FakeEv(type="member_message", member_id="m-1", round=1, sequence=1,
                payload={"content": '{"v":"digest_v1","position":"a","claims":[],"delta":null}'}),
        _FakeEv(type="member_message", member_id="m-2", round=1, sequence=2,
                payload={"content": '{"v":"digest_v1","position":"b","claims":[],"delta":"refined"}'}),
        _FakeEv(type="member_message", member_id="m-3", round=1, sequence=3,
                payload={"content": '{"v":"digest_v1","position":"c","claims":[],"delta":null}'}),
    ]
    counters = _counters(attempts={"m-1": 1, "m-2": 1, "m-3": 1}, totals=3)
    state = _state(members=_members(3), counters=counters, policy=_policy(), events=events)
    p = topo.propose_next(state, transcript=[])
    assert isinstance(p, TurnProposal)
    assert p.round == 2


def test_partial_consensus_stops_when_threshold_2():
    topo = ConsensusDeliberationTopology(consensus_threshold=2)
    events = [
        _FakeEv(type="run_started", sequence=0),
        _FakeEv(type="member_message", member_id="m-1", round=1, sequence=1,
                payload={"content": '{"v":"digest_v1","position":"a","claims":[],"delta":null}'}),
        _FakeEv(type="member_message", member_id="m-2", round=1, sequence=2,
                payload={"content": '{"v":"digest_v1","position":"b","claims":[],"delta":"refined"}'}),
        _FakeEv(type="member_message", member_id="m-3", round=1, sequence=3,
                payload={"content": '{"v":"digest_v1","position":"a","claims":[],"delta":"no_changed_views"}'}),
    ]
    counters = _counters(attempts={"m-1": 1, "m-2": 1, "m-3": 1}, totals=3)
    state = _state(members=_members(3), counters=counters, policy=_policy(), events=events)
    p = topo.propose_next(state, transcript=[])
    assert isinstance(p, RunCompletion)
    assert p.reason == "consensus_reached"


def test_max_rounds_hard_cap():
    topo = ConsensusDeliberationTopology()
    events = [
        _FakeEv(type="run_started", sequence=0),
        # 3 members each completed 2 attempts: round 2 fully done
        *[
            _FakeEv(type="member_message", member_id=f"m-{i}", round=r, sequence=s,
                    payload={"content": '{"v":"digest_v1","delta":"refined"}'})
            for s, (r, i) in enumerate(
                [(1, 1), (1, 2), (1, 3), (2, 1), (2, 2), (2, 3)], start=1
            )
        ],
    ]
    counters = _counters(attempts={"m-1": 2, "m-2": 2, "m-3": 2}, totals=6)
    p = topo.propose_next(
        _state(members=_members(3), counters=counters,
               policy=_policy(max_rounds=2), events=events),
        transcript=[],
    )
    assert isinstance(p, RunCompletion)
    # Either consensus or limits_exhausted is acceptable; with all "refined"
    # answers the no-change count is 0 so it's limits_exhausted via max_rounds.
    assert p.reason == "limits_exhausted"


def test_no_eligible_members_returns_no_eligible():
    topo = ConsensusDeliberationTopology()
    state = _state(
        members=[{"id": "m-1", "enabled": False}],
        counters=_counters(),
        policy=_policy(),
        events=[_FakeEv(type="run_started")],
    )
    p = topo.propose_next(state, transcript=[])
    assert isinstance(p, RunCompletion)
    assert p.reason == "no_eligible_members"
