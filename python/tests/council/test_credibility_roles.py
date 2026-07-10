"""F081 slice 3 — assigned adversarial roles + steelman-mounted predicate."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from errorta_council.credibility.models import ClaimAdmission
from errorta_council.scheduler import TurnScheduler


class _Meta:
    id = "r1"

    def __init__(self, snapshot):
        self.room_snapshot = snapshot


def _member(mid, role=None):
    m = {"id": mid, "enabled": True}
    if role:
        m["metadata"] = {"debate_role": role}
    return m


def _sched(members, cred):
    s = TurnScheduler.__new__(TurnScheduler)
    s._meta = _Meta({"members": members, "credibility_policy": cred,
                     "finalization_policy": {"mode": "credibility_report"}})
    return s


def test_explicit_opponent_wins():
    s = _sched([_member("A"), _member("B", role="opponent"), _member("C")],
              {"enabled": True, "rigor": "standard"})
    assert s._credibility_opponent_id() == "B"


def test_auto_assign_opponent_when_adversarial():
    s = _sched([_member("A"), _member("B"), _member("C")],
              {"enabled": True, "rigor": "adversarial"})
    assert s._credibility_opponent_id() == "C"  # last enabled non-judge


def test_no_opponent_in_lenient_room():
    s = _sched([_member("A"), _member("B")],
              {"enabled": True, "rigor": "lenient"})
    assert s._credibility_opponent_id() is None


def test_steelman_mounted_true_when_opponent_claim_admitted():
    s = _sched([_member("A"), _member("B", role="opponent")],
              {"enabled": True, "rigor": "standard"})
    admissions = [
        ClaimAdmission(claim_id="A:c1", admission="admitted"),
        ClaimAdmission(claim_id="B:c1", admission="admitted_with_caveat"),
    ]
    assert s._credibility_steelman_mounted([], admissions) is True


def test_steelman_not_mounted_when_opponent_excluded():
    s = _sched([_member("A"), _member("B", role="opponent")],
              {"enabled": True, "rigor": "standard"})
    admissions = [
        ClaimAdmission(claim_id="A:c1", admission="admitted"),
        ClaimAdmission(claim_id="B:c1", admission="excluded"),  # opponent failed the gate
    ]
    assert s._credibility_steelman_mounted([], admissions) is False


def test_steelman_predicate_skips_when_no_opponent():
    s = _sched([_member("A"), _member("B")], {"enabled": True, "rigor": "lenient"})
    assert s._credibility_steelman_mounted([], []) is True  # nothing to flag
