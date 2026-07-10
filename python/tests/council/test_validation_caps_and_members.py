"""F031-09 + F031-03 validation tightening regressions.

Locks:
- Topology must carry positive max_rounds AND max_messages_per_member,
  validate_room rejects when either is missing (F031-09 §Runnable config).
- POST /council/runs returns 422 on rooms missing caps (no silent
  normalization to 1).
- The local MVP runtime requires exactly two enabled members
  (F031-03 §MVP local-only).
"""
from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from errorta_app import server as server_mod
from errorta_council import paths as council_paths
from errorta_council.gateway_meta import FakeGatewayMeta
from errorta_council.room_store import RoomStore
from errorta_council.validation import validate_room


def _gw() -> FakeGatewayMeta:
    return FakeGatewayMeta(known_routes={}, catalog_version="2026-06-11")


@pytest.fixture
def client(tmp_errorta_home):
    return TestClient(server_mod.app)


def test_missing_topology_max_rounds_blocks(seed_room_full) -> None:
    room = seed_room_full(member_count=2, provider="fake",
                         model="stub-model", max_rounds=1)
    bad = replace(room, topology=replace(room.topology, max_rounds=None))
    result = validate_room(bad, _gw())
    assert result.status == "invalid"
    codes = {e["code"] for e in result.errors}
    assert "missing_topology_max_rounds" in codes


def test_missing_topology_max_messages_per_member_blocks(seed_room_full) -> None:
    room = seed_room_full(member_count=2, provider="fake",
                         model="stub-model", max_rounds=1)
    bad = replace(room, topology=replace(room.topology, max_messages_per_member=None))
    result = validate_room(bad, _gw())
    assert result.status == "invalid"
    codes = {e["code"] for e in result.errors}
    assert "missing_topology_max_messages_per_member" in codes


def test_one_enabled_member_blocks_below_minimum(seed_room_full) -> None:
    """F034 (2026-06-12) relaxed the constraint to 2-8 members; 1 still
    blocks because at least 2 is required for a meaningful Council
    deliberation."""
    room = seed_room_full(member_count=2, provider="fake",
                         model="stub-model", max_rounds=1)
    bad = replace(room, members=[room.members[0]])
    result = validate_room(bad, _gw())
    assert result.status == "invalid"
    codes = {e["code"] for e in result.errors}
    assert "member_count_out_of_range" in codes


def test_three_enabled_members_now_validates(seed_room_full) -> None:
    """F034 (2026-06-12) relaxed the F031-03 ``exactly 2`` constraint
    to 2-8 so multi-provider rooms can validate."""
    room = seed_room_full(member_count=2, provider="fake",
                         model="stub-model", max_rounds=1)
    extra = replace(room.members[0], id="m-3", name="Member m-3")
    multi = replace(
        room,
        members=[*room.members, extra],
        topology=replace(
            room.topology,
            max_total_turns=3,
            max_messages_per_member=1,
            speaker_order=["m-1", "m-2", "m-3"],
        ),
        budget_policy=replace(room.budget_policy, max_total_model_calls=3),
    )
    result = validate_room(multi, _gw())
    codes = {e["code"] for e in result.errors}
    assert "member_count_out_of_range" not in codes


def test_nine_enabled_members_blocks_above_maximum(seed_room_full) -> None:
    """Cap at 8 members to keep validation predictable and the UI
    responsive — anything beyond that is an unusual Council shape."""
    room = seed_room_full(member_count=2, provider="fake",
                         model="stub-model", max_rounds=1)
    extras = [
        replace(room.members[0], id=f"m-{i}", name=f"Member m-{i}")
        for i in range(3, 10)
    ]
    speaker_order = [m.id for m in room.members] + [m.id for m in extras]
    too_many = replace(
        room,
        members=[*room.members, *extras],
        topology=replace(
            room.topology,
            max_total_turns=9,
            max_messages_per_member=1,
            speaker_order=speaker_order,
        ),
        budget_policy=replace(room.budget_policy, max_total_model_calls=9),
    )
    result = validate_room(too_many, _gw())
    assert result.status == "invalid"
    codes = {e["code"] for e in result.errors}
    assert "member_count_out_of_range" in codes


def test_route_rejects_room_missing_caps(client: TestClient, seed_room_full) -> None:
    """End-to-end: a saved room with topology.max_messages_per_member=None
    is rejected by POST /council/runs (no silent normalization)."""
    base = seed_room_full(member_count=2, provider="fake",
                         model="stub-model", max_rounds=1)
    bad = replace(base, id="rm-missing-caps",
                  topology=replace(base.topology, max_messages_per_member=None))
    store = RoomStore(
        rooms_dir=council_paths.rooms_dir(),
        deleted_dir=council_paths.deleted_rooms_dir(),
    )
    store.create(bad)
    r = client.post(
        "/council/runs",
        json={"room_id": bad.id, "prompt": "hi", "corpus_ids": []},
    )
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["code"] == "room_not_runnable"
    codes = {e["code"] for e in detail["errors"]}
    assert "missing_topology_max_messages_per_member" in codes
