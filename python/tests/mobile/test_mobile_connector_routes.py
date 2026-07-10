from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from errorta_app import server as server_mod
from errorta_council import paths as council_paths
from errorta_council.run_store import RunStore
from errorta_council.schema import EventStatus, EventType
from errorta_mobile import config as mobile_config
from errorta_mobile import netif as mobile_netif
from errorta_mobile import routes as mobile_routes


@pytest.fixture(autouse=True)
def _isolated_errorta_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    return tmp_path


def test_mobile_health_is_public_and_default_disabled() -> None:
    client = TestClient(server_mod.app)

    response = client.get("/mobile/v1/health")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["status"] == "disabled"
    assert body["mobile_api_version"] == 1
    assert body["mobile_connector"]["enabled"] is False


def test_mobile_version_is_public() -> None:
    client = TestClient(server_mod.app)

    response = client.get("/mobile/v1/version")

    assert response.status_code == 200
    assert response.json()["mobile_api_version"] == 1
    assert response.json()["min_supported_mobile_api_version"] == 1


def test_mobile_data_routes_fail_closed_when_connector_disabled() -> None:
    client = TestClient(server_mod.app)

    response = client.get("/mobile/v1/runs")

    assert response.status_code == 503
    assert response.json()["detail"] == "mobile_connector_disabled"


def test_mobile_data_routes_require_pairing_when_connector_enabled() -> None:
    mobile_config.save({"enabled": True, "bind_mode": "loopback_dev"})
    client = TestClient(server_mod.app)

    response = client.get("/mobile/v1/runs")

    assert response.status_code == 401
    assert response.json()["detail"] == "mobile_device_auth_required"


def test_mobile_runs_projection_is_safe_after_auth_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mobile_config.save({"enabled": True, "bind_mode": "loopback_dev"})
    store = RunStore(runs_dir=council_paths.runs_dir())
    meta = store.create_run(
        room_id="room-mobile",
        room_snapshot={"id": "room-mobile", "name": "Mobile Room"},
        prompt="Summarize this run for my phone " + ("x" * 120),
        corpus_ids=[],
    )
    token = store.acquire_writer(meta.id)
    try:
        store.append_event(
            meta.id,
            type=EventType.POLICY_DECISION_CREATED,
            status=EventStatus.PENDING,
            payload={
                "decision_id": "decision-1",
                "raw_tool_result": "never send this to mobile",
            },
            writer=token,
        )
    finally:
        store.release_writer(token)
    monkeypatch.setattr(
        mobile_routes.mobile_auth,
        "require_capability",
        lambda _request, _capability: {"device_id": "phone-1"},
    )
    client = TestClient(server_mod.app)

    response = client.get("/mobile/v1/runs")

    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["runs"]) == 1
    projected = body["runs"][0]
    assert projected["run_id"] == meta.id
    assert projected["room_name"] == "Mobile Room"
    assert projected["needs_attention"] is True
    assert projected["pending_decision_count"] == 1
    assert len(projected["title"]) <= 80
    assert "raw_tool_result" not in json.dumps(projected)
    assert "never send this to mobile" not in json.dumps(projected)


def test_mobile_settings_read_and_write_are_desktop_scoped() -> None:
    client = TestClient(server_mod.app)

    denied = client.put(
        "/settings/mobile-connector",
        json={"enabled": True, "bind_mode": "loopback_dev"},
    )
    assert denied.status_code == 403

    saved = client.put(
        "/settings/mobile-connector",
        json={"enabled": True, "bind_mode": "loopback_dev", "port": 8791},
        headers={"x-errorta-origin": "tauri-ui"},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["enabled"] is True
    assert saved.json()["bind_mode"] == "loopback_dev"
    assert saved.json()["port"] == 8791

    loaded = client.get("/settings/mobile-connector")
    assert loaded.status_code == 200
    assert loaded.json()["enabled"] is True
    assert loaded.json()["device_count"] == 0


def test_mobile_settings_validation_errors_are_stable() -> None:
    client = TestClient(server_mod.app)

    response = client.put(
        "/settings/mobile-connector",
        json={"enabled": True, "bind_mode": "disabled"},
        headers={"x-errorta-origin": "tauri-ui"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "mobile_enabled_requires_bind_mode"


def test_mobile_settings_rejects_disabling_pin_for_lan() -> None:
    client = TestClient(server_mod.app)

    response = client.put(
        "/settings/mobile-connector",
        json={
            "enabled": True,
            "bind_mode": "lan",
            "lan_bind_address": "192.0.2.14",
            "pairing_pin_required": False,
        },
        headers={"x-errorta-origin": "tauri-ui"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "mobile_pairing_pin_required_for_non_loopback"


def test_mobile_lan_addresses_are_desktop_scoped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        mobile_netif,
        "lan_ipv4_candidates",
        lambda: [
            {
                "address": "192.0.2.14",
                "interface": "default",
                "is_default": True,
            }
        ],
    )
    client = TestClient(server_mod.app)

    denied = client.get("/settings/mobile-connector/lan-addresses")
    assert denied.status_code == 403

    allowed = client.get(
        "/settings/mobile-connector/lan-addresses",
        headers={"x-errorta-origin": "tauri-ui"},
    )
    assert allowed.status_code == 200
    assert allowed.json()["addresses"][0]["address"] == "192.0.2.14"


def test_diagnostics_lifecycle_surfaces_mobile_connector_without_host() -> None:
    mobile_config.save({
        "enabled": True,
        "bind_mode": "explicit_host",
        "explicit_host": "macbook.private.tailnet.ts.net",
    })
    client = TestClient(server_mod.app)

    response = client.get("/diagnostics/lifecycle?tail_lines=0")

    assert response.status_code == 200, response.text
    mobile = response.json()["mobile_connector"]
    assert mobile["enabled"] is True
    assert mobile["explicit_host_set"] is True
    assert "explicit_host" not in mobile
    assert "macbook.private.tailnet.ts.net" not in json.dumps(response.json())
