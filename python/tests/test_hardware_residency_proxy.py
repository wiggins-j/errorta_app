"""Hardware scan routes follow the active data residency target."""
from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from errorta_app.routes import _residency_proxy
from errorta_app.routes import hardware as hardware_routes


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(hardware_routes.router)
    return TestClient(app)


class _Response:
    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


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
    ):
        self.calls.append((method, url, headers))
        return _Response(200, {"os": {"name": "Linux"}, "source": "remote"})


def test_hardware_scan_uses_local_scanner_in_local_mode(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_errorta_home
) -> None:
    monkeypatch.setattr(hardware_routes.scanner, "scan", lambda: {"source": "local"})

    response = client.post("/hardware/scan")

    assert response.status_code == 200
    assert response.json() == {"source": "local"}


def test_hardware_scan_proxies_to_ssh_tunnel_port(
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

    response = client.post("/hardware/scan")

    assert response.status_code == 200
    assert response.json()["source"] == "remote"
    assert holder["client"].calls == [
        ("POST", "http://127.0.0.1:18770/hardware/scan", {})
    ]


def test_hardware_scan_rejects_ssh_remote_without_tunnel_port(
    client: TestClient, tmp_errorta_home
) -> None:
    from errorta_residency import config as residency_config

    residency_config.update(
        mode="ssh-remote",
        ssh_host="example-host",
        remote_sidecar_port=None,
        local_tunnel_port=None,
    )

    response = client.post("/hardware/scan")

    assert response.status_code == 503
    assert "local tunnel port" in response.json()["detail"]


def test_hardware_scan_does_not_use_remote_sidecar_port_as_local_proxy(
    client: TestClient, tmp_errorta_home
) -> None:
    from errorta_residency import config as residency_config

    residency_config.update(
        mode="ssh-remote",
        ssh_host="example-host",
        remote_sidecar_port=8770,
    )

    response = client.post("/hardware/scan")

    assert response.status_code == 503
    assert "local tunnel port" in response.json()["detail"]


def test_hardware_scan_rejects_cloud_until_auth_ships(
    client: TestClient, tmp_errorta_home
) -> None:
    from errorta_residency import config as residency_config

    residency_config.update(mode="cloud", cloud_url="https://cloud.example.com")

    response = client.post("/hardware/scan")

    assert response.status_code == 501
    assert "not enabled" in response.json()["detail"]
