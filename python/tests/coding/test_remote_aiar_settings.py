from __future__ import annotations

import json
import os
import stat

from fastapi.testclient import TestClient

from errorta_app.server import app
from errorta_project_grounding import remote_config
from errorta_project_grounding.remote_adapter import remote_aiar_config


_TAURI = {"x-errorta-origin": "tauri-ui"}


def test_remote_aiar_settings_roundtrip_masks_token(tmp_errorta_home) -> None:
    with TestClient(app) as client:
        saved = client.put(
            "/settings/remote-aiar",
            headers=_TAURI,
            json={
                "base_url": "http://127.0.0.1:8766/",
                "tunnel_port": 8766,
                "token": "watchdog-token-1234",
                "timeout_s": 42,
                "verify": False,
            },
        )
        assert saved.status_code == 200
        body = saved.json()
        assert body["configured"] is True
        assert body["base_url"] == "http://127.0.0.1:8766"
        assert body["tunnel_port"] == 8766
        assert body["timeout_s"] == 42
        assert body["verify"] is False
        assert body["token_configured"] is True
        assert body["token_preview"] == "…1234"
        assert "watchdog-token" not in json.dumps(body)

        loaded = client.get("/settings/remote-aiar", headers=_TAURI)
        assert loaded.status_code == 200
        assert loaded.json()["token_preview"] == "…1234"
        assert "watchdog-token" not in json.dumps(loaded.json())

    raw = json.loads(remote_config.path().read_text(encoding="utf-8"))
    assert raw["token"] == "watchdog-token-1234"
    assert stat.S_IMODE(os.stat(remote_config.path()).st_mode) == 0o600


def test_remote_aiar_settings_requires_tauri_origin(tmp_errorta_home) -> None:
    with TestClient(app) as client:
        assert client.get("/settings/remote-aiar").status_code == 403
        assert client.put(
            "/settings/remote-aiar",
            json={"base_url": "http://127.0.0.1:8766", "token": "x"},
        ).status_code == 403


def test_remote_aiar_settings_clear_removes_config(tmp_errorta_home) -> None:
    remote_config.save(base_url="http://127.0.0.1:8766", token="tok")

    with TestClient(app) as client:
        cleared = client.put(
            "/settings/remote-aiar",
            headers=_TAURI,
            json={"clear": True},
        )

    assert cleared.status_code == 200
    assert cleared.json()["configured"] is False
    assert not remote_config.path().exists()


def test_remote_aiar_config_prefers_stored_config_over_env(
    tmp_errorta_home,
    monkeypatch,
) -> None:
    monkeypatch.setenv("ERRORTA_AIAR_REMOTE_URL", "http://127.0.0.1:9999")
    monkeypatch.setenv("ERRORTA_AIAR_REMOTE_TOKEN", "env-token")
    remote_config.save(
        base_url="http://127.0.0.1:8766",
        token="stored-token",
        timeout_s=12,
        verify=False,
    )

    cfg = remote_aiar_config()

    assert cfg is not None
    assert cfg.base_url == "http://127.0.0.1:8766"
    assert cfg.token == "stored-token"
    assert cfg.timeout_s == 12
    assert cfg.verify is False


def test_remote_aiar_config_falls_back_to_env_when_no_stored_config(
    tmp_errorta_home,
    monkeypatch,
) -> None:
    monkeypatch.setenv("ERRORTA_AIAR_REMOTE_URL", "http://127.0.0.1:8766/")
    monkeypatch.setenv("ERRORTA_AIAR_REMOTE_TOKEN", "env-token")
    monkeypatch.setenv("ERRORTA_AIAR_REMOTE_TIMEOUT", "7")
    monkeypatch.setenv("ERRORTA_AIAR_REMOTE_VERIFY", "0")

    cfg = remote_aiar_config()

    assert cfg is not None
    assert cfg.base_url == "http://127.0.0.1:8766"
    assert cfg.token == "env-token"
    assert cfg.timeout_s == 7
    assert cfg.verify is False


def test_stored_url_without_token_falls_back_to_env_token(
    tmp_errorta_home,
    monkeypatch,
) -> None:
    # an operator saves an endpoint but no token, with the env token set:
    # the env token must still be used (not silently dropped).
    monkeypatch.setenv("ERRORTA_AIAR_REMOTE_TOKEN", "env-token")
    remote_config.save(base_url="http://127.0.0.1:8766", token=None)

    cfg = remote_aiar_config()

    assert cfg is not None and cfg.base_url == "http://127.0.0.1:8766"
    assert cfg.token == "env-token"  # env fallback, not None


# --- footgun guard: loopback URL without a port merges the tunnel port -------


def test_loopback_url_without_port_merges_tunnel_port() -> None:
    # the exact config that silently hit :80 before the fix
    assert remote_config._normalize_base_url("http://127.0.0.1", 8766) == "http://127.0.0.1:8766"
    assert remote_config._normalize_base_url("http://localhost", 8766) == "http://localhost:8766"


def test_explicit_port_is_preserved() -> None:
    assert remote_config._normalize_base_url("http://127.0.0.1:9000", 8766) == "http://127.0.0.1:9000"


def test_remote_host_without_port_is_not_rewritten() -> None:
    # a real remote URL is never rewritten by a (possibly stale) tunnel port
    assert remote_config._normalize_base_url("https://aiar.example.com", 8766) == "https://aiar.example.com"


def test_empty_url_falls_back_to_tunnel_port() -> None:
    assert remote_config._normalize_base_url("", 8766) == "http://127.0.0.1:8766"
