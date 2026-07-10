"""TS-03 — Council Deliberation: acceptance journey (hermetic).

Seed a room and run it with deterministic fake members through the public
routes: the run reaches a terminal state with member turns (TC-03.2) and the
audit summary aggregates the turn events (TC-03.16).

The byte-isolation marquee (TC-03.5 — a redacted member's request bytes never
contain corpus sentinel bytes) is locked end-to-end by
``tests/council/test_engine_router_wired.py``; this journey covers the run/audit
path a user walks in the UI.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from errorta_app.server import app

from ._council import room_payload

pytestmark = [pytest.mark.acceptance, pytest.mark.security, pytest.mark.blocking]


@pytest.fixture
def client(tmp_errorta_home) -> TestClient:
    return TestClient(app)


def test_ts03_seed_run_audit(client) -> None:
    # Seed the room.
    created = client.post("/council/rooms", json=room_payload("rm-ts03"))
    assert created.status_code == 200, created.text

    # TC-03.2: run it with deterministic fake members.
    run = client.post("/council/runs", json={
        "room_id": "rm-ts03", "prompt": "summarize the corpus",
        "corpus_ids": [], "dry_fake_members": True,
    })
    assert run.status_code == 200, run.text
    run_id = run.json()["run"]["id"]

    # The run record is retrievable.
    detail = client.get(f"/council/runs/{run_id}")
    assert detail.status_code == 200

    # TC-03.16: the audit summary aggregates the turn events.
    summary = client.get(f"/council/runs/{run_id}/audit-summary")
    assert summary.status_code == 200
    body = summary.json()
    assert body["run_id"] == run_id
    assert body["totals"]["completed"] >= 1
