"""Local /alpha/telemetry routes: consent read/write (origin-guarded) + inspector."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from errorta_alpha import telemetry
from errorta_app.routes.alpha import router

_TAURI = {"x-errorta-origin": "tauri-ui"}


@pytest.fixture
def client(alpha_home):
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_get_telemetry_reports_consent(client):
    body = client.get("/alpha/telemetry").json()
    assert body["gate_enabled"] is True
    assert body["extras_enabled"] is True


def test_put_requires_tauri_origin(client):
    r = client.put("/alpha/telemetry", json={"extras_enabled": False})
    assert r.status_code == 403


def test_put_toggles_extras(client):
    r = client.put("/alpha/telemetry", json={"extras_enabled": False}, headers=_TAURI)
    assert r.status_code == 200
    assert r.json()["extras_enabled"] is False
    assert telemetry.extras_enabled() is False


def test_inspect_returns_pending_payload(client):
    telemetry.record_launch()
    telemetry.record_feature_used("judge_run")
    snap = client.get("/alpha/telemetry/inspect").json()
    assert snap["floor"].get("launches") == 1
    assert snap["queue_len"] == 1
    assert snap["queue"][0]["name"] == "judge_run"
