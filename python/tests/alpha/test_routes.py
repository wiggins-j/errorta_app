"""Local /alpha/* routes: status polling + activate (with origin guard)."""
from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from errorta_alpha import client as alpha_client
from errorta_alpha import device
from errorta_alpha import license as license_store
from errorta_app.routes.alpha import router

_TAURI = {"x-errorta-origin": "tauri-ui"}


@pytest.fixture
def app_client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_status_reports_unactivated_when_gate_on(alpha_home, alpha_keys, app_client):
    body = app_client.get("/alpha/status").json()
    assert body["gate_enabled"] is True
    assert body["state"] == "unactivated"
    assert body["locked"] is True


def test_activate_requires_tauri_origin(alpha_home, alpha_keys, app_client):
    r = app_client.post("/alpha/activate", json={"code": "ERRT-TEST-0001"})
    assert r.status_code == 403


def test_activate_success_returns_active_status(alpha_home, alpha_keys, app_client, monkeypatch):
    now = int(time.time())
    did = device.get_or_create_device_id()
    tok = alpha_keys.mint(device_id=did, grace_until=now + 14 * 86400)
    monkeypatch.setattr(
        alpha_client, "_post_json",
        lambda path, body: (200, {"status": "active", "token": tok, "grace_days": 14}),
    )
    r = app_client.post("/alpha/activate", json={"code": "ERRT-TEST-0001"}, headers=_TAURI)
    assert r.status_code == 200
    assert r.json()["state"] == "active"
    assert r.json()["locked"] is False


def test_activate_rejected_code_returns_400(alpha_home, alpha_keys, app_client, monkeypatch):
    monkeypatch.setattr(
        alpha_client, "_post_json",
        lambda path, body: (404, {"error": "code_not_found"}),
    )
    r = app_client.post("/alpha/activate", json={"code": "NOPE"}, headers=_TAURI)
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "code_not_found"
    assert license_store.load() is None
