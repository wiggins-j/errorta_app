"""TS-04 — Council Rooms (configuration): acceptance journey.

The room config CRUD a user drives from the Room Editor: create -> validates
clean (TC-04.16) -> appears in the list + round-trips on GET -> standalone
validate (TC-04.14) -> clone (TC-04.18) -> delete. An invalid room is rejected
422 (TC-04.15).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from errorta_app.server import app

from ._council import room_payload

pytestmark = [pytest.mark.acceptance, pytest.mark.regression]


@pytest.fixture
def client(tmp_errorta_home) -> TestClient:
    return TestClient(app)


def test_ts04_room_crud(client) -> None:
    payload = room_payload("rm-ts04", "Editor Room")

    # TC-04.16: create -> persisted + validation surfaces.
    created = client.post("/council/rooms", json=payload)
    assert created.status_code == 200, created.text
    assert created.json()["room"]["id"] == "rm-ts04"
    assert "validation" in created.json()

    # Appears in the list and round-trips on GET.
    listed = client.get("/council/rooms").json()
    ids = [r["id"] for r in listed["rooms"]]
    assert "rm-ts04" in ids
    got = client.get("/council/rooms/rm-ts04")
    assert got.status_code == 200
    assert got.json()["room"]["name"] == "Editor Room"

    # TC-04.14: standalone validation of a well-formed room is clean.
    valid = client.post("/council/rooms/validate", json=payload)
    assert valid.status_code == 200
    assert valid.json()["status"] == "ready"
    assert valid.json()["errors"] == []

    # TC-04.18: clone produces a second, distinct room.
    cloned = client.post(
        "/council/rooms/rm-ts04/clone",
        json={"new_id": "rm-ts04-copy", "new_name": "Editor Room (copy)"},
    )
    assert cloned.status_code == 200, cloned.text
    clone_id = cloned.json()["room"]["id"]
    assert clone_id != "rm-ts04"

    # Delete the original; it's gone, the clone remains.
    assert client.delete("/council/rooms/rm-ts04").status_code == 200
    assert client.get("/council/rooms/rm-ts04").status_code == 404
    assert client.get(f"/council/rooms/{clone_id}").status_code == 200


def test_ts04_invalid_room_rejected(client) -> None:
    # TC-04.15: a structurally invalid room is rejected before persistence.
    bad = client.post("/council/rooms", json={"format_version": 1, "id": "bad"})
    assert bad.status_code == 422
