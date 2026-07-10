"""Schema round-trip + invariant 11 (unsupported format_version rejected)."""
from __future__ import annotations

import pytest

from errorta_council import schema
from errorta_council.schema import (
    FORMAT_VERSION,
    BudgetPolicy,
    ContextPolicy,
    CouncilEvent,
    CouncilEventError,
    CouncilMember,
    CouncilRoom,
    EventStatus,
    EventType,
    FinalizationPolicy,
    MemberSnapshot,
    RunMeta,
    TopologyPolicy,
    UnsupportedFormatVersion,
)


def test_format_version_is_one() -> None:
    assert FORMAT_VERSION == 1


def test_multi_member_round_trips_and_legacy_defaults_stay_omitted(sample_room) -> None:
    legacy = sample_room.to_dict()
    assert "model_mode" not in legacy["members"][0]
    assert "model_pool" not in legacy["members"][0]
    raw = sample_room.to_dict()
    raw["members"][0]["model_mode"] = "multi"
    raw["members"][0]["model_pool"] = ["fake.local.deterministic"]
    raw["members"][0].pop("gateway_route_id", None)
    parsed = CouncilRoom.from_dict(raw)
    assert parsed.members[0].model_mode == "multi"
    assert parsed.to_dict()["members"][0]["model_pool"] == ["fake.local.deterministic"]


def test_event_round_trip_minimal() -> None:
    ev = CouncilEvent(
        format_version=FORMAT_VERSION,
        id="ev-1",
        run_id="run-1",
        sequence=1,
        type=EventType.RUN_STARTED,
        status=EventStatus.RUNNING,
        created_at="2026-06-11T00:00:00Z",
        payload={"room_id": "room-1"},
    )
    raw = ev.to_dict()
    assert raw["format_version"] == 1
    assert raw["type"] == "run_started"
    assert raw["status"] == "running"
    back = CouncilEvent.from_dict(raw)
    assert back == ev


def test_event_round_trip_with_member_snapshot_and_error() -> None:
    snap = MemberSnapshot(
        member_id="m-1",
        name="Fake Drafter",
        role="answerer",
        provider_display="Fake",
        model_display="deterministic",
        locality="fake",
        context_access="prompt_only",
        transcript_access="own_messages",
        catalog_version=None,
    )
    err = CouncilEventError(
        code="provider_timeout",
        message="Provider timed out.",
        retryable=True,
        details={"phase": "call"},
    )
    ev = CouncilEvent(
        format_version=FORMAT_VERSION,
        id="ev-2",
        run_id="run-1",
        sequence=2,
        type=EventType.MEMBER_FAILED,
        status=EventStatus.FAILED,
        created_at="2026-06-11T00:00:01Z",
        payload={"error_code": "provider_timeout"},
        member_id="m-1",
        member_snapshot=snap,
        error=err,
    )
    back = CouncilEvent.from_dict(ev.to_dict())
    assert back.member_snapshot == snap
    assert back.error == err


def test_unknown_field_is_tolerated() -> None:
    """Invariant 11: readers tolerate unknown fields (additive evolution)."""
    raw = {
        "format_version": 1,
        "id": "ev-3",
        "run_id": "run-1",
        "sequence": 1,
        "type": "run_started",
        "status": "running",
        "created_at": "2026-06-11T00:00:00Z",
        "payload": {},
        "future_field_we_dont_know_yet": {"x": 1},
    }
    ev = CouncilEvent.from_dict(raw)
    assert ev.id == "ev-3"


def test_unsupported_format_version_rejected() -> None:
    """Invariant 11: unsupported future versions rejected with a clear error."""
    raw = {
        "format_version": 99,
        "id": "ev-x",
        "run_id": "run-1",
        "sequence": 1,
        "type": "run_started",
        "status": "running",
        "created_at": "2026-06-11T00:00:00Z",
        "payload": {},
    }
    with pytest.raises(UnsupportedFormatVersion) as exc:
        CouncilEvent.from_dict(raw)
    assert "99" in str(exc.value)
    assert "1" in str(exc.value)


