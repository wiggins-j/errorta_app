"""F065 B3b — config validation + listener lifecycle sync."""
from __future__ import annotations

import httpx
import pytest

from errorta_app import mobile_lifecycle
from errorta_mobile import config as mobile_config


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    yield
    mobile_lifecycle.stop()


def test_lan_mode_requires_bind_address():
    with pytest.raises(ValueError, match="lan_bind_address_required"):
        mobile_config.normalize({"enabled": True, "bind_mode": "lan"})


def test_lan_bind_rejects_all_interfaces():
    with pytest.raises(ValueError, match="bind_must_be_specific"):
        mobile_config.normalize({
            "enabled": True, "bind_mode": "lan", "lan_bind_address": "0.0.0.0",
        })


def test_lan_bind_rejects_invalid_ip():
    with pytest.raises(ValueError, match="lan_bind_address_invalid"):
        mobile_config.normalize({
            "enabled": True, "bind_mode": "lan", "lan_bind_address": "not-an-ip",
        })


def test_sync_disabled_runs_nothing():
    cfg = mobile_config.save({"enabled": False, "bind_mode": "disabled"})
    assert mobile_lifecycle.sync(cfg)["running"] is False


def test_sync_starts_tls_listener_on_loopback_and_stops():
    cfg = mobile_config.save({
        "enabled": True, "bind_mode": "loopback_dev", "pairing_enabled": True,
        "require_tls": True, "port": 8795,
    })
    status = mobile_lifecycle.sync(cfg)
    assert status["running"] is True
    assert status["host"] == "127.0.0.1"
    assert status["cert_sha256"]
    # Live: TLS /healthz reachable on the configured port.
    cert = mobile_config.mobile_dir() / "tls" / "server-cert.pem"
    r = httpx.get(f"https://127.0.0.1:{status['port']}/healthz",
                  verify=str(cert), timeout=5)
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert mobile_lifecycle.status()["running"] is True
    mobile_lifecycle.stop()
    assert mobile_lifecycle.status()["running"] is False
