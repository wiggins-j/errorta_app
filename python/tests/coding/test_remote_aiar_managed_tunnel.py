"""F089 — remote-AIAR managed-tunnel mode (config + adapter resolution + routes).

A managed config (ssh_host set) derives its base_url from the Errorta-owned SSH
tunnel; a bring-your-own-tunnel config (no ssh_host) is byte-identical to today.
The tunnel manager is faked so nothing spawns a real ssh.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import errorta_tunnels
from errorta_project_grounding import remote_adapter
from errorta_project_grounding import remote_config as rc


class _FakeManager:
    def __init__(self) -> None:
        self.ensured: list = []

    def ensure(self, spec, *, wait=True):
        self.ensured.append(spec)
        return 54999

    def status_for(self, spec):
        return {"ssh_host": spec.ssh_host, "state": "up", "local_port": 54999}

    def reconnect(self, spec):
        return True


@pytest.fixture
def fake_tunnels(monkeypatch):
    fake = _FakeManager()
    monkeypatch.setattr(errorta_tunnels, "tunnel_manager", fake)
    return fake


# --- config validation ------------------------------------------------------

def test_managed_requires_remote_port(tmp_errorta_home: Path) -> None:
    with pytest.raises(ValueError):
        rc.save(ssh_host="example-host")  # no remote_port
    saved = rc.save(ssh_host="example-host", remote_port=8766, token="t")
    assert saved.managed is True
    assert saved.configured is True


def test_byo_still_requires_base_url(tmp_errorta_home: Path) -> None:
    with pytest.raises(ValueError):
        rc.save()  # neither base_url nor ssh_host
    saved = rc.save(base_url="http://127.0.0.1:8766", token="t")
    assert saved.managed is False
    assert saved.base_url == "http://127.0.0.1:8766"


def test_save_rejects_flag_injection_ssh_host(tmp_errorta_home: Path) -> None:
    with pytest.raises(ValueError):
        rc.save(ssh_host="-oProxyCommand=evil", remote_port=8766, token="t")


def test_tunnel_spec_built_for_managed_only(tmp_errorta_home: Path) -> None:
    rc.save(base_url="http://127.0.0.1:8766", token="t")
    assert rc.tunnel_spec() is None  # BYO -> no spec
    rc.save(ssh_host="example-host", remote_port=8766, remote_host="127.0.0.1", token="t")
    spec = rc.tunnel_spec()
    assert spec is not None
    assert spec.ssh_host == "example-host" and spec.remote_port == 8766


# --- adapter resolution -----------------------------------------------------

def test_managed_config_derives_base_url_from_tunnel(tmp_errorta_home, fake_tunnels) -> None:
    rc.save(ssh_host="example-host", remote_port=8766, token="tok")
    cfg = remote_adapter.remote_aiar_config()
    assert cfg is not None
    assert cfg.base_url == "http://127.0.0.1:54999"  # derived from the tunnel
    assert len(fake_tunnels.ensured) == 1
    assert fake_tunnels.ensured[0].ssh_host == "example-host"


def test_byo_config_is_unchanged_and_never_touches_tunnel(
    tmp_errorta_home, fake_tunnels,
) -> None:
    rc.save(base_url="http://127.0.0.1:8766", token="tok")
    cfg = remote_adapter.remote_aiar_config()
    assert cfg is not None
    assert cfg.base_url == "http://127.0.0.1:8766"  # verbatim
    assert fake_tunnels.ensured == []  # BYO -> tunnel manager untouched


def test_masked_includes_tunnel_block_for_managed(tmp_errorta_home, fake_tunnels) -> None:
    saved = rc.save(ssh_host="example-host", remote_port=8766, token="secret-1234")
    masked = rc.masked(saved)
    assert masked["managed"] is True
    assert masked["ssh_host"] == "example-host"
    assert masked["remote_port"] == 8766
    assert masked["token_preview"] == "…1234"  # token still masked
    assert masked["tunnel"]["state"] == "up"
    assert masked["tunnel"]["local_port"] == 54999


def test_masked_byo_has_no_tunnel_block(tmp_errorta_home) -> None:
    saved = rc.save(base_url="http://127.0.0.1:8766", token="tok")
    masked = rc.masked(saved)
    assert masked["managed"] is False
    assert "tunnel" not in masked


# --- routes -----------------------------------------------------------------

def _client(tmp_errorta_home: Path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from errorta_app.routes import settings as settings_routes
    app = FastAPI()
    app.include_router(settings_routes.router)
    return TestClient(app, headers={"x-errorta-origin": "tauri-ui"})


def test_put_managed_then_get_reflects_tunnel(tmp_errorta_home, fake_tunnels) -> None:
    client = _client(tmp_errorta_home)
    resp = client.put("/settings/remote-aiar", json={
        "ssh_host": "example-host", "remote_port": 8766, "token": "tok",
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["managed"] is True and body["ssh_host"] == "example-host"
    assert body["tunnel"]["state"] == "up"
    # The PUT eagerly brought the tunnel up.
    assert any(s.ssh_host == "example-host" for s in fake_tunnels.ensured)


def test_reconnect_route_409_when_not_managed(tmp_errorta_home, fake_tunnels) -> None:
    client = _client(tmp_errorta_home)
    client.put("/settings/remote-aiar", json={
        "base_url": "http://127.0.0.1:8766", "token": "tok",
    })
    resp = client.post("/settings/remote-aiar/tunnel/reconnect")
    assert resp.status_code == 409  # BYO mode has no managed tunnel


def test_reconnect_route_kicks_managed_tunnel(tmp_errorta_home, fake_tunnels) -> None:
    client = _client(tmp_errorta_home)
    client.put("/settings/remote-aiar", json={
        "ssh_host": "example-host", "remote_port": 8766, "token": "tok",
    })
    resp = client.post("/settings/remote-aiar/tunnel/reconnect")
    assert resp.status_code == 200
    assert resp.json()["managed"] is True
