"""Task 10 — the FastAPI routes facade.

Mounted as a standalone app (just ``errorta_slack.routes.router``), not the
full sidecar app: these routes read/write ``errorta_slack`` state directly
(config/store/secrets/linking), not ``request.app.state``, so a lightweight
app is enough and keeps this suite fast and independent of the sidecar's
full lifespan (council/coding recovery, sidecar advert, alpha sync, ...).
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from errorta_slack import config, linking, secrets


@pytest.fixture(autouse=True)
def _isolated_errorta_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture()
def client() -> TestClient:
    from errorta_slack.routes import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


# --- GET /slack/health -------------------------------------------------------


def test_health_ok(client: TestClient) -> None:
    resp = client.get("/slack/health")

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


# --- GET /slack/status -------------------------------------------------------


def test_status_on_fresh_config_is_disabled(client: TestClient) -> None:
    resp = client.get("/slack/status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is False
    assert body["bindings"] == []
    assert body["tokens_masked"] == {"app_token": None, "bot_token": None}


def test_status_reflects_bindings(client: TestClient) -> None:
    from errorta_slack import store

    store.bind_channel("C123", "proj-a")

    resp = client.get("/slack/status")

    assert resp.status_code == 200
    assert resp.json()["bindings"] == [{"channel_id": "C123", "project_id": "proj-a"}]


def test_status_masks_tokens_never_returns_raw(client: TestClient) -> None:
    secrets.save_tokens("xapp-1-AAAA-realsecretvalue", "xoxb-1-BBBB-realsecretvalue")

    resp = client.get("/slack/status")

    assert resp.status_code == 200
    masked = resp.json()["tokens_masked"]
    assert masked["app_token"] == "…alue"
    assert masked["bot_token"] == "…alue"
    body_text = resp.text
    assert "realsecretvalue" not in body_text
    assert "xapp-1-AAAA" not in body_text
    assert "xoxb-1-BBBB" not in body_text


# --- POST /slack/enable / /slack/disable ------------------------------------


def test_enable_without_tokens_returns_400(client: TestClient) -> None:
    resp = client.post("/slack/enable")

    assert resp.status_code == 400
    assert config.load()["enabled"] is False


def test_enable_with_tokens_persists_enabled(client: TestClient) -> None:
    secrets.save_tokens("xapp-1-AAAA-token", "xoxb-1-BBBB-token")

    resp = client.post("/slack/enable")

    assert resp.status_code == 200
    assert resp.json()["enabled"] is True
    assert config.load()["enabled"] is True


def test_disable_persists_disabled(client: TestClient) -> None:
    secrets.save_tokens("xapp-1-AAAA-token", "xoxb-1-BBBB-token")
    client.post("/slack/enable")
    assert config.load()["enabled"] is True

    resp = client.post("/slack/disable")

    assert resp.status_code == 200
    assert resp.json()["enabled"] is False
    assert config.load()["enabled"] is False


# --- POST /slack/link/approve ------------------------------------------------


def test_link_approve_unknown_id_returns_404(client: TestClient) -> None:
    resp = client.post("/slack/link/approve", json={"link_id": "does-not-exist"})

    assert resp.status_code == 404


def test_link_approve_binds_channel(client: TestClient) -> None:
    link_id = linking.request_link("C999", "proj-b", "U1")

    resp = client.post("/slack/link/approve", json={"link_id": link_id})

    assert resp.status_code == 200
    assert resp.json()["state"] == "approved"

    from errorta_slack import store

    assert store.binding_for("C999") == {"channel_id": "C999", "project_id": "proj-b"}


def test_link_approve_already_resolved_returns_409(client: TestClient) -> None:
    link_id = linking.request_link("C999", "proj-b", "U1")
    client.post("/slack/link/approve", json={"link_id": link_id})

    resp = client.post("/slack/link/approve", json={"link_id": link_id})

    assert resp.status_code == 409


# --- POST /slack/studio/bind / GET /slack/studio ----------------------------


def test_studio_bind_then_get_roundtrips(client: TestClient) -> None:
    resp = client.post("/slack/studio/bind", json={"channel_id": "C1"})

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "studio_channel": "C1"}

    resp = client.get("/slack/studio")

    assert resp.status_code == 200
    assert resp.json() == {"studio_channel": "C1"}


def test_studio_get_before_bind_is_none(client: TestClient) -> None:
    resp = client.get("/slack/studio")

    assert resp.status_code == 200
    assert resp.json() == {"studio_channel": None}


def test_studio_bind_overwrites_prior_value(client: TestClient) -> None:
    client.post("/slack/studio/bind", json={"channel_id": "C1"})

    resp = client.post("/slack/studio/bind", json={"channel_id": "C2"})

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "studio_channel": "C2"}
    assert client.get("/slack/studio").json() == {"studio_channel": "C2"}
