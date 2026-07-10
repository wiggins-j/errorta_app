"""F031-01 per-member runtime fields land on the snapshot.

Locks the P2 drift: the scheduler reads `route_id`, `max_output_tokens`,
and `temperature` from each member dict; without the route's snapshot
helper flattening these from `gateway_route_id` / `turn_limits` /
`generation`, the scheduler silently fell back to its own defaults.
"""
from __future__ import annotations

from errorta_app.routes.council import _room_dict_with_provider_hint
from errorta_council.schema import (
    BudgetPolicy,
    ContextPolicy,
    CouncilMember,
    CouncilRoom,
    FORMAT_VERSION,
    FinalizationPolicy,
    TopologyPolicy,
)


def _room() -> CouncilRoom:
    m = lambda mid: CouncilMember(
        id=mid, name=f"M{mid}", role="answerer", enabled=True,
        gateway_route_id=f"fake.local.{mid}-model",
        provider_kind="local",
        provider_display="Fake", model_display=f"{mid}-model",
        catalog_version="2026-06-11",
        context_access="prompt_only", transcript_access="own_messages",
        turn_limits={
            "max_messages": 1, "max_input_tokens": 1024,
            "max_output_tokens": 777,   # ← distinctive
            "max_context_tokens": 1024,
        },
        generation={"temperature": 0.42, "top_p": None, "seed": None},  # ← distinctive
        system_prompt="", metadata={},
    )
    return CouncilRoom(
        format_version=FORMAT_VERSION, id="rm", name="x", description="",
        members=[m("m-1"), m("m-2")],
        topology=TopologyPolicy(
            kind="round_robin", max_rounds=1, max_total_turns=2,
            max_messages_per_member=1, speaker_order=["m-1", "m-2"],
        ),
        context_policy=ContextPolicy(
            default_context_access="prompt_only",
            default_transcript_access="own_messages",
            allow_full_context=False,
            require_confirmation_for_remote_context=True,
            require_confirmation_for_full_context=True,
        ),
        budget_policy=BudgetPolicy(
            max_rounds=1, max_messages_per_member=1, max_total_model_calls=2,
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


def test_route_id_flattened_from_gateway_route_id() -> None:
    snap = _room_dict_with_provider_hint(_room())
    members = {m["id"]: m for m in snap["members"]}
    assert members["m-1"]["route_id"] == "fake.local.m-1-model"
    assert members["m-2"]["route_id"] == "fake.local.m-2-model"


def test_max_output_tokens_flattened_from_turn_limits() -> None:
    snap = _room_dict_with_provider_hint(_room())
    for m in snap["members"]:
        assert m["max_output_tokens"] == 777, (
            "scheduler must see the room-configured per-member token cap, "
            f"not the scheduler's hardcoded fallback (got {m.get('max_output_tokens')})"
        )


def test_temperature_flattened_from_generation() -> None:
    snap = _room_dict_with_provider_hint(_room())
    for m in snap["members"]:
        assert m["temperature"] == 0.42, (
            "scheduler must see the room-configured per-member temperature, "
            f"not the 0.2 fallback (got {m.get('temperature')})"
        )


def test_provider_and_model_still_lifted() -> None:
    """Regression for the previous P1 fix — keep provider/model flattening."""
    snap = _room_dict_with_provider_hint(_room())
    for m in snap["members"]:
        assert m["provider"] == "fake"
        assert m["model"] == f"{m['id']}-model"
