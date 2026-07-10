"""F078 Slice 1 — Credibility-mode room validation (fail closed)."""
from __future__ import annotations

from dataclasses import replace

from errorta_council.gateway_meta import FakeGatewayMeta
from errorta_council.schema import (
    CouncilRoom,
    CredibilityPolicy,
    FinalizationPolicy,
    ToolEnabledPolicy,
    ToolPolicy,
    ToolWebFetchPolicy,
    TopologyPolicy,
)
from errorta_council.validation import validate_room


def _meta() -> FakeGatewayMeta:
    return FakeGatewayMeta(
        known_routes={"fake.local.deterministic": {"kind": "local", "priced": False}},
        catalog_version="2026-06-11",
    )


def _codes(result) -> set[str]:
    return {e["code"] for e in result.errors}


def _tools_on() -> ToolPolicy:
    return ToolPolicy(
        web_search=ToolEnabledPolicy(enabled=True),
        web_fetch=ToolWebFetchPolicy(enabled=True),
    )


def _credibility_room(sample_room: CouncilRoom, **pol_kw) -> CouncilRoom:
    """A coherent Credibility room: topology + finalization + tools + policy."""
    topo = replace(sample_room.topology, kind="credibility")
    return replace(
        sample_room,
        topology=topo,
        finalization_policy=FinalizationPolicy(mode="credibility_report"),
        tool_policy=_tools_on(),
        credibility_policy=CredibilityPolicy(enabled=True, **pol_kw),
    )


def test_coherent_credibility_room_is_ready(sample_room: CouncilRoom) -> None:
    result = validate_room(_credibility_room(sample_room), _meta())
    assert result.status == "ready", result.errors
    assert result.capabilities["has_credibility"] is True


def test_default_room_unaffected(sample_room: CouncilRoom) -> None:
    result = validate_room(sample_room, _meta())
    assert result.status == "ready"
    assert result.capabilities.get("has_credibility") is False


def test_missing_web_search_blocks(sample_room: CouncilRoom) -> None:
    room = _credibility_room(sample_room)
    room = replace(room, tool_policy=ToolPolicy(web_fetch=ToolWebFetchPolicy(enabled=True)))
    result = validate_room(room, _meta())
    assert "credibility_requires_web_search" in _codes(result)
    assert result.status == "blocked_by_policy"


def test_missing_web_fetch_blocks(sample_room: CouncilRoom) -> None:
    room = _credibility_room(sample_room)
    room = replace(room, tool_policy=ToolPolicy(web_search=ToolEnabledPolicy(enabled=True)))
    result = validate_room(room, _meta())
    assert "credibility_requires_web_fetch" in _codes(result)


def test_policy_enabled_without_topology_errors(sample_room: CouncilRoom) -> None:
    room = replace(
        sample_room,
        tool_policy=_tools_on(),
        credibility_policy=CredibilityPolicy(enabled=True),
    )
    result = validate_room(room, _meta())
    assert "credibility_requires_topology" in _codes(result)


def test_topology_without_credibility_finalization_errors(sample_room: CouncilRoom) -> None:
    topo = replace(sample_room.topology, kind="credibility")
    room = replace(sample_room, topology=topo, tool_policy=_tools_on(),
                   credibility_policy=CredibilityPolicy(enabled=True))
    result = validate_room(room, _meta())
    assert "credibility_finalization_mode_mismatch" in _codes(result)


def test_unknown_leader_member_errors(sample_room: CouncilRoom) -> None:
    room = _credibility_room(sample_room, leader_member_id="ghost")
    result = validate_room(room, _meta())
    assert "credibility_unknown_leader_member" in _codes(result)


def test_unknown_strictness_errors(sample_room: CouncilRoom) -> None:
    room = _credibility_room(sample_room, strictness="paranoid")
    result = validate_room(room, _meta())
    assert "credibility_strictness_unknown" in _codes(result)


def test_negative_repair_passes_errors(sample_room: CouncilRoom) -> None:
    room = _credibility_room(sample_room, max_repair_passes=-1)
    result = validate_room(room, _meta())
    assert "credibility_repair_passes_negative" in _codes(result)


def test_downgrade_without_consent_blocks(sample_room: CouncilRoom) -> None:
    room = _credibility_room(
        sample_room, fallback_on_tool_failure="downgrade_to_normal",
        allow_downgrade_consent=False,
    )
    result = validate_room(room, _meta())
    assert "credibility_downgrade_requires_consent" in _codes(result)


def test_downgrade_with_consent_is_ready(sample_room: CouncilRoom) -> None:
    room = _credibility_room(
        sample_room, fallback_on_tool_failure="downgrade_to_normal",
        allow_downgrade_consent=True,
    )
    result = validate_room(room, _meta())
    assert result.status == "ready", result.errors


def test_impossible_source_policy_errors(sample_room: CouncilRoom) -> None:
    room = _credibility_room(sample_room, min_fetched_sources_per_member=0)
    result = validate_room(room, _meta())
    assert "credibility_source_policy_impossible" in _codes(result)


def test_search_budget_too_low_errors(sample_room: CouncilRoom) -> None:
    room = _credibility_room(sample_room, max_searches_per_member=0)
    result = validate_room(room, _meta())
    assert "credibility_search_budget_too_low" in _codes(result)


def test_single_member_too_low(sample_room: CouncilRoom) -> None:
    one = replace(sample_room, members=[sample_room.members[0]])
    room = _credibility_room(one)
    # speaker_order still references m-2; trim to avoid unrelated noise.
    room = replace(room, topology=replace(room.topology, speaker_order=["m-1"]))
    result = validate_room(room, _meta())
    assert "credibility_member_count_too_low" in _codes(result)


def test_disabled_policy_with_config_warns_not_errors(sample_room: CouncilRoom) -> None:
    room = replace(sample_room, credibility_policy=CredibilityPolicy(strictness="strict"))
    result = validate_room(room, _meta())
    assert not any(c.startswith("credibility_") for c in _codes(result))
    assert any(w["code"] == "credibility_disabled_with_config" for w in result.warnings)
