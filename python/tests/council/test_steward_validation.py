"""F038 Council Steward policy validation."""
from __future__ import annotations

from dataclasses import replace

from errorta_council.gateway_meta import FakeGatewayMeta
from errorta_council.schema import (
    CouncilRoom,
    StewardAssignment,
    StewardPolicy,
)
from errorta_council.steward.policy import resolve_steward_policy
from errorta_council.validation import validate_room


def _fake_meta() -> FakeGatewayMeta:
    return FakeGatewayMeta(
        known_routes={
            "fake.local.deterministic": {"kind": "local", "priced": False},
            "fake.remote.priced": {"kind": "remote", "priced": True},
        },
        catalog_version="2026-06-11",
    )


def _with_steward(room: CouncilRoom, policy: StewardPolicy) -> CouncilRoom:
    return replace(room, steward_policy=policy)


def test_resolver_defaults_to_off(sample_room: CouncilRoom) -> None:
    policy = resolve_steward_policy(sample_room)
    assert policy.enabled is False
    assert policy.assignment.mode == "external"
    assert policy.packet_mode == "hybrid"
    assert policy.recent_full_messages == 2
    assert policy.max_packet_tokens == 1200
    assert "steward_policy" not in sample_room.to_dict()


def test_member_steward_validates_existing_enabled_member(
    sample_room: CouncilRoom,
) -> None:
    room = _with_steward(
        sample_room,
        StewardPolicy(
            enabled=True,
            assignment=StewardAssignment(mode="member", member_id="m-1"),
        ),
    )
    result = validate_room(room, _fake_meta())
    assert result.status == "ready", result.errors
    assert result.capabilities["has_steward"] is True
    assert result.capabilities["steward_mode"] == "member"
    assert result.capabilities["steward_requires_extra_model_calls"] is False


def test_member_steward_rejects_unknown_member(sample_room: CouncilRoom) -> None:
    room = _with_steward(
        sample_room,
        StewardPolicy(
            enabled=True,
            assignment=StewardAssignment(mode="member", member_id="ghost"),
        ),
    )
    result = validate_room(room, _fake_meta())
    assert result.status == "invalid"
    assert any(e["code"] == "steward_member_unknown" for e in result.errors)


def test_member_steward_rejects_disabled_member(
    sample_room: CouncilRoom, member_factory
) -> None:
    room = replace(
        sample_room,
        members=[member_factory("m-1", enabled=False), member_factory("m-2")],
        steward_policy=StewardPolicy(
            enabled=True,
            assignment=StewardAssignment(mode="member", member_id="m-1"),
        ),
    )
    result = validate_room(room, _fake_meta())
    assert result.status == "invalid"
    assert any(e["code"] == "steward_member_disabled" for e in result.errors)


def test_external_local_steward_validates_route(sample_room: CouncilRoom) -> None:
    room = _with_steward(
        sample_room,
        StewardPolicy(
            enabled=True,
            assignment=StewardAssignment(
                mode="external",
                gateway_route_id="local.summary-model",
                provider_kind="local",
            ),
        ),
    )
    result = validate_room(room, _fake_meta())
    assert result.status == "ready", result.errors
    assert result.capabilities["steward_mode"] == "external"
    assert result.capabilities["steward_remote"] is False
    assert result.capabilities["steward_requires_extra_model_calls"] is True


def test_external_steward_unknown_route_is_needs_provider(
    sample_room: CouncilRoom,
) -> None:
    room = _with_steward(
        sample_room,
        StewardPolicy(
            enabled=True,
            assignment=StewardAssignment(
                mode="external",
                gateway_route_id="anthropic.missing",
                provider_kind="remote",
            ),
        ),
    )
    result = validate_room(room, _fake_meta())
    assert result.status == "needs_provider"
    assert any(e["code"] == "steward_external_unknown_route" for e in result.errors)


def test_remote_steward_requires_explicit_remote_opt_in(
    sample_room: CouncilRoom,
) -> None:
    budget = replace(
        sample_room.budget_policy,
        max_remote_calls_per_run=1,
        max_remote_steward_calls_per_run=1,
    )
    room = replace(
        sample_room,
        budget_policy=budget,
        steward_policy=StewardPolicy(
            enabled=True,
            remote_steward_allowed=False,
            assignment=StewardAssignment(
                mode="external",
                gateway_route_id="fake.remote.priced",
                provider_kind="remote",
            ),
        ),
    )
    result = validate_room(room, _fake_meta())
    assert result.status == "blocked_by_policy"
    assert any(e["code"] == "steward_remote_not_allowed" for e in result.errors)


def test_remote_steward_requires_remote_budget(sample_room: CouncilRoom) -> None:
    budget = replace(
        sample_room.budget_policy,
        max_remote_calls_per_run=1,
        max_remote_steward_calls_per_run=0,
    )
    room = replace(
        sample_room,
        budget_policy=budget,
        steward_policy=StewardPolicy(
            enabled=True,
            remote_steward_allowed=True,
            assignment=StewardAssignment(
                mode="external",
                gateway_route_id="fake.remote.priced",
                provider_kind="remote",
            ),
        ),
    )
    result = validate_room(room, _fake_meta())
    assert result.status == "blocked_by_policy"
    assert any(e["code"] == "steward_remote_zero_budget" for e in result.errors)


def test_steward_rejects_unknown_mode_cadence_and_fallback(
    sample_room: CouncilRoom,
) -> None:
    room = _with_steward(
        sample_room,
        StewardPolicy(
            enabled=True,
            assignment=StewardAssignment(mode="oracle"),
            cadence="every_femtosecond",
            fallback_on_failure="pretend_it_worked",
        ),
    )
    result = validate_room(room, _fake_meta())
    assert result.status == "invalid"
    codes = {e["code"] for e in result.errors}
    assert "unknown_steward_assignment_mode" in codes
    assert "steward_cadence_unknown" in codes
    assert "steward_fallback_mode_unknown" in codes


def test_steward_rejects_negative_recent_messages_and_tiny_packet(
    sample_room: CouncilRoom,
) -> None:
    room = _with_steward(
        sample_room,
        StewardPolicy(
            enabled=True,
            recent_full_messages=-1,
            max_packet_tokens=64,
        ),
    )
    result = validate_room(room, _fake_meta())
    assert result.status == "invalid"
    codes = {e["code"] for e in result.errors}
    assert "steward_recent_full_messages_negative" in codes
    assert "steward_max_packet_tokens_too_low" in codes
