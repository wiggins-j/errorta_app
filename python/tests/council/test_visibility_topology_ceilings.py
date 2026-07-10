"""Topology cannot widen transcript_access. Invariant 7 (Caps are absolute)."""
from __future__ import annotations

from errorta_council.context.visibility import TranscriptVisibilityResolver


def _events():
    return [
        {"sequence": 1, "id": "evt-0001", "type": "member_message", "member_id": "m_user",
         "payload": {"sensitivity": "known_local"}},
        {"sequence": 2, "id": "evt-0002", "type": "member_message", "member_id": "m_local_a",
         "payload": {"sensitivity": "known_local"}},
    ]


def _members():
    return [
        {"member_id": "m_user", "role": "user", "provider_class": "user"},
        {"member_id": "m_local_a", "role": "council", "provider_class": "local"},
        {"member_id": "m_local_b", "role": "council", "provider_class": "local"},
    ]


def test_topology_widening_is_clamped_to_member_request():
    resolver = TranscriptVisibilityResolver()
    plan = resolver.resolve(
        member={"member_id": "m_local_b", "requested_transcript_access": "own_and_user"},
        run={"run_id": "r1", "members": _members(), "events": _events(),
             "scheduled_member_id": "m_local_b"},
        transcript_cursor=10,
        topology_state={"topology_id": "evil", "transcript_access_ceiling": "all_messages",
                        "attempts_widen": True},
    )
    assert plan.effective_transcript_access == "own_and_user", (
        "Topology must never widen member request; "
        f"got effective={plan.effective_transcript_access}"
    )
    assert "evt-0002" not in plan.selected_event_ids


def test_topology_narrowing_clamps_member_request_down():
    resolver = TranscriptVisibilityResolver()
    plan = resolver.resolve(
        member={"member_id": "m_local_b", "requested_transcript_access": "all_messages"},
        run={"run_id": "r1", "members": _members(), "events": _events(),
             "scheduled_member_id": "m_local_b"},
        transcript_cursor=10,
        topology_state={"topology_id": "round_robin", "transcript_access_ceiling": "own_and_user"},
    )
    assert plan.effective_transcript_access == "own_and_user"
    assert plan.requested_transcript_access == "all_messages"
    assert any("clamp" in w for w in plan.warnings)
