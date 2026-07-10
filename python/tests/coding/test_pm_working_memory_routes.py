from __future__ import annotations

from fastapi.testclient import TestClient


def _client(tmp_errorta_home) -> TestClient:
    from errorta_app.server import app

    return TestClient(app, headers={"x-errorta-origin": "tauri-ui"})


def test_pm_working_memory_status_route_returns_refs_not_raw_content(tmp_errorta_home) -> None:
    client = _client(tmp_errorta_home)
    client.post(
        "/coding/projects",
        json={
            "project_id": "pmwm-route",
            "north_star": "PRIVATE DETAIL should not leave",
            "definition_of_done": "done",
            "target": "new",
        },
    )

    resp = client.get("/coding/projects/pmwm-route/pm-working-memory")

    assert resp.status_code == 200, resp.text
    data = resp.json()["pm_working_memory"]
    assert data["project_id"] == "pmwm-route"
    assert data["status"] == "local"
    assert data["memory_ref"].startswith("mem:")
    assert "PRIVATE DETAIL" not in resp.text


def test_pm_working_memory_status_route_404s_for_missing_project(tmp_errorta_home) -> None:
    resp = _client(tmp_errorta_home).get("/coding/projects/nope/pm-working-memory")

    assert resp.status_code == 404
