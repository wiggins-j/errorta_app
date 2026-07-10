"""Ollama setup routes follow the active data residency target."""
from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from errorta_app.routes import _residency_proxy
from errorta_app.routes import ollama as ollama_routes


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(ollama_routes.router)
    return TestClient(app)


class _Response:
    status_code = 200
    text = ""

    def json(self) -> dict[str, Any]:
        return {
            "reachable": True,
            "host": "http://127.0.0.1:11434",
            "version": "0.7.0",
            "error": None,
            "managed_by_errorta": True,
            "needs_install": False,
            "platform_supported": True,
        }


class _Client:
    calls: list[tuple[str, str, dict[str, str]]]

    def __init__(self, *_args, **_kwargs) -> None:
        self.calls = []

    def __enter__(self) -> "_Client":
        return self

    def __exit__(self, *_exc) -> None:
        return None

    def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        json: Any | None = None,
    ) -> _Response:
        self.calls.append((method, url, headers))
        return _Response()


def test_ollama_health_proxies_to_ssh_tunnel_port(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_errorta_home
) -> None:
    from errorta_residency import config as residency_config

    residency_config.update(
        mode="ssh-remote",
        ssh_host="example-host",
        remote_sidecar_port=8770,
        local_tunnel_port=18770,
    )
    holder: dict[str, _Client] = {}

    def factory(*args, **kwargs):
        c = _Client(*args, **kwargs)
        holder["client"] = c
        return c

    monkeypatch.setattr(_residency_proxy.httpx, "Client", factory)

    response = client.get("/ollama/health")

    assert response.status_code == 200
    assert response.json()["reachable"] is True
    assert holder["client"].calls == [
        ("GET", "http://127.0.0.1:18770/ollama/health", {})
    ]
