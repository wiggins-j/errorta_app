"""F039 — /council/runs/{id}/apply-workspace preview + human-accept routes."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from errorta_app import server as server_mod
from errorta_tools.builtins.code import CodeWriteHandler
from errorta_tools.gateway import ToolCallRequest

_TAURI = {"X-Errorta-Origin": "tauri-ui"}


def _seed_run(client, seed_room_full) -> str:
    room = seed_room_full(member_count=2, provider="fake", model="stub-model",
                          max_rounds=1)
    r = client.post("/council/runs", json={"room_id": room.id, "prompt": "p",
                                            "corpus_ids": []})
    assert r.status_code in (200, 201), r.text
    run_id = r.json()["run"]["id"]
    from errorta_app.routes.council import drain_scheduler_threads
    drain_scheduler_threads(timeout=5.0)
    return run_id


async def _make_apply_workspace(project, run_id):
    pol = {
        "code_read": {"enabled": True, "workspace_path": str(project)},
        "code_write": {"enabled": True, "mode": "auto_apply"},
        "execution": {"location": "local"},
    }
    await CodeWriteHandler().invoke(
        ToolCallRequest(
            call_id="tc-1", run_id=run_id, turn_id="t-1", member_id="m-1",
            tool_id="code_write", arguments={"path": "app.py", "content": "x = 9\n"},
            metadata={"round": 1, "tool_policy": pol},
        )
    )


def test_apply_workspace_404_when_run_missing(tmp_errorta_home):
    client = TestClient(server_mod.app)
    r = client.get("/council/runs/no-such-run/apply-workspace")
    assert r.status_code == 404
    assert r.json()["detail"] == "run_not_found"


def test_apply_workspace_404_when_no_workspace(tmp_errorta_home, seed_room_full):
    client = TestClient(server_mod.app)
    run_id = _seed_run(client, seed_room_full)
    r = client.get(f"/council/runs/{run_id}/apply-workspace")
    assert r.status_code == 404
    assert r.json()["detail"] == "apply_workspace_not_found"


@pytest.mark.asyncio
async def test_preview_then_accept_applies(tmp_path, tmp_errorta_home, seed_room_full):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "app.py").write_text("x = 1\n")
    client = TestClient(server_mod.app)
    run_id = _seed_run(client, seed_room_full)
    await _make_apply_workspace(project, run_id)

    # Preview shows the proposed change; nothing written yet.
    pv = client.get(f"/council/runs/{run_id}/apply-workspace")
    assert pv.status_code == 200, pv.text
    body = pv.json()
    assert body["has_changes"] is True
    assert any(c["path"] == "app.py" for c in body["changed_files"])
    assert (project / "app.py").read_text() == "x = 1\n"

    # Accept WITHOUT confirm -> fails closed.
    bad = client.post(f"/council/runs/{run_id}/apply-workspace/accept",
                      json={"confirm": False}, headers=_TAURI)
    assert bad.status_code == 400
    assert bad.json()["detail"] == "confirmation_required"
    assert (project / "app.py").read_text() == "x = 1\n"

    # Accept without the Tauri origin -> 403.
    noorigin = client.post(f"/council/runs/{run_id}/apply-workspace/accept",
                           json={"confirm": True})
    assert noorigin.status_code == 403

    # Confirmed + UI origin -> applied.
    ok = client.post(f"/council/runs/{run_id}/apply-workspace/accept",
                     json={"confirm": True}, headers=_TAURI)
    assert ok.status_code == 200, ok.text
    assert "app.py" in ok.json()["written"]
    assert (project / "app.py").read_text() == "x = 9\n"


@pytest.mark.asyncio
async def test_accept_409_on_conflict(tmp_path, tmp_errorta_home, seed_room_full):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "app.py").write_text("x = 1\n")
    client = TestClient(server_mod.app)
    run_id = _seed_run(client, seed_room_full)
    await _make_apply_workspace(project, run_id)
    # Concurrent user edit -> conflict.
    (project / "app.py").write_text("x = 555  # user\n")
    r = client.post(f"/council/runs/{run_id}/apply-workspace/accept",
                    json={"confirm": True}, headers=_TAURI)
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "conflicts"
    assert (project / "app.py").read_text() == "x = 555  # user\n"
