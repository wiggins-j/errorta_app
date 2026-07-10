from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from errorta_app import server as server_mod
from errorta_mobile import config as mobile_config
from errorta_mobile import devices as mobile_devices
from errorta_mobile import inbox as mobile_inbox


@pytest.fixture(autouse=True)
def _isolated_errorta_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    return tmp_path


def _auth_headers(*, send_messages: bool = True) -> dict[str, str]:
    mobile_config.save({"enabled": True, "bind_mode": "loopback_dev"})
    token = f"session-token-{send_messages}"
    record = mobile_devices.create(
        display_name="Share Phone",
        platform="ios",
        public_key=f"public-key-{send_messages}",
        session_token=token,
    )
    # F065: send_messages is no longer a default capability — grant it
    # explicitly for tests that exercise it.
    mobile_devices.update_capabilities(
        record["device_id"], {"send_messages": send_messages}
    )
    return {
        "x-errorta-mobile-device-id": record["device_id"],
        "authorization": f"Bearer {token}",
    }


def test_inbox_route_requires_paired_device() -> None:
    mobile_config.save({"enabled": True, "bind_mode": "loopback_dev"})
    client = TestClient(server_mod.app)

    response = client.post(
        "/mobile/v1/inbox-items",
        json={"kind": "text", "text": "hello"},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "mobile_device_auth_required"


def test_create_inbox_item_records_device_and_does_not_fetch_url() -> None:
    client = TestClient(server_mod.app)
    headers = _auth_headers()

    response = client.post(
        "/mobile/v1/inbox-items",
        json={
            "kind": "url",
            "title": "Docs",
            "text": "https://example.com/docs",
            "source_app": "com.apple.mobilesafari",
        },
        headers=headers,
    )

    assert response.status_code == 200, response.text
    item = response.json()["item"]
    assert item["kind"] == "url"
    assert item["text"] == "https://example.com/docs"
    assert item["device_id"] == headers["x-errorta-mobile-device-id"]
    assert item["status"] == "pending"
    stored = json.loads(mobile_inbox.inbox_path().read_text(encoding="utf-8"))
    assert stored["items"][0]["text"] == "https://example.com/docs"
    assert "fetched" not in json.dumps(stored)


def test_inbox_payload_caps_are_enforced() -> None:
    client = TestClient(server_mod.app)

    response = client.post(
        "/mobile/v1/inbox-items",
        json={"kind": "url", "text": "x" * (mobile_inbox.MAX_URL_LENGTH + 1)},
        headers=_auth_headers(),
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "mobile_inbox_url_too_large"


def test_inbox_requires_send_messages_capability() -> None:
    client = TestClient(server_mod.app)

    response = client.post(
        "/mobile/v1/inbox-items",
        json={"kind": "text", "text": "hello"},
        headers=_auth_headers(send_messages=False),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "mobile_capability_forbidden:send_messages"


def test_inbox_list_is_scoped_to_current_device() -> None:
    client = TestClient(server_mod.app)
    first = _auth_headers()
    second = _auth_headers()
    client.post(
        "/mobile/v1/inbox-items",
        json={"kind": "text", "text": "first"},
        headers=first,
    )
    client.post(
        "/mobile/v1/inbox-items",
        json={"kind": "text", "text": "second"},
        headers=second,
    )

    response = client.get("/mobile/v1/inbox-items", headers=first)

    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["text"] == "first"
