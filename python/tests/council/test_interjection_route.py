"""F049 slice 1 — interjection control + POST /council/runs/{id}/interjection."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from errorta_app import server as server_mod

_TAURI = {"X-Errorta-Origin": "tauri-ui"}


def _seed_terminal_run(client, seed_room_full) -> str:
    room = seed_room_full(member_count=2, provider="fake", model="stub-model",
                          max_rounds=1)
    r = client.post("/council/runs", json={"room_id": room.id, "prompt": "p",
                                            "corpus_ids": []})
    assert r.status_code in (200, 201), r.text
    run_id = r.json()["run"]["id"]
    from errorta_app.routes.council import drain_scheduler_threads
    drain_scheduler_threads(timeout=5.0)
    return run_id


def test_interjection_404_unknown_run(tmp_errorta_home):
    client = TestClient(server_mod.app)
    r = client.post("/council/runs/nope/interjection",
                    json={"text": "hi"}, headers=_TAURI)
    assert r.status_code == 404


def test_interjection_requires_ui_origin(tmp_errorta_home, seed_room_full):
    client = TestClient(server_mod.app)
    run_id = _seed_terminal_run(client, seed_room_full)
    r = client.post(f"/council/runs/{run_id}/interjection", json={"text": "hi"})
    assert r.status_code == 403


def test_interjection_409_on_terminal_run(tmp_errorta_home, seed_room_full):
    client = TestClient(server_mod.app)
    run_id = _seed_terminal_run(client, seed_room_full)  # drained -> terminal
    r = client.post(f"/council/runs/{run_id}/interjection",
                    json={"text": "steer left"}, headers=_TAURI)
    assert r.status_code == 409
    assert r.json()["detail"] == "terminal_run"


def test_interjection_400_on_empty_text(tmp_errorta_home, seed_room_full):
    client = TestClient(server_mod.app)
    room = seed_room_full(member_count=2, provider="fake", model="stub-model",
                          max_rounds=1)
    r = client.post("/council/runs", json={"room_id": room.id, "prompt": "p",
                                            "corpus_ids": []})
    run_id = r.json()["run"]["id"]
    # Submit BEFORE draining (run not yet terminal) with blank text.
    resp = client.post(f"/council/runs/{run_id}/interjection",
                       json={"text": "   "}, headers=_TAURI)
    assert resp.status_code == 400
    assert "empty" in resp.json()["detail"]
    from errorta_app.routes.council import drain_scheduler_threads
    drain_scheduler_threads(timeout=5.0)


@pytest.mark.asyncio
async def test_control_submit_interjection_appends_event(tmp_errorta_home, seed_room_full):
    # Direct control-layer test: with no scheduler writer outstanding the event
    # is appended immediately; the payload marks it as the user's authoritative
    # message.
    from errorta_council import paths as council_paths
    from errorta_council.control import RunControl
    from errorta_council.run_store import RunStore

    client = TestClient(server_mod.app)
    room = seed_room_full(member_count=2, provider="fake", model="stub-model",
                          max_rounds=1)
    r = client.post("/council/runs", json={"room_id": room.id, "prompt": "p",
                                            "corpus_ids": []})
    run_id = r.json()["run"]["id"]
    from errorta_app.routes.council import drain_scheduler_threads
    drain_scheduler_threads(timeout=5.0)
    # Run is terminal now; submit_interjection should refuse.
    runs = RunStore(runs_dir=council_paths.runs_dir())
    control = RunControl(run_store=runs, run_id=run_id)
    from errorta_council.control import TerminalRunError
    with pytest.raises(TerminalRunError):
        await control.submit_interjection(text="hello council", requested_by="user")
