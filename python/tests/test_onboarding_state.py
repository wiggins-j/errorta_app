"""Onboarding state starts with data-residency selection."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from errorta_app.routes import _residency_proxy
from errorta_app.routes import onboarding as onboarding_routes


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(onboarding_routes.router)
    return TestClient(app)


def test_fresh_install_recommends_residency_first(tmp_errorta_home) -> None:
    response = _client().get("/onboarding/state")

    assert response.status_code == 200
    body = response.json()
    assert body["residency_ready"] is False
    assert body["residency_mode"] == "local"
    assert body["recommended_next_step"] == "residency"


def test_after_residency_selection_recommends_hardware(
    tmp_errorta_home, monkeypatch
) -> None:
    from errorta_residency import config as residency_config

    residency_config.update(mode="local")
    monkeypatch.setattr(onboarding_routes, "_hardware_ready", lambda: False)
    monkeypatch.setattr(onboarding_routes, "_ollama_state", lambda: (False, "offline"))
    monkeypatch.setattr(onboarding_routes, "_corpora", lambda: [])

    response = _client().get("/onboarding/state")

    assert response.status_code == 200
    body = response.json()
    assert body["residency_ready"] is True
    assert body["recommended_next_step"] == "hardware"


class _OllamaHealthResponse:
    status_code = 200
    text = ""

    def json(self):
        return {
            "reachable": True,
            "host": "http://127.0.0.1:11434",
            "version": "0.7.0",
            "error": None,
            "managed_by_errorta": True,
            "needs_install": False,
            "platform_supported": True,
        }


class _OllamaHealthClient:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        return None

    def request(self, method, url, headers, json=None):
        assert method == "GET"
        assert url == "http://127.0.0.1:18770/ollama/health"
        return _OllamaHealthResponse()


class _RemoteCorporaResponse:
    status_code = 200
    text = ""

    def json(self):
        return {
            "corpora": [
                {"name": "remote-welcome", "file_count": 6, "ready_count": 6}
            ]
        }


class _RemoteCorporaClient:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        return None

    def request(self, method, url, headers, json=None):
        assert method == "GET"
        assert url == "http://127.0.0.1:18770/onboarding/corpora"
        return _RemoteCorporaResponse()


def test_onboarding_ollama_ready_uses_active_residency_target(
    tmp_errorta_home, monkeypatch
) -> None:
    from errorta_residency import config as residency_config

    residency_config.update(
        mode="ssh-remote",
        ssh_host="example-host",
        remote_sidecar_port=8770,
        local_tunnel_port=18770,
    )
    monkeypatch.setattr(onboarding_routes, "_hardware_ready", lambda: True)
    monkeypatch.setattr(onboarding_routes, "_corpora", lambda: [])
    monkeypatch.setattr(_residency_proxy.httpx, "Client", _OllamaHealthClient)

    response = _client().get("/onboarding/state")

    assert response.status_code == 200
    body = response.json()
    assert body["ollama_ready"] is True
    assert body["recommended_next_step"] == "welcome"


def test_onboarding_corpora_use_active_residency_target(
    tmp_errorta_home, monkeypatch
) -> None:
    from errorta_residency import config as residency_config

    residency_config.update(
        mode="ssh-remote",
        ssh_host="example-host",
        remote_sidecar_port=8770,
        local_tunnel_port=18770,
    )
    monkeypatch.setattr(onboarding_routes, "_hardware_ready", lambda: True)
    monkeypatch.setattr(onboarding_routes, "_ollama_state", lambda: (True, None))
    monkeypatch.setattr(_residency_proxy.httpx, "Client", _RemoteCorporaClient)

    response = _client().get("/onboarding/state")

    assert response.status_code == 200
    body = response.json()
    assert body["corpora"] == ["remote-welcome"]
    assert body["recommended_next_step"] == "done"
