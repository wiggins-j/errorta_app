"""Welcome corpus routes follow the active data-residency target."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from errorta_app.routes import _residency_proxy
from errorta_app.routes import welcome as welcome_routes


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(welcome_routes.router)
    return TestClient(app)


class _JsonResponse:
    status_code = 200
    text = ""

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _RecordingClient:
    calls: list[tuple[str, str, object | None]] = []

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        return None

    def request(self, method, url, headers, json=None):
        self.calls.append((method, url, json))
        assert headers == {}
        if url.endswith("/welcome/install"):
            return _JsonResponse(
                {
                    "corpus_name": "welcome",
                    "suggested_prompt": "What does Errorta do?",
                    "files_ingested": 6,
                    "bytes_downloaded": 1234,
                    "sha256": "abc",
                    "f004_invoked": True,
                    "f004_error": None,
                }
            )
        if url.endswith("/welcome/status"):
            return _JsonResponse(
                {
                    "phase": "done",
                    "progress": 1.0,
                    "bytes_downloaded": 1234,
                    "bytes_total": 1234,
                    "eta_seconds": None,
                    "corpus_name": "welcome",
                    "suggested_prompt": "What does Errorta do?",
                    "error": None,
                }
            )
        raise AssertionError(f"unexpected url: {url}")


def test_welcome_install_proxies_to_active_ssh_sidecar(
    tmp_errorta_home, monkeypatch
) -> None:
    from errorta_residency import config as residency_config

    _RecordingClient.calls = []
    residency_config.update(
        mode="ssh-remote",
        ssh_host="example-host",
        remote_sidecar_port=8770,
        local_tunnel_port=18770,
    )
    monkeypatch.setattr(_residency_proxy.httpx, "Client", _RecordingClient)

    response = _client().post("/welcome/install")

    assert response.status_code == 200
    assert response.json()["corpus_name"] == "welcome"
    assert _RecordingClient.calls == [
        ("POST", "http://127.0.0.1:18770/welcome/install", None)
    ]


def test_welcome_status_proxies_to_active_ssh_sidecar(
    tmp_errorta_home, monkeypatch
) -> None:
    from errorta_residency import config as residency_config

    _RecordingClient.calls = []
    residency_config.update(
        mode="ssh-remote",
        ssh_host="example-host",
        remote_sidecar_port=8770,
        local_tunnel_port=18770,
    )
    monkeypatch.setattr(_residency_proxy.httpx, "Client", _RecordingClient)

    response = _client().get("/welcome/status")

    assert response.status_code == 200
    assert response.json()["phase"] == "done"
    assert _RecordingClient.calls == [
        ("GET", "http://127.0.0.1:18770/welcome/status", None)
    ]