def test_unknown_event_type_surfaces_as_generic() -> None:
    """Forward compat for reserved future event types (member_delta, etc.)."""
    raw = {
        "format_version": 1,
        "id": "ev-future",
        "run_id": "run-1",
        "sequence": 1,
        "type": "member_delta",
        "status": "running",
        "created_at": "2026-06-11T00:00:00Z",
        "payload": {},
    }
    # Reserved future type; the parser must not crash.
    ev = CouncilEvent.from_dict(raw)
    assert ev.type == EventType.MEMBER_DELTA


def _sample_room() -> CouncilRoom:
    member = CouncilMember(
        id="m-1",
        name="Fake Drafter",
        role="answerer",
        enabled=True,
        gateway_route_id="fake.local.deterministic",
        provider_kind="local",
        provider_display="Fake",
        model_display="deterministic",
        catalog_version="2026-06-11",
        context_access="prompt_only",
        transcript_access="own_messages",
        turn_limits={"max_messages": 1, "max_input_tokens": 1024,
                     "max_output_tokens": 256, "max_context_tokens": 1024},
        generation={"temperature": 0.0, "top_p": None, "seed": None},
        system_prompt="Draft a short answer.",
        metadata={},
    )
    return CouncilRoom(
        format_version=FORMAT_VERSION,
        id="room-1",
        name="One Local Drafter",
        description="Phase 0 fake-driver smoke room.",
        members=[member],
        topology=TopologyPolicy(
            kind="round_robin",
            max_rounds=1,
            max_total_turns=1,
            max_messages_per_member=1,
            speaker_order=["m-1"],
        ),
        context_policy=ContextPolicy(
            default_context_access="prompt_only",
            default_transcript_access="own_messages",
            allow_full_context=False,
            require_confirmation_for_remote_context=True,
            require_confirmation_for_full_context=True,
        ),
        budget_policy=BudgetPolicy(
            max_rounds=1, max_messages_per_member=1, max_total_model_calls=1,
            max_remote_calls_per_run=0, max_remote_calls_per_day=None,
            max_input_tokens_per_turn=1024, max_output_tokens_per_turn=256,
            max_context_tokens_per_member=1024,
            max_estimated_usd_per_run=0.0, max_estimated_usd_per_month=None,
        ),
        finalization_policy=FinalizationPolicy(mode="transcript_only"),
        created_at="2026-06-11T00:00:00Z",
        updated_at="2026-06-11T00:00:00Z",
        revision=1,
    )


def test_room_round_trip() -> None:
    room = _sample_room()
    back = CouncilRoom.from_dict(room.to_dict())
    assert back == room


def test_room_rejects_future_format_version() -> None:
    raw = _sample_room().to_dict()
    raw["format_version"] = 99
    with pytest.raises(UnsupportedFormatVersion):
        CouncilRoom.from_dict(raw)


def test_run_meta_round_trip() -> None:
    meta = RunMeta(
        format_version=FORMAT_VERSION,
        id="run-1", room_id="room-1",
        room_snapshot={"name": "One Local Drafter", "topology_kind": "round_robin",
                       "member_count": 1, "room_format_version": 1},
        prompt="hello",
        corpus_ids=[],
        status="created",
        created_at="2026-06-11T00:00:00Z",
        started_at=None,
        updated_at="2026-06-11T00:00:00Z",
        finished_at=None,
        last_sequence=0,
        event_count=0,
        terminal_event_id=None,
        resume_policy="mark_interrupted",
        costs={"remote_calls": 0, "local_calls": 0,
               "input_tokens": 0, "output_tokens": 0, "estimated_usd": 0.0},
        capabilities={"streaming": False, "fake_members": True},
    )
    assert RunMeta.from_dict(meta.to_dict()) == meta


