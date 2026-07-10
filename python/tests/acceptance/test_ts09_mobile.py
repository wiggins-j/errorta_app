"""TS-09 — Mobile Companion: acceptance journey (hermetic slice).

Owner-only gating on the connector + the device permission lifecycle, without a
real phone: settings read is owner-gated; a non-owner cannot approve a pairing
(TC-09.3); a seeded device is read-only by default (TC-09.4); granting a
capability works (TC-09.5); revoking removes access (TC-09.6). Real QR/PIN pairing
and on-device control stay manual per the plan.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from errorta_app.server import app
from errorta_mobile import devices as mobile_devices

pytestmark = [pytest.mark.acceptance, pytest.mark.security]

OWNER = {"x-errorta-origin": "tauri-ui"}


@pytest.fixture
def client(tmp_errorta_home) -> TestClient:
    return TestClient(app)


def test_ts09_connector_owner_gated(client) -> None:
    # Connector settings are readable on loopback.
    assert client.get("/settings/mobile-connector", headers=OWNER).status_code == 200
    # TC-09.3: a non-owner cannot approve a pairing (origin checked first).
    assert client.post("/settings/mobile-connector/pairing/any-session/approve").status_code == 403


def test_ts09_device_capability_lifecycle(client) -> None:
    # Seed a paired device directly (the real phone QR/PIN handshake is manual).
    dev = mobile_devices.create(
        display_name="iPhone", platform="ios",
        public_key="pk_test", session_token="tok_test",
    )
    device_id = dev["device_id"]

    listed = client.get("/settings/mobile-connector/devices", headers=OWNER).json()
    rec = next(d for d in listed["devices"] if d["device_id"] == device_id)
    # TC-09.4: default read-only.
    assert rec["capabilities"]["read_runs"] is True
    assert rec["capabilities"]["start_runs"] is False

    # TC-09.5: grant a capability.
    patched = client.patch(
        f"/settings/mobile-connector/devices/{device_id}",
        json={"capabilities": {"start_runs": True}}, headers=OWNER,
    )
    assert patched.status_code == 200
    assert patched.json()["device"]["capabilities"]["start_runs"] is True

    # TC-09.6: revoke removes access.
    revoked = client.post(
        f"/settings/mobile-connector/devices/{device_id}/revoke", headers=OWNER
    )
    assert revoked.status_code == 200
    assert revoked.json()["device"]["revoked_at"] is not None
