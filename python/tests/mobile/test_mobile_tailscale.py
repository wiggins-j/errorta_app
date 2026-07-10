"""F071 — Tailscale off-LAN bind/advertise."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from errorta_app import mobile_lifecycle
from errorta_app import server as server_mod
from errorta_mobile import config as mobile_config
from errorta_mobile import netif
from errorta_mobile import tls as mobile_tls
from errorta_mobile.ratelimit import pairing_limiter


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    pairing_limiter.reset()
    return tmp_path


def _tauri() -> dict[str, str]:
    return {"x-errorta-origin": "tauri-ui"}


# --- netif classification ----------------------------------------------------

def test_netif_classifies_tailscale_cgnat() -> None:
    assert netif._kind_for("100.101.102.103") == "tailscale"
    assert netif._kind_for("192.0.2.14") == "lan"
    assert netif._kind_for("10.1.2.3") == "lan"


# --- config validation -------------------------------------------------------

def test_tailscale_bind_must_be_cgnat_and_specific() -> None:
    with pytest.raises(ValueError):
        mobile_config.normalize({"tailscale_bind_address": "192.0.2.14"})  # not CGNAT
    with pytest.raises(ValueError):
        mobile_config.normalize({"tailscale_bind_address": "0.0.0.0"})
    with pytest.raises(ValueError):
        mobile_config.normalize({"also_tailscale": True})  # missing address
    ok = mobile_config.normalize({
        "also_tailscale": True, "tailscale_bind_address": "100.64.1.2",
    })
    assert ok["also_tailscale"] is True
    assert ok["tailscale_bind_address"] == "100.64.1.2"


# --- multi-SAN cert ----------------------------------------------------------

def test_cert_covers_both_lan_and_tailscale_and_is_stable(tmp_path: Path) -> None:
    d = tmp_path / "tls"
    cert, _key = mobile_tls.ensure_self_signed(["192.0.2.14", "100.64.1.2"], d)
    fp1 = mobile_tls.cert_der_sha256(cert)
    assert mobile_tls._cert_covers_host(cert, "192.0.2.14")
    assert mobile_tls._cert_covers_host(cert, "100.64.1.2")
    # Same host set → reused (stable fingerprint), not regenerated.
    cert2, _ = mobile_tls.ensure_self_signed(["192.0.2.14", "100.64.1.2"], d)
    assert mobile_tls.cert_der_sha256(cert2) == fp1


# --- bind-host set -----------------------------------------------------------

def test_bind_hosts_includes_tailscale_when_enabled() -> None:
    cfg = mobile_config.normalize({
        "enabled": True, "bind_mode": "lan", "lan_bind_address": "192.0.2.14",
        "also_tailscale": True, "tailscale_bind_address": "100.64.1.2",
    })
    assert mobile_lifecycle._bind_hosts(cfg) == ["192.0.2.14", "100.64.1.2"]
    cfg_off = dict(cfg)
    cfg_off["also_tailscale"] = False
    assert mobile_lifecycle._bind_hosts(cfg_off) == ["192.0.2.14"]


# --- pairing payload advertises the real tailscale IP ------------------------

def test_pairing_payload_advertises_tailscale_host() -> None:
    mobile_config.save({
        "enabled": True, "bind_mode": "lan", "lan_bind_address": "192.0.2.14",
        "pairing_enabled": True, "require_tls": False,
        "also_tailscale": True, "tailscale_bind_address": "100.64.1.2",
    })
    client = TestClient(server_mod.app)
    started = client.post(
        "/settings/mobile-connector/pairing/start",
        json={"desktop_name": "Mac", "ttl_seconds": 120}, headers=_tauri(),
    ).json()
    hosts = started["pairing_payload"]["hosts"]
    kinds = {h["kind"]: h["host"] for h in hosts}
    assert kinds.get("lan") == "192.0.2.14"
    assert kinds.get("tailscale") == "100.64.1.2"  # real IP, not the placeholder
    assert "tailscale" not in [h["host"] for h in hosts]  # placeholder string gone
