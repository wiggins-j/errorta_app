"""Tests for the residency FastAPI router (errorta_app.routes.residency).

F-INFRA-12 Phase B Slice 2 — GET / PUT / probe routes against a tmp-dir
residency config. All network I/O is mocked at the ``httpx.Client``
factory level so no real HTTPS calls escape the test suite.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from errorta_app.routes import residency as residency_routes
from errorta_residency import probe as residency_probe


@pytest.fixture
def client() -> TestClient:
    """Mount only the residency router on a minimal app.

    Mirrors test_judge_routes.py's pattern: keeping siblings out so
    fixture-based mocks of unrelated modules don't bleed in.
    """
    app = FastAPI()
    app.include_router(residency_routes.router)
    return TestClient(app)


def _config_path(tmp_errorta_home: Path) -> Path:
    return tmp_errorta_home / ".errorta" / "data-residency.json"


def _patch_probe(monkeypatch: pytest.MonkeyPatch, *, ok: bool, body: dict | None = None,
                 error: str | None = None) -> MagicMock:
    """Stub out ``residency_probe.probe_https_url`` with a recording MagicMock.

    We patch both the source attribute and the routes-module-local
    reference so calls from inside ``routes/residency.py`` are caught
    regardless of which import path the route uses.
    """
    result = {
        "ok": ok,
        "status": 200 if ok else None,
        "body": body if ok else None,
        "error": error,
    }
    stub = MagicMock(return_value=result)
    monkeypatch.setattr(residency_probe, "probe_https_url", stub)
    monkeypatch.setattr(
        residency_routes.residency_probe, "probe_https_url", stub
    )
    return stub


# ---------------------------------------------------------------------------
# GET /residency
# ---------------------------------------------------------------------------


def test_get_residency_default(client: TestClient, tmp_errorta_home: Path) -> None:
    """Fresh home dir → mode=local, tunnel down, no remote healthz."""
    resp = client.get("/residency")
    assert resp.status_code == 200
    body = resp.json()
    assert body["config"]["mode"] == "local"
    assert body["config"]["cloud_token"] is None
    assert body["tunnel_state"] == "down"
    assert body["remote_healthz"] is None


def test_get_residency_cloud_invokes_probe(
    client: TestClient,
    tmp_errorta_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET in cloud mode probes the upstream and surfaces its /healthz."""
    # Persist a cloud config directly through the config layer, then GET
    # and assert the probe was hit + the upstream body bubbled through.
    from errorta_residency import config as residency_config

    _patch_probe(
        monkeypatch,
        ok=True,
        body={"service": "errorta-sidecar", "version": "0.5.0", "aiar_pin": {"source": "pinned"}},
    )
    # Persist via update so validation runs (cloud_url must be https).
    residency_config.update(mode="cloud", cloud_url="https://cloud.example/api")

    resp = client.get("/residency")
    assert resp.status_code == 200
    body = resp.json()
    assert body["config"]["mode"] == "cloud"
    assert body["config"]["cloud_url"] == "https://cloud.example/api"
    assert body["config"]["cloud_token"] is None  # never echoed
    assert body["remote_healthz"]["service"] == "errorta-sidecar"
    assert body["remote_healthz"]["aiar_pin"] == {"source": "pinned"}


# ---------------------------------------------------------------------------
# PUT /residency
# ---------------------------------------------------------------------------


