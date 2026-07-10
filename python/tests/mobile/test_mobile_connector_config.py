from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from errorta_mobile import config


@pytest.fixture(autouse=True)
def _isolated_errorta_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    return tmp_path


def test_load_creates_default_disabled_config() -> None:
    loaded = config.load()

    assert loaded["enabled"] is False
    assert loaded["bind_mode"] == "disabled"
    assert loaded["require_tls"] is True
    assert config.config_path().exists()
    assert json.loads(config.config_path().read_text(encoding="utf-8")) == loaded
    assert stat.S_IMODE(config.config_path().stat().st_mode) == 0o600


def test_save_persists_owner_only_enabled_loopback_config() -> None:
    saved = config.save({
        "enabled": True,
        "bind_mode": "loopback_dev",
        "port": "8790",
        "allowed_networks": ["tailscale", "tailscale"],
    })

    assert saved["enabled"] is True
    assert saved["bind_mode"] == "loopback_dev"
    assert saved["port"] == 8790
    assert saved["pairing_pin_required"] is False
    assert saved["allowed_networks"] == ["tailscale"]
    assert json.loads(config.config_path().read_text(encoding="utf-8")) == saved
    assert stat.S_IMODE(config.config_path().stat().st_mode) == 0o600
    assert not list(config.mobile_dir().glob(".mobile-connector-*.json"))


def test_enabled_connector_rejects_disabled_bind_mode() -> None:
    with pytest.raises(ValueError, match="mobile_enabled_requires_bind_mode"):
        config.normalize({"enabled": True, "bind_mode": "disabled"})


def test_non_loopback_bind_derives_pairing_pin_required() -> None:
    saved = config.save({
        "enabled": True,
        "bind_mode": "lan",
        "lan_bind_address": "192.0.2.14",
    })

    assert saved["pairing_pin_required"] is True


def test_non_loopback_bind_coerces_pairing_pin_required() -> None:
    normalized = config.normalize({
        "enabled": True,
        "bind_mode": "lan",
        "lan_bind_address": "192.0.2.14",
        "pairing_pin_required": False,
    })

    assert normalized["pairing_pin_required"] is True


def test_enabled_explicit_host_requires_host_value() -> None:
    with pytest.raises(ValueError, match="mobile_explicit_host_required"):
        config.normalize({"enabled": True, "bind_mode": "explicit_host"})


def test_public_status_does_not_expose_explicit_host() -> None:
    status = config.public_status({
        "enabled": True,
        "bind_mode": "explicit_host",
        "explicit_host": "macbook.private.tailnet.ts.net",
    })

    assert status["explicit_host_set"] is True
    assert "explicit_host" not in status
    assert "macbook.private.tailnet.ts.net" not in json.dumps(status)


def test_device_count_accepts_current_and_future_storage_shapes() -> None:
    config.devices_path().write_text(
        json.dumps({"devices": [{"id": "phone-1"}, {"id": "phone-2"}]}),
        encoding="utf-8",
    )
    assert config.device_count() == 2

    config.devices_path().write_text(
        json.dumps([{"id": "phone-1"}]),
        encoding="utf-8",
    )
    assert config.device_count() == 1
