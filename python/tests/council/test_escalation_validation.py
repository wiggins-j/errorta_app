"""F037 Slice 1 — escalation roster + policy schema round-trip and validation.

Invariant 4 (fail closed): a misconfigured escalation policy never returns
``ready``. Validation never calls a provider; the only collaborator is a
fake gateway metadata reader. Mirrors test_room_validation.py.
"""
from __future__ import annotations

from dataclasses import replace

from errorta_council.gateway_meta import FakeGatewayMeta
from errorta_council.schema import (
    CouncilRoom,
    EscalationPolicy,
    EscalationRosterEntry,
)
from errorta_council.validation import validate_room


def _fake_meta() -> FakeGatewayMeta:
    return FakeGatewayMeta(
        known_routes={
            "fake.local.deterministic": {"kind": "local", "priced": False},
            "fake.remote.priced":       {"kind": "remote", "priced": True},
        },
        catalog_version="2026-06-11",
    )


def _enabled_policy(**over) -> EscalationPolicy:
    base = {"enabled": True}
    base.update(over)
    return EscalationPolicy(**base)


def _headroom(room: CouncilRoom) -> CouncilRoom:
    """Lift the model-call budget so callout headroom exists, mirroring the
    editor's auto-bump. Isolates the error code each test targets."""
    return replace(
        room,
        budget_policy=replace(room.budget_policy, max_total_model_calls=None),
    )


def _local_target(tid: str = "deep-reviewer", **over) -> EscalationRosterEntry:
    base = {
        "id": tid,
        "name": "Deep Reviewer",
        "gateway_route_id": "fake.local.deterministic",
        "provider_kind": "local",
        "context_access": "redacted_summary",
        "transcript_access": "summary_only",
    }
    base.update(over)
    return EscalationRosterEntry(**base)


# --- schema round-trip ----------------------------------------------------

def test_room_without_escalation_serializes_unchanged(sample_room: CouncilRoom) -> None:
    d = sample_room.to_dict()
    assert "escalation_policy" not in d
    assert "escalation_roster" not in d
    # round-trips back to a default disabled policy + empty roster
    back = CouncilRoom.from_dict(d)
    assert back.escalation_policy == EscalationPolicy()
    assert back.escalation_roster == []


def test_escalation_config_round_trips(sample_room: CouncilRoom) -> None:
    room = replace(
        sample_room,
        escalation_policy=_enabled_policy(max_callouts_per_run=2),
        escalation_roster=[_local_target()],
    )
    d = room.to_dict()
    assert d["escalation_policy"]["enabled"] is True
    assert d["escalation_roster"][0]["id"] == "deep-reviewer"
    back = CouncilRoom.from_dict(d)
    assert back.escalation_policy.max_callouts_per_run == 2
    assert back.escalation_roster[0].gateway_route_id == "fake.local.deterministic"


def test_unknown_roster_fields_preserved(sample_room: CouncilRoom) -> None:
    raw = sample_room.to_dict()
    raw["escalation_policy"] = {"enabled": True, "future_knob": 7}
    raw["escalation_roster"] = [
        {"id": "x", "gateway_route_id": "fake.local.deterministic", "future_field": "keep"}
    ]
    back = CouncilRoom.from_dict(raw)
    assert back.escalation_policy._extras == {"future_knob": 7}
    assert back.escalation_roster[0]._extras == {"future_field": "keep"}
    # and they survive a write-back
    assert back.to_dict()["escalation_policy"]["future_knob"] == 7
    assert back.to_dict()["escalation_roster"][0]["future_field"] == "keep"


# --- validation -----------------------------------------------------------

def test_disabled_policy_with_roster_is_warning_only(sample_room: CouncilRoom) -> None:
    room = replace(sample_room, escalation_roster=[_local_target()])
    result = validate_room(room, _fake_meta())
    assert result.status == "ready", result.errors
    assert any(w["code"] == "escalation_disabled_with_roster" for w in result.warnings)
    assert result.capabilities["has_callout_roster"] is True
    assert result.derived["callout_target_count"] == 1


