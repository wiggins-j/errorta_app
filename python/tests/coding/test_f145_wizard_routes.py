"""F145 Slice 2 — the AI Wizard routes (start / message / finalize / create)."""
from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from errorta_council.coding import pm_reference, wizard
from errorta_council.coding.ledger import LedgerStore

TAURI = {"x-errorta-origin": "tauri-ui"}

_ROUTES = [
    {"route_id": "local.qwen", "family": "qwen", "provider_class": "local"},
    {"route_id": "anthropic.sonnet", "family": "claude", "provider_class": "anthropic"},
]

_FULL_CHARTER = {
    "reply": "Ready to build.",
    "charter": {
        "north_star": "A tip-split calculator",
        "audience": "friends", "modality": "static",
        "definition_of_done": "opens in a browser, updates live",
        "entrypoint": "index.html", "team_recipe": "fast_cheap",
        "autonomous": False,
    },
    "ready": True, "missing": [],
}


def _client() -> TestClient:
    from errorta_app.server import app
    return TestClient(app, headers=TAURI)


def _patch(monkeypatch, *, routes=_ROUTES, reply=_FULL_CHARTER):
    monkeypatch.setattr(pm_reference, "list_available_routes", lambda: list(routes))
    monkeypatch.setattr(
        wizard, "_default_caller",
        lambda: (lambda member, prompt: json.dumps(reply)))


def test_start_requires_available_route(tmp_errorta_home: Path, monkeypatch):
    _patch(monkeypatch)
    c = _client()
    # unavailable route → 422 grounded refusal
    r = c.post("/coding/wizard/start", json={"model_route": "openai.ghost"})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "model_unavailable"
    # available route → session created + opening line
    r = c.post("/coding/wizard/start", json={"model_route": "anthropic.sonnet"})
    assert r.status_code == 200
    assert r.json()["session_id"].startswith("wiz-")
    assert r.json()["reply"]


def test_message_finalize_flow(tmp_errorta_home: Path, monkeypatch):
    _patch(monkeypatch)
    c = _client()
    sid = c.post("/coding/wizard/start",
                 json={"model_route": "local.qwen"}).json()["session_id"]
    r = c.post(f"/coding/wizard/{sid}/message", json={"message": "a tip splitter"})
    assert r.status_code == 200 and r.json()["ready"] is True
    fin = c.post(f"/coding/wizard/{sid}/finalize")
    assert fin.status_code == 200
    assert fin.json()["charter"]["modality"] == "static"


def test_finalize_incomplete_is_409(tmp_errorta_home: Path, monkeypatch):
    partial = {"reply": "what modality?", "charter": {"north_star": "x"},
               "ready": True, "missing": []}
    _patch(monkeypatch, reply=partial)
    c = _client()
    sid = c.post("/coding/wizard/start",
                 json={"model_route": "local.qwen"}).json()["session_id"]
    c.post(f"/coding/wizard/{sid}/message", json={"message": "hi"})
    r = c.post(f"/coding/wizard/{sid}/finalize")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "charter_incomplete"


def test_create_builds_runnable_project(tmp_errorta_home: Path, monkeypatch):
    _patch(monkeypatch)
    c = _client()
    sid = c.post("/coding/wizard/start",
                 json={"model_route": "local.qwen"}).json()["session_id"]
    c.post(f"/coding/wizard/{sid}/message", json={"message": "a tip splitter"})
    r = c.post(f"/coding/wizard/{sid}/create", json={"project_id": "tip-split"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] is True
    assert body["team_size"] == 4  # pm + 2 dev + reviewer
    assert body["run_setup_confirmed"] is True
    # the project really exists, runnable, with the charter + a seeded brainstorm
    store = LedgerStore("tip-split")
    proj = store.get_project()
    assert "tip-split" in proj.id
    assert proj.north_star.startswith("A tip-split")
    cfg = store.get_run_config()
    roles = {(m.get("metadata") or {}).get("coding_role") for m in cfg["members"]}
    assert {"pm", "dev", "reviewer"} <= roles
    # session is discarded after create
    assert wizard.get_session(sid) is None


def test_create_without_models_warns_and_is_unconfirmed(tmp_errorta_home: Path, monkeypatch):
    _patch(monkeypatch, routes=[])  # no available routes
    # start must still succeed — patch a route just for the availability gate at start
    one = [{"route_id": "local.qwen", "family": "q", "provider_class": "local"}]
    monkeypatch.setattr(pm_reference, "list_available_routes", lambda: one)
    c = _client()
    sid = c.post("/coding/wizard/start",
                 json={"model_route": "local.qwen"}).json()["session_id"]
    c.post(f"/coding/wizard/{sid}/message", json={"message": "x"})
    # now make the catalog empty for the create-time team resolution
    monkeypatch.setattr(pm_reference, "list_available_routes", lambda: [])
    r = c.post(f"/coding/wizard/{sid}/create", json={"project_id": "no-models"})
    assert r.status_code == 200
    body = r.json()
    assert body["team_size"] == 0 and body["run_setup_confirmed"] is False
    assert any("no_models_available" in w for w in body["warnings"])


def test_create_rejects_bad_project_id(tmp_errorta_home: Path, monkeypatch):
    _patch(monkeypatch)
    c = _client()
    sid = c.post("/coding/wizard/start",
                 json={"model_route": "local.qwen"}).json()["session_id"]
    c.post(f"/coding/wizard/{sid}/message", json={"message": "x"})
    r = c.post(f"/coding/wizard/{sid}/create", json={"project_id": "bad id"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid_project_id"
