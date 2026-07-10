"""Regression: Start Run resolves members from a room_id (the UI's normal path).

The coding console always starts a run by passing the selected Council room's
``room_id``. ``_resolve_members`` crashed that path twice — ``RoomStore()`` with
no args (TypeError) and ``member.to_dict()`` (AttributeError) — so every Start
Run via the UI returned a 500. These lock both.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from errorta_app.routes.coding import _resolve_members
from errorta_council import paths as council_paths
from errorta_council.room_store import RoomNotFound, RoomStore
from errorta_council.schema import (
    FORMAT_VERSION,
    BudgetPolicy,
    ContextPolicy,
    CouncilMember,
    CouncilRoom,
    FinalizationPolicy,
    TopologyPolicy,
)


def _store() -> RoomStore:
    return RoomStore(rooms_dir=council_paths.rooms_dir(),
                     deleted_dir=council_paths.deleted_rooms_dir())


def _member(mid: str, role: str) -> CouncilMember:
    return CouncilMember(id=mid, name=role.upper(), provider_kind="local",
                         metadata={"coding_role": role})


def _room(rid: str) -> CouncilRoom:
    return CouncilRoom(
        format_version=FORMAT_VERSION, id=rid, name="Team", description="",
        members=[_member("m-1", "pm"), _member("m-2", "dev")],
        topology=TopologyPolicy(kind="round_robin", max_rounds=1,
                                max_total_turns=2, max_messages_per_member=1,
                                speaker_order=["m-1", "m-2"]),
        context_policy=ContextPolicy(
            default_context_access="prompt_only",
            default_transcript_access="own_messages", allow_full_context=False,
            require_confirmation_for_remote_context=True,
            require_confirmation_for_full_context=True),
        budget_policy=BudgetPolicy(
            max_rounds=1, max_messages_per_member=1, max_total_model_calls=2,
            max_remote_calls_per_run=0, max_remote_calls_per_day=None,
            max_input_tokens_per_turn=1024, max_output_tokens_per_turn=256,
            max_context_tokens_per_member=1024, max_estimated_usd_per_run=0.0,
            max_estimated_usd_per_month=None),
        finalization_policy=FinalizationPolicy(mode="transcript_only"),
        created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
        revision=1)


def test_resolve_members_from_room_id(tmp_errorta_home: Path) -> None:
    _store().create(_room("team-x"))
    members = _resolve_members({"room_id": "team-x"})
    assert len(members) == 2
    # every member is a plain dict the runner can consume (regression: was a 500)
    assert all(isinstance(m, dict) and m.get("id") for m in members)
    assert {(m.get("metadata") or {}).get("coding_role") for m in members} == {"pm", "dev"}


def test_resolve_members_explicit_list_still_works(tmp_errorta_home: Path) -> None:
    members = _resolve_members({"members": [
        {"id": "m1", "enabled": True, "metadata": {"coding_role": "pm"}}]})
    assert len(members) == 1 and members[0]["id"] == "m1"


def test_resolve_members_unknown_room_raises_clean(tmp_errorta_home: Path) -> None:
    # an unknown room raises RoomNotFound (FastAPI maps it), NOT the old bare
    # TypeError from RoomStore() with no args.
    with pytest.raises(RoomNotFound):
        _resolve_members({"room_id": "does-not-exist"})
