"""F065 B3 — the dedicated LAN listener: surface minimization + TLS + fail-closed."""
from __future__ import annotations

import socket

import httpx
import pytest
from fastapi.testclient import TestClient

from errorta_app import mobile_server
from errorta_mobile import tls as mobile_tls


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_lan_app_exposes_only_mobile_and_healthz():
    app = mobile_server.build_mobile_app()
    paths = {getattr(r, "path", "") for r in app.routes}
    offending = [
        p for p in paths
        if p and not p.startswith("/mobile/v1") and p != "/healthz"
    ]
    # The structural guarantee: nothing but /mobile/v1/* and /healthz on the LAN.
    assert offending == [], f"LAN app leaks non-mobile routes: {offending}"


def test_lan_app_does_not_serve_council_routes():
    client = TestClient(mobile_server.build_mobile_app())
    assert client.get("/council/rooms").status_code == 404
    assert client.get("/settings/mobile-connector").status_code == 404
    assert client.post("/settings/mobile-connector/pairing/start").status_code == 404


def test_lan_app_has_no_cors_middleware():
    app = mobile_server.build_mobile_app()
    names = [type(m.cls).__name__ if hasattr(m, "cls") else str(m) for m in app.user_middleware]
    joined = " ".join(str(getattr(m, "cls", m)) for m in app.user_middleware)
    assert "CORSMiddleware" not in joined, names


def test_lan_app_health_is_bare_liveness():
    client = TestClient(mobile_server.build_mobile_app())
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body == {"ok": True}  # no version/config/device_count


def test_start_refuses_all_interfaces_bind(tmp_path):
    cert, key = mobile_tls.ensure_self_signed("127.0.0.1", tmp_path)
    with pytest.raises(ValueError, match="bind_must_be_specific"):
        mobile_server.start_lan_listener(host="0.0.0.0", port=_free_port(),
                                         certfile=cert, keyfile=key)


def test_start_refuses_without_cert(tmp_path):
    with pytest.raises(FileNotFoundError, match="tls_missing"):
        mobile_server.start_lan_listener(
            host="127.0.0.1", port=_free_port(),
            certfile=tmp_path / "nope-cert.pem", keyfile=tmp_path / "nope-key.pem",
        )


def test_live_tls_listener_serves_healthz(tmp_path, tmp_errorta_home):
    cert, key = mobile_tls.ensure_self_signed("127.0.0.1", tmp_path)
    port = _free_port()
    listener = mobile_server.start_lan_listener(
        host="127.0.0.1", port=port, certfile=cert, keyfile=key,
    )
    try:
        assert listener.is_alive()
        # Real TLS request, verifying against the generated cert.
        r = httpx.get(f"https://127.0.0.1:{port}/healthz", verify=str(cert), timeout=5)
        assert r.status_code == 200 and r.json() == {"ok": True}
        # A council route is NOT served here.
        r2 = httpx.get(f"https://127.0.0.1:{port}/council/rooms", verify=str(cert), timeout=5)
        assert r2.status_code == 404
    finally:
        listener.stop()
    assert not listener.is_alive()
