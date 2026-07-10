"""Quick-setup "Marathon" preset produces a runnable, valid room.

The editor's Marathon preset sets a very high (but finite) round ceiling on a
consensus topology, enables a member-mode steward as the council leader, and
turns on transcript compaction. This locks that the resulting room validates
clean — the round ceiling does not trip impossible_budget, and the member-mode
steward needs no extra remote budget.
"""
from __future__ import annotations

from dataclasses import replace

from errorta_council.gateway_meta import FakeGatewayMeta
from errorta_council.schema import (
    BudgetPolicy,
    CouncilRoom,
    StewardAssignment,
    StewardPolicy,
    TopologyPolicy,
)
from errorta_council.validation import validate_room

MARATHON_ROUNDS = 100


def _fake_meta() -> FakeGatewayMeta:
    return FakeGatewayMeta(
        known_routes={"fake.local.deterministic": {"kind": "local", "priced": False}},
        catalog_version="2026-06-11",
    )


def _marathon(room: CouncilRoom) -> CouncilRoom:
    enabled = [m for m in room.members if m.enabled]
    # Mirror the editor: consensus topology + high finite ceiling, budget floor
    # auto-bumped to enabled*rounds, member-mode steward = council leader.
    topology = replace(
        room.topology,
        kind="consensus_deliberation",
        max_rounds=MARATHON_ROUNDS,
        max_messages_per_member=MARATHON_ROUNDS,
        max_total_turns=MARATHON_ROUNDS * 8,
    )
    budget = replace(
        room.budget_policy,
        max_rounds=MARATHON_ROUNDS,
        max_messages_per_member=MARATHON_ROUNDS,
        max_total_model_calls=len(enabled) * MARATHON_ROUNDS,
    )
    steward = StewardPolicy(
        enabled=True,
        assignment=StewardAssignment(mode="member", member_id=enabled[0].id),
    )
    return replace(
        room, topology=topology, budget_policy=budget, steward_policy=steward
    )


def test_marathon_room_validates(sample_room: CouncilRoom) -> None:
    result = validate_room(_marathon(sample_room), _fake_meta())
    assert result.errors == [], result.errors
    assert result.capabilities["has_steward"] is True
    assert result.capabilities["steward_mode"] == "member"
    # Member-mode steward is an existing member — no extra (remote) calls.
    assert result.capabilities["steward_requires_extra_model_calls"] is False


def test_marathon_high_round_ceiling_does_not_trip_impossible_budget(
    sample_room: CouncilRoom,
) -> None:
    room = _marathon(sample_room)
    # Budget covers enabled*rounds, so no impossible_budget / no-headroom error.
    assert all(
        e["code"] not in ("impossible_budget", "max_total_model_calls_no_headroom")
        for e in validate_room(room, _fake_meta()).errors
    )