def test_enabled_local_roster_is_ready(sample_room: CouncilRoom) -> None:
    room = _headroom(replace(
        sample_room,
        escalation_policy=_enabled_policy(),
        escalation_roster=[_local_target()],
    ))
    result = validate_room(room, _fake_meta())
    assert result.status == "ready", result.errors
    assert result.capabilities["requires_callout_approval"] is True


def test_duplicate_target_id_invalid(sample_room: CouncilRoom) -> None:
    room = replace(
        sample_room,
        escalation_policy=_enabled_policy(),
        escalation_roster=[_local_target("dup"), _local_target("dup")],
    )
    result = validate_room(room, _fake_meta())
    assert result.status == "invalid"
    assert any(e["code"] == "duplicate_escalation_target_id" for e in result.errors)


def test_unknown_route_is_needs_provider(sample_room: CouncilRoom) -> None:
    room = _headroom(replace(
        sample_room,
        escalation_policy=_enabled_policy(),
        escalation_roster=[_local_target(gateway_route_id="does.not.exist")],
    ))
    result = validate_room(room, _fake_meta())
    assert result.status == "needs_provider"
    assert any(e["code"] == "unknown_escalation_route" for e in result.errors)


def test_remote_target_zero_budget_is_blocked_by_policy(sample_room: CouncilRoom) -> None:
    room = _headroom(replace(
        sample_room,
        escalation_policy=_enabled_policy(max_remote_callouts_per_run=0),
        escalation_roster=[
            _local_target(gateway_route_id="fake.remote.priced", provider_kind="remote")
        ],
    ))
    result = validate_room(room, _fake_meta())
    assert result.status == "blocked_by_policy"
    assert any(e["code"] == "remote_callout_zero_budget" for e in result.errors)
    assert result.capabilities["has_remote_callout_targets"] is True


def test_full_context_target_blocked_when_disallowed(sample_room: CouncilRoom) -> None:
    room = _headroom(replace(
        sample_room,
        context_policy=replace(sample_room.context_policy, allow_full_context=False),
        escalation_policy=_enabled_policy(),
        escalation_roster=[_local_target(context_access="full_context")],
    ))
    result = validate_room(room, _fake_meta())
    assert result.status == "blocked_by_policy"
    assert any(e["code"] == "callout_full_context_not_allowed" for e in result.errors)


def test_unknown_approval_mode_invalid(sample_room: CouncilRoom) -> None:
    room = replace(
        sample_room,
        escalation_policy=_enabled_policy(approval_mode="telepathy"),
        escalation_roster=[_local_target()],
    )
    result = validate_room(room, _fake_meta())
    assert result.status == "invalid"
    assert any(e["code"] == "callout_approval_mode_unknown" for e in result.errors)


def test_unknown_requester_member_invalid(sample_room: CouncilRoom) -> None:
    room = replace(
        sample_room,
        escalation_policy=_enabled_policy(
            requester_mode="member_allowlist", requester_member_ids=["ghost"]
        ),
        escalation_roster=[_local_target()],
    )
    result = validate_room(room, _fake_meta())
    assert result.status == "invalid"
    assert any(e["code"] == "callout_requester_member_unknown" for e in result.errors)


def test_auto_trigger_without_roster_invalid(sample_room: CouncilRoom) -> None:
    room = replace(
        sample_room,
        escalation_policy=_enabled_policy(auto_after_no_consensus_rounds=2),
        escalation_roster=[],
    )
    result = validate_room(room, _fake_meta())
    assert result.status == "invalid"
    assert any(e["code"] == "callout_auto_trigger_without_roster" for e in result.errors)


def test_impossible_callout_budget_invalid(sample_room: CouncilRoom) -> None:
    # Pin max_total_model_calls to exactly the member-turn floor so there is
    # no headroom for the configured callout.
    enabled = len([m for m in sample_room.members if m.enabled])
    rounds = sample_room.topology.max_rounds or 1
    room = replace(
        sample_room,
        budget_policy=replace(
            sample_room.budget_policy, max_total_model_calls=enabled * rounds
        ),
        escalation_policy=_enabled_policy(max_callouts_per_run=1),
        escalation_roster=[_local_target()],
    )
    result = validate_room(room, _fake_meta())
    assert result.status == "invalid"
    assert any(e["code"] == "callout_total_budget_impossible" for e in result.errors)
