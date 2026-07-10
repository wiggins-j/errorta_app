"""F081 slice 2 — novelty-exhaustion termination + quality_flag plumbing."""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from errorta_council.credibility.report import CredibilityReport
from errorta_council.scheduler import TurnScheduler
from errorta_council.schema import EventType


@dataclass
class _Ev:
    type: Any = None  # noqa: F821 - EventType
    round: int | None = None
    member_id: str | None = None
    payload: dict = field(default_factory=dict)


from typing import Any  # noqa: E402


def _review_msg(reviewer, claim_id, status, rnd):
    return _Ev(type=EventType.MEMBER_MESSAGE, round=rnd, member_id=reviewer,
               payload={"content": json.dumps({"reviews": [
                   {"claim_id": claim_id, "status": status}]})})


class _FakeStore:
    def __init__(self, events):
        self._events = events

    def read_run(self, _run_id):
        return None, self._events


class _Meta:
    id = "r1"

    def __init__(self, snapshot):
        self.room_snapshot = snapshot


def _sched(events, *, novelty_rounds=2):
    s = TurnScheduler.__new__(TurnScheduler)
    s._meta = _Meta({
        "credibility_policy": {"enabled": True, "rigor": "standard",
                               "require_entailment": True,
                               "novelty_exhaustion_rounds": novelty_rounds},
        "finalization_policy": {"mode": "credibility_report"},
    })
    s._store = _FakeStore(events)
    return s


def test_quality_flag_round_trips_on_report():
    rep = CredibilityReport(quality_flag="unchallenged_consensus")
    assert rep.to_dict()["quality_flag"] == "unchallenged_consensus"
    assert CredibilityReport().to_dict()["quality_flag"] == ""


def test_novelty_set_counts_claims_sources_reviews():
    events = [
        _Ev(type=EventType.CREDIBILITY_SOURCE_CAPTURED, round=0,
            payload={"content_sha256": "s1"}),
        _Ev(type=EventType.CREDIBILITY_ENTAILMENT_CHECKED, round=1,
            payload={"claim_id": "A:c1", "grade": "entails"}),
        _Ev(type=EventType.CREDIBILITY_ENTAILMENT_CHECKED, round=1,
            payload={"claim_id": "A:c2", "grade": "unsupported"}),  # not novelty
        _review_msg("B", "A:c1", "verified", 2),
    ]
    s = _sched(events)
    got = s._credibility_novelty_set(events, up_to_round=2)
    assert ("src", "s1") in got
    assert ("claim", "A:c1") in got
    assert ("claim", "A:c2") not in got  # unsupported doesn't count as new evidence
    assert ("rev", "B", "A:c1", "verified") in got


def test_novelty_stop_fires_when_rounds_add_nothing():
    # Round 1 establishes a claim + source; rounds 2 & 3 repeat the SAME review.
    events = [
        _Ev(type=EventType.CREDIBILITY_SOURCE_CAPTURED, round=0,
            payload={"content_sha256": "s1"}),
        _Ev(type=EventType.CREDIBILITY_ENTAILMENT_CHECKED, round=1,
            payload={"claim_id": "A:c1", "grade": "entails"}),
        _review_msg("B", "A:c1", "verified", 1),
        _review_msg("B", "A:c1", "verified", 2),  # repeat — no novelty
        _review_msg("B", "A:c1", "verified", 3),  # repeat — no novelty
    ]
    s = _sched(events, novelty_rounds=2)
    # About to start round 4: rounds 2 and 3 added nothing → stop.
    assert s._maybe_credibility_novelty_stop(4) == "novelty_exhausted"


def test_novelty_stop_holds_off_while_new_material_arrives():
    events = [
        _Ev(type=EventType.CREDIBILITY_ENTAILMENT_CHECKED, round=1,
            payload={"claim_id": "A:c1", "grade": "entails"}),
        _review_msg("B", "A:c1", "verified", 1),
        _review_msg("C", "A:c1", "verified", 2),   # NEW reviewer — novelty
        _review_msg("B", "A:c2", "contradicted", 3),  # NEW review — novelty
    ]
    s = _sched(events, novelty_rounds=2)
    assert s._maybe_credibility_novelty_stop(4) is None


def test_novelty_stop_off_when_not_required():
    s = TurnScheduler.__new__(TurnScheduler)
    s._meta = _Meta({"credibility_policy": {"enabled": True, "rigor": "lenient"},
                     "finalization_policy": {"mode": "credibility_report"}})
    s._store = _FakeStore([])
    assert s._maybe_credibility_novelty_stop(5) is None
