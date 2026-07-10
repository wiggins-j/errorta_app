"""F031-01 §Acceptance criteria — readiness validation matrix.

Invariant 4 (fail closed): ambiguous config never returns ``ready``.
Validation never calls a provider; the only collaborator is a fake
gateway metadata reader.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from errorta_council.gateway_meta import FakeGatewayMeta
from errorta_council.schema import (
    BudgetPolicy,
    ContextPolicy,
    CouncilRoom,
    FinalizationPolicy,
    TopologyPolicy,
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


def test_minimal_local_room_is_ready(sample_room: CouncilRoom) -> None:
    result = validate_room(sample_room, _fake_meta())
    assert result.status == "ready", result.errors
    assert result.errors == []


def test_duplicate_member_ids_invalid(sample_room: CouncilRoom, member_factory) -> None:
    bad = replace(sample_room, members=[member_factory("m-1"), member_factory("m-1")])
    result = validate_room(bad, _fake_meta())
    assert result.status == "invalid"
    assert any(e["code"] == "duplicate_member_id" for e in result.errors)


def test_enabled_member_missing_route_is_needs_provider(
    sample_room: CouncilRoom, member_factory
) -> None:
    bad = replace(
        sample_room,
        members=[
            member_factory("m-1", gateway_route_id="fake.local.deterministic"),
            member_factory("m-2", gateway_route_id="does.not.exist"),
        ],
    )
    result = validate_room(bad, _fake_meta())
    assert result.status == "needs_provider"
    assert any(e["code"] == "unknown_gateway_route" for e in result.errors)


def test_multi_member_validates_pool_instead_of_static_route(
    sample_room: CouncilRoom, member_factory
) -> None:
    multi = replace(
        member_factory("m-1"), gateway_route_id=None, model_mode="multi",
        model_pool=["fake.local.deterministic", "fake.remote.priced"],
        metadata={"coding_role": "dev"},
    )
    room = replace(
        sample_room, members=[multi, member_factory("m-2")],
        budget_policy=replace(sample_room.budget_policy, max_remote_calls_per_run=1),
    )
    result = validate_room(room, _fake_meta())
    assert not any(e["code"] == "missing_gateway_route" for e in result.errors)
    assert result.status == "ready", result.errors


def test_multi_pm_and_duplicate_pool_fail_validation(
    sample_room: CouncilRoom, member_factory
) -> None:
    multi = replace(
        member_factory("m-1"), model_mode="multi",
        model_pool=["fake.local.deterministic", "fake.local.deterministic"],
        metadata={"coding_role": "pm"},
    )
    result = validate_room(replace(sample_room, members=[multi]), _fake_meta())
    codes = {error["code"] for error in result.errors}
    assert {"pm_model_mode_multi", "duplicate_model_pool_route"}.issubset(codes)


def test_unknown_topology_kind_invalid(sample_room: CouncilRoom) -> None:
    bad = replace(sample_room, topology=replace(sample_room.topology, kind="warp_drive"))
    result = validate_room(bad, _fake_meta())
    assert result.status == "invalid"
    assert any(e["code"] == "unknown_topology_kind" for e in result.errors)


def test_unknown_context_access_invalid(
    sample_room: CouncilRoom, member_factory
) -> None:
    bad = replace(
        sample_room,
        members=[member_factory("m-1"),
                 member_factory("m-2") .__class__(  # build via dataclasses.replace
                     **{**member_factory("m-2").__dict__,
                        "context_access": "telepathy"})],
    )
    result = validate_room(bad, _fake_meta())
    assert result.status == "invalid"
    assert any(e["code"] == "unknown_context_access" for e in result.errors)


def test_full_context_without_allow_full_is_blocked(
    sample_room: CouncilRoom, member_factory
) -> None:
    m1 = member_factory("m-1")
    m2 = member_factory("m-2")
    m2 = m2.__class__(**{**m2.__dict__, "context_access": "full_context"})
    bad = replace(sample_room, members=[m1, m2])
    # ContextPolicy.allow_full_context = False in the sample.
    result = validate_room(bad, _fake_meta())
    assert result.status == "blocked_by_policy"
    assert any(e["code"] == "full_context_not_allowed" for e in result.errors)


def test_dangling_finalizer_ref_invalid(sample_room: CouncilRoom) -> None:
    bad = replace(
        sample_room,
        finalization_policy=FinalizationPolicy(
            mode="single_finalizer", finalizer_member_id="ghost-id"
        ),
    )
    result = validate_room(bad, _fake_meta())
    assert result.status == "invalid"
    assert any(e["code"] == "dangling_finalizer_member" for e in result.errors)


def test_impossible_budget_invalid(sample_room: CouncilRoom) -> None:
    bad = replace(
        sample_room,
        budget_policy=replace(sample_room.budget_policy,
                              max_total_model_calls=0, max_messages_per_member=1),
    )
    result = validate_room(bad, _fake_meta())
    # Two enabled members but max_total_model_calls=0 — topology cannot run.
    assert result.status == "invalid"
    assert any(e["code"] == "impossible_budget" for e in result.errors)


def test_remote_member_with_zero_remote_calls_blocked(
    sample_room: CouncilRoom, member_factory
) -> None:
    remote_member = member_factory(
        "m-2", gateway_route_id="fake.remote.priced", provider_kind="remote"
    )
    bad = replace(sample_room, members=[member_factory("m-1"), remote_member])
    # Default sample budget has max_remote_calls_per_run=0.
    result = validate_room(bad, _fake_meta())
    assert result.status == "blocked_by_policy"
    assert any(e["code"] == "remote_member_zero_budget" for e in result.errors)


def test_draft_room_with_no_members_is_draft_not_ready(sample_room: CouncilRoom) -> None:
    bad = replace(sample_room, members=[])
    result = validate_room(bad, _fake_meta())
    assert result.status == "draft"
