"""F078 Slice 1 — CredibilityPolicy schema round-trip + room integration."""
from __future__ import annotations

from dataclasses import replace

from errorta_council.schema import (
    CouncilRoom,
    CredibilityPolicy,
    resolve_credibility_leader,
)


def test_default_policy_is_off_and_omitted(sample_room: CouncilRoom) -> None:
    pol = CredibilityPolicy()
    assert pol.enabled is False
    # A default-policy room must serialize WITHOUT a credibility_policy key
    # (byte-compat with pre-F078 rooms).
    assert "credibility_policy" not in sample_room.to_dict()


def test_round_trip_preserves_fields() -> None:
    pol = CredibilityPolicy(
        enabled=True,
        strictness="strict",
        leader_member_id="m-2",
        min_fetched_sources_per_member=3,
        max_credibility_cycles=2,
        max_review_fetches_per_member=4,
        recency_days=30,
        allow_downgrade_consent=True,
        fallback_on_tool_failure="downgrade_to_normal",
    )
    again = CredibilityPolicy.from_dict(pol.to_dict())
    assert again == pol


def test_unknown_keys_round_trip_via_extras() -> None:
    raw = {"enabled": True, "future_knob": 7}
    pol = CredibilityPolicy.from_dict(raw)
    assert pol.enabled is True
    out = pol.to_dict()
    assert out["future_knob"] == 7  # forward-compat passthrough


def test_room_serializes_policy_when_non_default(sample_room: CouncilRoom) -> None:
    room = replace(sample_room, credibility_policy=CredibilityPolicy(enabled=True))
    d = room.to_dict()
    assert d["credibility_policy"]["enabled"] is True
    # Full round-trip through the room.
    back = CouncilRoom.from_dict(d)
    assert back.credibility_policy.enabled is True


def test_leader_resolution_explicit_then_speaker_order_then_last(
    sample_room: CouncilRoom,
) -> None:
    # Explicit id wins.
    room = replace(sample_room, credibility_policy=CredibilityPolicy(leader_member_id="m-1"))
    assert resolve_credibility_leader(room) == "m-1"
    # Unset → last speaker in speaker_order (["m-1", "m-2"]).
    room2 = replace(sample_room, credibility_policy=CredibilityPolicy())
    assert resolve_credibility_leader(room2) == "m-2"
    # Unset + no speaker_order → last enabled member.
    topo = replace(sample_room.topology, speaker_order=[])
    room3 = replace(room2, topology=topo)
    assert resolve_credibility_leader(room3) == "m-2"
