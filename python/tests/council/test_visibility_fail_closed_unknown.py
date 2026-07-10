"""Unknown sensitivity on a remote-bound turn → blocked.

Invariant 4 (Fail closed, marquee). Deliberate-bug fixture: a transcript event
carries sensitivity='unknown'. A subsequent remote-bound member must block;
a local-bound member allows it only with explicit room policy.
"""
from __future__ import annotations

from errorta_council.context.visibility import TranscriptVisibilityResolver


def _events_with_unknown():
    return [
        {"sequence": 1, "id": "evt-0001", "type": "member_message", "member_id": "m_user",
         "payload": {"sensitivity": "known_local", "text": "user prompt"}},
        {"sequence": 2, "id": "evt-0002", "type": "member_message", "member_id": "m_local_a",
         "payload": {"sensitivity": "unknown", "text": "WHO KNOWS"}},
    ]


def _members():
    return [
        {"member_id": "m_user", "role": "user", "provider_class": "user"},
        {"member_id": "m_local_a", "role": "council", "provider_class": "local"},
        {"member_id": "m_remote_b", "role": "council", "provider_class": "remote"},
    ]


def test_remote_bound_blocks_on_unknown_sensitivity():
    resolver = TranscriptVisibilityResolver()
    plan = resolver.resolve(
        member={
            "member_id": "m_remote_b",
            "requested_transcript_access": "all_messages",
            "destination_scope": "remote",
        },
        run={"run_id": "r1", "members": _members(), "events": _events_with_unknown(),
             "scheduled_member_id": "m_remote_b",
             "room_policy": {"allow_unknown_sensitivity_local": False}},
        transcript_cursor=10,
        topology_state={"transcript_access_ceiling": "all_messages"},
    )
    assert plan.blocked_reason == "unknown_sensitivity_remote"
    assert plan.effective_transcript_access == "none"
    assert "evt-0002" not in plan.selected_event_ids


def test_local_bound_blocks_unknown_when_room_policy_forbids():
    resolver = TranscriptVisibilityResolver()
    plan = resolver.resolve(
        member={
            "member_id": "m_local_a",
            "requested_transcript_access": "all_messages",
            "destination_scope": "local",
        },
        run={"run_id": "r1", "members": _members(), "events": _events_with_unknown(),
             "scheduled_member_id": "m_local_a",
             "room_policy": {"allow_unknown_sensitivity_local": False}},
        transcript_cursor=10,
        topology_state={"transcript_access_ceiling": "all_messages"},
    )
    assert "evt-0002" not in plan.selected_event_ids
    assert any(o.get("event_id") == "evt-0002" and o.get("reason") == "unknown_sensitivity"
               for o in plan.omitted)


def test_local_bound_allows_unknown_when_room_policy_permits():
    resolver = TranscriptVisibilityResolver()
    plan = resolver.resolve(
        member={
            "member_id": "m_local_a",
            "requested_transcript_access": "all_messages",
            "destination_scope": "local",
        },
        run={"run_id": "r1", "members": _members(), "events": _events_with_unknown(),
             "scheduled_member_id": "m_local_a",
             "room_policy": {"allow_unknown_sensitivity_local": True}},
        transcript_cursor=10,
        topology_state={"transcript_access_ceiling": "all_messages"},
    )
    assert plan.blocked_reason is None
    assert "evt-0002" in plan.selected_event_ids