def test_unknown_field_preserved_through_round_trip() -> None:
    """Invariant 11: unknown top-level fields survive read → write → read."""
    raw = {
        "format_version": 1,
        "id": "ev-extras",
        "run_id": "run-1",
        "sequence": 1,
        "type": "run_started",
        "status": "running",
        "created_at": "2026-06-11T00:00:00Z",
        "payload": {},
        "future_top_level": {"x": 1, "y": [1, 2]},
    }
    ev = CouncilEvent.from_dict(raw)
    out = ev.to_dict()
    assert out["future_top_level"] == {"x": 1, "y": [1, 2]}
    back = CouncilEvent.from_dict(out)
    assert back == ev


def test_unknown_nested_member_snapshot_field_tolerated_and_preserved() -> None:
    raw = {
        "format_version": 1,
        "id": "ev-nest",
        "run_id": "run-1",
        "sequence": 1,
        "type": "member_message",
        "status": "completed",
        "created_at": "2026-06-11T00:00:00Z",
        "payload": {},
        "member_snapshot": {
            "member_id": "m-1",
            "name": "Fake",
            "role": "answerer",
            "provider_display": "Fake",
            "model_display": "deterministic",
            "locality": "fake",
            "context_access": "prompt_only",
            "transcript_access": "own_messages",
            "catalog_version": None,
            "future_snapshot_field": "ok",
        },
    }
    ev = CouncilEvent.from_dict(raw)  # must not raise TypeError
    assert ev.member_snapshot is not None
    out = ev.to_dict()
    assert out["member_snapshot"]["future_snapshot_field"] == "ok"
    back = CouncilEvent.from_dict(out)
    assert back == ev


def test_unknown_nested_error_field_tolerated_and_preserved() -> None:
    raw = {
        "format_version": 1,
        "id": "ev-err",
        "run_id": "run-1",
        "sequence": 1,
        "type": "member_failed",
        "status": "failed",
        "created_at": "2026-06-11T00:00:00Z",
        "payload": {"error_code": "x"},
        "error": {
            "code": "provider_timeout",
            "message": "x",
            "retryable": True,
            "details": {},
            "future_error_field": [1, 2, 3],
        },
    }
    ev = CouncilEvent.from_dict(raw)
    assert ev.error is not None
    out = ev.to_dict()
    assert out["error"]["future_error_field"] == [1, 2, 3]
    back = CouncilEvent.from_dict(out)
    assert back == ev


def test_unknown_nested_room_policy_fields_tolerated_and_preserved() -> None:
    """One test covering CouncilMember + each of the 4 policy types in a room."""
    room = _sample_room()
    raw = room.to_dict()
    raw["members"][0]["future_member_field"] = "mok"
    raw["topology"]["future_topology_field"] = 1
    raw["context_policy"]["future_context_field"] = True
    raw["budget_policy"]["future_budget_field"] = 0.5
    raw["finalization_policy"]["future_final_field"] = ["a"]
    raw["future_room_field"] = {"nested": True}

    back = CouncilRoom.from_dict(raw)  # must not raise TypeError
    out = back.to_dict()
    assert out["members"][0]["future_member_field"] == "mok"
    assert out["topology"]["future_topology_field"] == 1
    assert out["context_policy"]["future_context_field"] is True
    assert out["budget_policy"]["future_budget_field"] == 0.5
    assert out["finalization_policy"]["future_final_field"] == ["a"]
    assert out["future_room_field"] == {"nested": True}
    again = CouncilRoom.from_dict(out)
    assert again == back


def test_unknown_run_meta_field_tolerated_and_preserved() -> None:
    meta = RunMeta(
        format_version=FORMAT_VERSION,
        id="run-1", room_id="room-1", room_snapshot={},
        prompt="hello", corpus_ids=[],
        status="created",
        created_at="2026-06-11T00:00:00Z", started_at=None,
        updated_at="2026-06-11T00:00:00Z", finished_at=None,
        last_sequence=0, event_count=0, terminal_event_id=None,
        resume_policy="mark_interrupted",
        costs={}, capabilities={},
    )
    raw = meta.to_dict()
    raw["future_run_meta_field"] = {"k": "v"}
    back = RunMeta.from_dict(raw)
    out = back.to_dict()
    assert out["future_run_meta_field"] == {"k": "v"}
    again = RunMeta.from_dict(out)
    assert again == back
