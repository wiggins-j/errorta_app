"""TS-06 — Model Providers & Gateway: acceptance journey.

Provider keys: save -> masked (raw never echoed) -> clear (TC-06.1/06.3). Gateway:
status + policy round-trip; a local alias dispatches (TC-06.9, handler stubbed);
a remote alias with no policy/budget is blocked 403, never dispatched (TC-06.11).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from errorta_app.routes import gateway as gateway_routes
from errorta_app.routes import model_gateway as mg_routes

pytestmark = [pytest.mark.acceptance, pytest.mark.security]

TAURI = {"x-errorta-origin": "tauri-ui"}
RAW_KEY = "sk-ant-DO-NOT-LEAK-abcdef123456"


@pytest.fixture
def client(tmp_errorta_home, monkeypatch) -> TestClient:
    monkeypatch.delenv("AIAR_LOCAL_ONLY", raising=False)
    monkeypatch.delenv("ERRORTA_MODEL_GATEWAY_LOCAL_ONLY", raising=False)
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    app = FastAPI()
    app.include_router(gateway_routes.router)
    app.include_router(mg_routes.router)
    return TestClient(app)


def test_ts06_provider_key_masked_then_cleared(client) -> None:
    # TC-06.1: save a key -> masked state; the raw key never appears in responses.
    saved = client.put("/provider-keys/anthropic", json={"api_key": RAW_KEY}, headers=TAURI)
    assert saved.status_code == 200
    assert RAW_KEY not in saved.text
    listed = client.get("/provider-keys")
    assert listed.status_code == 200 and RAW_KEY not in listed.text
    # Owner gate on the mutation.
    assert client.put("/provider-keys/openai", json={"api_key": "x"}).status_code == 403
    # TC-06.3: clear.
    assert client.delete("/provider-keys/anthropic", headers=TAURI).status_code == 200


def test_ts06_gateway_dispatch_and_block(client, monkeypatch) -> None:
    assert client.get("/model-gateway/status").status_code == 200

    # Policy round-trips.
    pol = client.put("/model-gateway/policy", json={
        "global_mode": "you_pick",
        "role_routes": {"judge": {"provider": "local"}},
    })
    assert pol.status_code == 200

    # TC-06.11: a remote alias with no key/budget is blocked BEFORE dispatch.
    dispatch_calls = 0

    async def _must_not_dispatch(plan, body):
        nonlocal dispatch_calls
        dispatch_calls += 1
        raise AssertionError("remote policy block must happen before dispatch")

    monkeypatch.setattr(mg_routes, "_dispatch_resolved", _must_not_dispatch)
    blocked = client.post("/model-gateway/ollama/api/chat", json={
        "model": "errorta.judge.remote",
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert blocked.status_code == 403
    assert blocked.json()["detail"]["allowed"] is False
    assert dispatch_calls == 0

    # TC-06.9: a local alias dispatches (provider handler stubbed -> no live model).
    async def _fake_dispatch(plan, body):
        nonlocal dispatch_calls
        dispatch_calls += 1
        return SimpleNamespace(content="local answer", input_tokens=3, output_tokens=5)

    monkeypatch.setattr(mg_routes, "_dispatch_resolved", _fake_dispatch)
    ok = client.post("/model-gateway/ollama/api/chat", json={
        "model": "errorta.judge.local",
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert ok.status_code == 200
    assert ok.json()["message"]["content"] == "local answer"
    assert dispatch_calls == 1
