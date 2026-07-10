from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from errorta_app.server import app


@pytest.fixture
def client(tmp_errorta_home):
    return TestClient(app)


def test_pause_resume_cancel_are_idempotent(client: TestClient, seed_room_full) -> None:
    room = seed_room_full(member_count=2, provider="fake", model="stub-model", max_rounds=1)
    r = client.post("/council/runs", json={"room_id": room.id, "prompt": "hi", "corpus_ids": []})
    assert r.status_code in (200, 201), r.text
    run_id = r.json()["run"]["id"]

    r1 = client.post(f"/council/runs/{run_id}/pause")
    assert r1.status_code == 200
    r2 = client.post(f"/council/runs/{run_id}/pause")
    assert r2.status_code == 200  # idempotent

    r3 = client.post(f"/council/runs/{run_id}/resume")
    assert r3.status_code == 200

    r4 = client.post(f"/council/runs/{run_id}/cancel", json={"reason": "user_action"})
    assert r4.status_code == 200
    r5 = client.post(f"/council/runs/{run_id}/cancel", json={"reason": "user_action"})
    assert r5.status_code == 200


def test_cancel_terminal_run_returns_409(client: TestClient, seed_room_full) -> None:
    room = seed_room_full(member_count=2, provider="fake", model="stub-model", max_rounds=1)
    r = client.post("/council/runs", json={"room_id": room.id, "prompt": "hi", "corpus_ids": []})
    run_id = r.json()["run"]["id"]
    client.post(f"/council/runs/{run_id}/cancel", json={"reason": "user_action"})
    for _ in range(50):
        meta = client.get(f"/council/runs/{run_id}").json()["run"]
        if meta["status"] in ("cancelled", "completed", "failed"):
            break
    r2 = client.post(f"/council/runs/{run_id}/cancel", json={"reason": "user_action"})
    assert r2.status_code == 409


def test_decision_route_records_choice(client: TestClient, seed_room_full) -> None:
    """F031-09 P2 lock: the happy path requires the run to be in
    ``awaiting_user_decision``. Drive the scheduler to terminal, park
    the run in awaiting_user_decision, then POST the decision. The
    production ask-pause path is covered by test_ask_decision_e2e.py.
    """
    from errorta_app.routes.council import drain_scheduler_threads
    from errorta_council import paths as council_paths
    from errorta_council.run_store import RunStore

    room = seed_room_full(member_count=2, provider="fake", model="stub-model", max_rounds=1)
    r = client.post("/council/runs", json={"room_id": room.id, "prompt": "hi", "corpus_ids": []})
    run_id = r.json()["run"]["id"]
    drain_scheduler_threads(timeout=5.0)
    store = RunStore(runs_dir=council_paths.runs_dir())
    store.merge_meta_fields(
        run_id, status="awaiting_user_decision", terminal_reason=None,
    )
    r2 = client.post(
        f"/council/runs/{run_id}/decision",
        json={"choice": "skip_member", "scope": "current_round"},
        headers={"X-Errorta-Origin": "tauri-ui"},
    )
    assert r2.status_code == 200, r2.text
    # The decision is always durably projected into RunMeta.last_decision
    # even when the scheduler's writer reservation forces a meta-only path
    # (in which case the route's "event" field is null).
    assert r2.json()["run"]["last_decision"]["choice"] == "skip_member"
    assert r2.json()["run"]["last_decision"]["scope"] == "current_round"


def test_decision_route_rejects_non_ui_origin(client: TestClient, seed_room_full) -> None:
    room = seed_room_full(member_count=2, provider="fake", model="stub-model", max_rounds=1)
    r = client.post("/council/runs", json={"room_id": room.id, "prompt": "hi", "corpus_ids": []})
    run_id = r.json()["run"]["id"]
    r2 = client.post(
        f"/council/runs/{run_id}/decision",
        json={"choice": "skip_member", "scope": "current_round"},
        # No X-Errorta-Origin header — should be rejected.
    )
    assert r2.status_code == 403
    assert r2.json()["detail"] == "origin_not_authorized"


def test_resource_check_returns_per_member_status(client: TestClient, seed_room_full) -> None:
    room = seed_room_full(member_count=2, provider="fake", model="stub-model", max_rounds=1)
    r = client.post(f"/council/rooms/{room.id}/resource-check")
    assert r.status_code == 200
    body = r.json()
    assert "per_member" in body
    # Both members are fake → fit.
    assert all(p["classification"] == "fits" for p in body["per_member"])


def test_dry_run_returns_validation_plus_resources(client: TestClient, seed_room_full) -> None:
    room = seed_room_full(member_count=2, provider="fake", model="stub-model", max_rounds=1)
    r = client.post(f"/council/rooms/{room.id}/dry-run")
    assert r.status_code == 200
    body = r.json()
    assert "room_validation" in body
    assert "local_resources" in body
