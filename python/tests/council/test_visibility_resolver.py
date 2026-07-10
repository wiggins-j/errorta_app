"""F031-06 access-vocabulary tests for TranscriptVisibilityResolver.

Locks invariants 5 (sealed/pure-per-turn) and 11 (additive schema).
"""
from __future__ import annotations

import pytest

from errorta_council.context.visibility import (
    TranscriptVisibilityResolver,
    VisibilityPlan,
)


def _event(seq, event_type, member_id=None, sensitivity="known_local", text="hi"):
    return {
        "sequence": seq,
        "id": f"evt-{seq:04d}",
        "type": event_type,
        "member_id": member_id,
        "payload": {"text": text, "sensitivity": sensitivity},
    }


def _run(events, member_id="m_local_a"):
    return {
        "run_id": "run-test",
        "members": [
            {"member_id": "m_user", "role": "user", "provider_class": "user"},
            {"member_id": "m_local_a", "role": "council", "provider_class": "local"},
            {"member_id": "m_local_b", "role": "council", "provider_class": "local"},
        ],
        "events": events,
        "scheduled_member_id": member_id,
    }


def _topology(ceiling="all_messages"):
    return {"topology_id": "round_robin", "transcript_access_ceiling": ceiling}


def test_none_returns_empty_plan():
    resolver = TranscriptVisibilityResolver()
    events = [
        _event(1, "run_started"),
        _event(2, "member_message", member_id="m_local_a", text="alpha"),
    ]
    plan = resolver.resolve(
        member={"member_id": "m_local_b", "requested_transcript_access": "none"},
        run=_run(events, "m_local_b"),
        transcript_cursor=10,
        topology_state=_topology(),
    )
    assert isinstance(plan, VisibilityPlan)
    assert plan.effective_transcript_access == "none"
    assert plan.selected_event_ids == []
    assert plan.blocked_reason is None


def test_user_only_returns_user_events_only():
    resolver = TranscriptVisibilityResolver()
    events = [
        _event(1, "run_started"),
        _event(2, "member_message", member_id="m_user", text="user-prompt"),
        _event(3, "member_message", member_id="m_local_a", text="alpha-says"),
    ]
    plan = resolver.resolve(
        member={"member_id": "m_local_b", "requested_transcript_access": "user_only"},
        run=_run(events, "m_local_b"),
        transcript_cursor=10,
        topology_state=_topology(),
    )
    assert plan.effective_transcript_access == "user_only"
    assert plan.selected_event_ids == ["evt-0002"]


def test_own_and_user_returns_user_plus_self_events():
    resolver = TranscriptVisibilityResolver()
    events = [
        _event(1, "member_message", member_id="m_user"),
        _event(2, "member_message", member_id="m_local_a"),
        _event(3, "member_message", member_id="m_local_b", text="self"),
    ]
    plan = resolver.resolve(
        member={"member_id": "m_local_b", "requested_transcript_access": "own_and_user"},
        run=_run(events, "m_local_b"),
        transcript_cursor=10,
        topology_state=_topology(),
    )
    assert plan.selected_event_ids == ["evt-0001", "evt-0003"]


def test_cursor_excludes_future_events():
    resolver = TranscriptVisibilityResolver()
    events = [
        _event(1, "member_message", member_id="m_user"),
        _event(2, "member_message", member_id="m_local_a"),
        _event(3, "member_message", member_id="m_local_a", text="future"),
    ]
    plan = resolver.resolve(
        member={"member_id": "m_local_b", "requested_transcript_access": "all_messages"},
        run=_run(events, "m_local_b"),
        transcript_cursor=2,
        topology_state=_topology(),
    )
    assert "evt-0003" not in plan.selected_event_ids
    assert plan.transcript_cursor == 2


def test_visibility_plan_is_frozen():
    resolver = TranscriptVisibilityResolver()
    plan = resolver.resolve(
        member={"member_id": "m_local_a", "requested_transcript_access": "none"},
        run=_run([]),
        transcript_cursor=0,
        topology_state=_topology(),
    )
    with pytest.raises((AttributeError, Exception)):
        plan.effective_transcript_access = "all_messages"  # type: ignore[misc]