def test_put_residency_local_clears_remote_fields(
    client: TestClient,
    tmp_errorta_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PUT mode=local zeroes out any prior ssh/cloud fields on disk."""
    # Seed with an ssh-remote state so we can observe the clear.
    from errorta_residency import config as residency_config

    residency_config.update(
        mode="ssh-remote", ssh_host="example-host", ssh_username="ops"
    )
    resp = client.put("/residency", json={"mode": "local"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["config"]["mode"] == "local"
    assert body["config"]["ssh_host"] is None
    assert body["config"]["ssh_username"] is None

    persisted = json.loads(_config_path(tmp_errorta_home).read_text())
    assert persisted["mode"] == "local"
    assert persisted["ssh_host"] is None


def test_put_residency_ssh_remote_persists(
    client: TestClient,
    tmp_errorta_home: Path,
) -> None:
    """PUT mode=ssh-remote with ssh_host=example-host round-trips through GET."""
    resp = client.put(
        "/residency",
        json={"mode": "ssh-remote", "ssh_host": "example-host", "ssh_username": "ops"},
    )
    assert resp.status_code == 200
    assert resp.json()["config"]["mode"] == "ssh-remote"
    assert resp.json()["config"]["ssh_host"] == "example-host"

    got = client.get("/residency").json()
    assert got["config"]["mode"] == "ssh-remote"
    assert got["config"]["ssh_host"] == "example-host"
    assert got["tunnel_state"] == "down"  # Slice 2 stand-in


def test_put_residency_cloud_is_disabled_and_no_write(
    client: TestClient,
    tmp_errorta_home: Path,
) -> None:
    """Cloud residency is v0.6-only until token auth ships."""
    # Seed with a known mode=local persisted state.
    from errorta_residency import config as residency_config

    residency_config.update(mode="local")
    before = json.loads(_config_path(tmp_errorta_home).read_text())

    resp = client.put(
        "/residency",
        json={"mode": "cloud", "cloud_url": "https://cloud.example/api", "cloud_token": "tk_xyz"},
    )
    assert resp.status_code == 501
    detail = resp.json()["detail"]
    assert detail["field"] == "mode"
    assert "not enabled" in detail["error"]

    after = json.loads(_config_path(tmp_errorta_home).read_text())
    assert after == before  # nothing was written


def test_put_residency_cloud_does_not_probe_or_persist(
    client: TestClient,
    tmp_errorta_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cloud rejection happens before probe so no token leaves the process."""
    stub = _patch_probe(
        monkeypatch,
        ok=True,
        body={"service": "errorta-sidecar", "aiar_pin": {"source": "pinned"}},
    )
    from errorta_residency import config as residency_config

    residency_config.update(mode="local")
    before = json.loads(_config_path(tmp_errorta_home).read_text())

    resp = client.put(
        "/residency",
        json={
            "mode": "cloud",
            "cloud_url": "https://cloud.example/api",
            "cloud_token": "tk_supersecret",
        },
    )
    assert resp.status_code == 501

    persisted = json.loads(_config_path(tmp_errorta_home).read_text())
    assert persisted == before
    raw = _config_path(tmp_errorta_home).read_text()
    assert "tk_supersecret" not in raw
    assert stub.call_count == 0

def test_put_residency_cloud_http_scheme_hits_disabled_gate_first(
    client: TestClient,
    tmp_errorta_home: Path,
) -> None:
    """Cloud is disabled regardless of URL shape until the auth slice lands."""
    resp = client.put(
        "/residency",
        json={"mode": "cloud", "cloud_url": "http://insecure.example/api"},
    )
    assert resp.status_code == 501
    detail = resp.json()["detail"]
    assert detail["field"] == "mode"
    assert "not enabled" in detail["error"]


# ---------------------------------------------------------------------------
# POST /residency/probe
# ---------------------------------------------------------------------------


def test_probe_endpoint_success(
    client: TestClient,
    tmp_errorta_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """200 from the upstream → ``ok=True`` + body surfaced."""
    _patch_probe(monkeypatch, ok=True, body={"service": "errorta-sidecar"})
    resp = client.post(
        "/residency/probe",
        json={"url": "https://cloud.example/api", "token": "tk_xyz"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["body"]["service"] == "errorta-sidecar"


def test_probe_endpoint_network_failure(
    client: TestClient,
    tmp_errorta_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A network failure does not raise — returned as ``ok=False``."""
    _patch_probe(monkeypatch, ok=False, error="ConnectError: dns lookup failed")
    resp = client.post(
        "/residency/probe",
        json={"url": "https://does-not-exist.example/api"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "dns" in body["error"].lower()


def test_probe_endpoint_rejects_http_scheme(
    client: TestClient,
    tmp_errorta_home: Path,
) -> None:
    """Bad URL shape is reported through the same ``ok=False`` channel."""
    resp = client.post("/residency/probe", json={"url": "http://insecure.example"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "https" in body["error"].lower()


# ---------------------------------------------------------------------------
# probe.probe_https_url — direct unit coverage via mock_httpx_client
# ---------------------------------------------------------------------------


def test_probe_https_url_swallows_httpx_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``httpx.HTTPError`` and friends bubble through as ok=False, no raise."""
    import httpx

    class _BoomClient:
        def __init__(self, *_a, **_kw) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc) -> None:
            return None

        def get(self, *_a, **_kw):
            raise httpx.ConnectError("nope")

    monkeypatch.setattr(httpx, "Client", _BoomClient)
    result = residency_probe.probe_https_url("https://x.example")
    assert result["ok"] is False
    assert result["status"] is None
    assert "nope" in result["error"]


def test_validate_https_url_strips_and_rejects() -> None:
    """Trim whitespace, accept https, reject http / empty / non-string."""
    assert residency_probe.validate_https_url("  https://x.example/api  ") == "https://x.example/api"
    with pytest.raises(ValueError):
        residency_probe.validate_https_url("http://x.example")
    with pytest.raises(ValueError):
        residency_probe.validate_https_url("")
    with pytest.raises(ValueError):
        residency_probe.validate_https_url("https://")
