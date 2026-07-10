"""TS-07 — AIAR Backend & Data Residency: acceptance journey (hermetic).

Reads the AIAR runtime + residency surfaces and asserts the honesty/fail-closed
properties: AIAR status/connection are masked (TC-07.3/07.6); residency reads +
sets Local and probes (TC-07.10); Cloud mode is disabled (TC-07.11); /healthz
carries the corpus_backend coordination block (TC-07.7).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from errorta_app.server import app

pytestmark = [pytest.mark.acceptance, pytest.mark.regression]


@pytest.fixture
def client(tmp_errorta_home) -> TestClient:
    return TestClient(app)


def test_ts07_aiar_status_is_masked(client) -> None:
    # TC-07.3/07.6: status + connection never expose a raw token.
    status = client.get("/aiar/status")
    assert status.status_code == 200
    assert "token" not in status.json() or status.json().get("token") is None

    conn = client.get("/aiar/connection")
    assert conn.status_code == 200
    body = conn.json()
    # Disconnected: no canonical connection, and no raw `token` field is ever
    # serialized (only the boolean `token_configured` flag).
    assert body["configured"] is False
    assert body["canonical"] is None
    assert '"token":' not in conn.text


def test_ts07_residency_local_and_cloud_gate(client) -> None:
    # TC-07.10: residency reads, sets Local, and probes.
    assert client.get("/residency").status_code == 200
    put = client.put("/residency", json={"mode": "local"})
    assert put.status_code == 200
    assert client.get("/residency").json()["config"]["mode"] == "local"
    assert client.post("/residency/probe", json={"mode": "local"}).status_code in (200, 422)

    # TC-07.11: Cloud mode is disabled until token auth ships.
    cloud = client.put("/residency", json={"mode": "cloud", "cloud_url": "https://x"})
    assert cloud.status_code == 501


def test_ts07_healthz_carries_coordination(client) -> None:
    # TC-07.7: the corpus_backend coordination block is present + honest.
    cb = client.get("/healthz").json()["corpus_backend"]
    assert "kind" in cb and "retrieval_coordinated" in cb
    assert isinstance(cb["retrieval_coordinated"], bool)
