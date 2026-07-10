"""F121 Part A — the run-state contract the UI's Start/Stop feedback depends on.

The frontend derives the "Stopping…" phase from the backend's persisted
``cancel_requested`` flag (so it survives a reload/poll). This locks that the
``GET /coding/projects/{id}/run`` projection actually surfaces it inside
``state`` after a cancel — a silent drop would make a stopping run look frozen.
"""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient


def _client(tmp_errorta_home: Path) -> TestClient:
    from errorta_app.server import app

    return TestClient(app, headers={"x-errorta-origin": "tauri-ui"})


def _create(c: TestClient, pid: str) -> None:
    r = c.post(
        "/coding/projects",
        json={"project_id": pid, "north_star": "n", "definition_of_done": "d", "target": "new"},
    )
    assert r.status_code == 200, r.text


def test_run_state_includes_cancel_requested(tmp_errorta_home: Path) -> None:
    c = _client(tmp_errorta_home)
    _create(c, "pf121a")

    # Fresh project: cancel_requested defaults to False inside the run-state.
    state = c.get("/coding/projects/pf121a/run").json()["state"]
    assert state.get("cancel_requested") is False

    # After a cancel, the flag flips and is surfaced in the same projection the
    # UI polls — this is the sole backend dependency of the "Stopping…" phase.
    assert c.post("/coding/projects/pf121a/run/cancel").json()["cancelled"] is True
    state = c.get("/coding/projects/pf121a/run").json()["state"]
    assert state.get("cancel_requested") is True
