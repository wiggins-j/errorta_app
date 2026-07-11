"""F147 S9a §13.3 — on-demand POST /coding/projects/{id}/delivery-review.

Re-runs the F146 delivery review on an already-``done`` project outside the run
loop. Covers the origin guard, the graceful ``not done`` / ``no team`` paths, and
that a done project WITH a saved team actually invokes the F146 machinery.
"""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient


def _client(headers=None) -> TestClient:
    from errorta_app.server import app

    return TestClient(app, headers=headers or {"x-errorta-origin": "tauri-ui"})


def _create(c: TestClient, project_id: str) -> None:
    r = c.post("/coding/projects", json={
        "project_id": project_id, "north_star": "n",
        "definition_of_done": "d", "target": "new"})
    assert r.status_code == 200, r.text


def test_origin_guard_rejects_untrusted(tmp_errorta_home: Path) -> None:
    c = _client(headers={"x-errorta-origin": "evil"})
    r = c.post("/coding/projects/whatever/delivery-review")
    assert r.status_code == 403


def test_origin_guard_accepts_cli(tmp_errorta_home: Path) -> None:
    # cli origin passes the guard (then 404s on the missing project, not 403).
    c = _client(headers={"x-errorta-origin": "cli"})
    r = c.post("/coding/projects/ghost/delivery-review")
    assert r.status_code == 404


def test_missing_project_404(tmp_errorta_home: Path) -> None:
    c = _client()
    assert c.post("/coding/projects/ghost/delivery-review").status_code == 404


def test_not_done_is_graceful_409(tmp_errorta_home: Path) -> None:
    c = _client()
    _create(c, "pnd")  # status defaults to active/new, not done
    r = c.post("/coding/projects/pnd/delivery-review")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "not_done"


def test_done_but_run_live_is_409(tmp_errorta_home: Path, monkeypatch) -> None:
    """A delivered project with a run live (this or another sidecar) is refused —
    the reviewer/tests/launch probe would contend with the worker's worktree."""
    from errorta_app.routes import coding as coding_routes
    from errorta_council.coding.ledger import LedgerStore

    c = _client()
    _create(c, "plive")
    LedgerStore("plive").set_project_status("done")
    # Simulate a live run (here or in another process) via the same predicate the
    # route consults; avoids standing up a real worker thread.
    monkeypatch.setattr(coding_routes, "_run_live", lambda project_id, state: True)
    r = c.post("/coding/projects/plive/delivery-review")
    assert r.status_code == 409
    assert r.json()["detail"] == "a run is already in progress"


def test_done_no_team_is_graceful(tmp_errorta_home: Path) -> None:
    from errorta_council.coding.ledger import LedgerStore

    c = _client()
    _create(c, "pnt")
    LedgerStore("pnt").set_project_status("done")
    r = c.post("/coding/projects/pnt/delivery-review")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ran"] is False
    assert body["reason"] == "no_team"


def test_done_with_team_runs_delivery_review(tmp_errorta_home: Path) -> None:
    from errorta_council.coding.ledger import LedgerStore

    c = _client()
    _create(c, "pdr")
    store = LedgerStore("pdr")
    # Save a team with a reviewer (offline fake route -> no network).
    members = [
        {"id": "pm", "enabled": True, "gateway_route_id": "fake.local.deterministic",
         "provider_kind": "local", "metadata": {"coding_role": "pm"}},
        {"id": "rev", "enabled": True, "gateway_route_id": "fake.local.deterministic",
         "provider_kind": "local", "metadata": {"coding_role": "reviewer"}},
    ]
    store.set_run_config(members=members)
    store.set_project_status("done")

    r = c.post("/coding/projects/pdr/delivery-review")
    assert r.status_code == 200, r.text
    body = r.json()
    # It reached and ran the F146 machinery against the delivered head.
    assert body["ran"] is True
    assert isinstance(body["passed"], bool)
    assert "reason" in body
