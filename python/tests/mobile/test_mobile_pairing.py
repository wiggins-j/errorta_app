"""F057/F065 — pairing with desktop owner-confirmation + rate limiting."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from errorta_app import server as server_mod
from errorta_mobile import config as mobile_config
from errorta_mobile import pairing as mobile_pairing
from errorta_mobile.ratelimit import pairing_limiter


@pytest.fixture(autouse=True)
def _isolated_errorta_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    pairing_limiter.reset()  # in-memory limiter is process-global
    mobile_pairing.verify_pin_limiter.reset()
    return tmp_path


def _enable_connector() -> None:
    # require_tls False here so pairing doesn't need a generated cert in tests
    # that aren't exercising the TLS gate.
    mobile_config.save({
        "enabled": True,
        "bind_mode": "loopback_dev",
        "pairing_enabled": True,
        "require_tls": False,
    })


def _enable_pin_connector() -> None:
    mobile_config.save({
        "enabled": True,
        "bind_mode": "lan",
        "lan_bind_address": "192.0.2.14",
        "pairing_enabled": True,
        "require_tls": False,
    })


def _tauri() -> dict[str, str]:
    return {"x-errorta-origin": "tauri-ui"}


def _start(client: TestClient) -> dict[str, Any]:
    r = client.post(
        "/settings/mobile-connector/pairing/start",
        json={"desktop_name": "Dev Mac", "ttl_seconds": 300},
        headers=_tauri(),
    )
    assert r.status_code == 200, r.text
    return r.json()


def _complete(client, started, *, display_name="Test iPhone", public_key="pk-1"):
    p = started["pairing_payload"]
    r = client.post("/mobile/v1/pairing/complete", json={
        "pairing_token": p["pairing_token"],
        "tls_cert_sha256": p["tls_cert_sha256"],
        "display_name": display_name, "platform": "ios", "public_key": public_key,
    })
    return r


def _approve(client, session_id) -> dict[str, Any]:
    r = client.post(
        f"/settings/mobile-connector/pairing/{session_id}/approve", headers=_tauri()
    )
    assert r.status_code == 200, r.text
    return r.json()["pairing"]


def _verify_pin(client, started, pin: str):
    return client.post("/mobile/v1/pairing/verify-pin", json={
        "session_id": started["session_id"],
        "pairing_token": started["pairing_payload"]["pairing_token"],
        "pin": pin,
    })


def _poll(client, session_id, pairing_token) -> dict[str, Any]:
    r = client.post("/mobile/v1/pairing/status", json={
        "session_id": session_id, "pairing_token": pairing_token,
    })
    assert r.status_code == 200, r.text
    return r.json()


def _pair_fully(client, *, public_key="pk-1") -> dict[str, Any]:
    started = _start(client)
    cr = _complete(client, started, public_key=public_key)
    assert cr.status_code == 200, cr.text
    assert cr.json()["state"] == "awaiting_approval"
    assert cr.json()["requires_pin"] is False
    approved = _approve(client, started["session_id"])
    status = _poll(client, started["session_id"], started["pairing_payload"]["pairing_token"])
    return {"started": started, "device": approved["device"],
            "session_token": status["session_token"], "device_id": status["device_id"]}


def _auth(paired) -> dict[str, str]:
    return {
        "x-errorta-mobile-device-id": paired["device_id"],
        "authorization": f"Bearer {paired['session_token']}",
    }


# --- gates --------------------------------------------------------------------

def test_start_requires_desktop_origin() -> None:
    _enable_connector()
    client = TestClient(server_mod.app)
    assert client.post("/settings/mobile-connector/pairing/start", json={}).status_code == 403


def test_start_requires_pairing_enabled() -> None:
    mobile_config.save({"enabled": True, "bind_mode": "loopback_dev", "require_tls": False})
    client = TestClient(server_mod.app)
    r = client.post("/settings/mobile-connector/pairing/start", json={}, headers=_tauri())
    assert r.status_code == 400 and r.json()["detail"] == "mobile_pairing_disabled"


# --- owner-confirmation state machine ----------------------------------------

def test_complete_only_drafts_no_device_until_approved() -> None:
    _enable_connector()
    client = TestClient(server_mod.app)
    started = _start(client)
    cr = _complete(client, started)
    assert cr.status_code == 200
    assert cr.json()["state"] == "awaiting_approval"
    assert cr.json()["requires_pin"] is False
    # No device record yet (approval not given).
    assert mobile_config.device_count() == 0
    # Polling before approval reports awaiting_approval, no token.
    status = _poll(client, started["session_id"], started["pairing_payload"]["pairing_token"])
    assert status["state"] == "awaiting_approval"
    assert "session_token" not in status


def test_full_flow_issues_readonly_device_and_token_once() -> None:
    _enable_connector()
    client = TestClient(server_mod.app)
    paired = _pair_fully(client)
    # READ-ONLY first-pair capabilities (F065).
    caps = paired["device"]["capabilities"]
    assert caps["read_runs"] is True
    assert caps["start_runs"] is False
    assert caps["send_messages"] is False
    assert caps["approve_low_risk"] is False
    assert caps["approve_code_exec"] is False
    # The token works.
    assert client.get("/mobile/v1/capabilities", headers=_auth(paired)).status_code == 200
    # No raw token / pairing token on disk.
    device_file = mobile_config.devices_path().read_text()
    session_file = mobile_pairing.pairing_sessions_path().read_text()
    assert paired["session_token"] not in device_file
    assert paired["session_token"] not in session_file
    assert paired["started"]["pairing_payload"]["pairing_token"] not in session_file
    # A second poll does NOT re-deliver the token (single use).
    again = _poll(client, paired["started"]["session_id"],
                  paired["started"]["pairing_payload"]["pairing_token"])
    assert again["state"] == "consumed"
    assert again.get("session_token") is None


def test_token_minted_only_at_approval_not_at_complete() -> None:
    _enable_connector()
    client = TestClient(server_mod.app)
    started = _start(client)
    _complete(client, started)
    # Before approval the session file must hold NO session_token_sha256.
    sessions = mobile_pairing.load_sessions()
    s = next(x for x in sessions if x["session_id"] == started["session_id"])
    assert s["state"] == "awaiting_approval"
    assert s.get("session_token_sha256") is None


def test_lan_pairing_start_returns_pin_only_to_desktop() -> None:
    _enable_pin_connector()
    client = TestClient(server_mod.app)
    started = _start(client)

    assert started["pin"].isdigit()
    assert len(started["pin"]) == 6
    serialized_payload = json.dumps(started["pairing_payload"])
    assert started["pin"] not in serialized_payload
    session_file = mobile_pairing.pairing_sessions_path().read_text(encoding="utf-8")
    assert started["pin"] not in session_file
    session = mobile_pairing.load_sessions()[0]
    assert session["pin_required"] is True
    assert session["pin_attempts"] == 0
    assert session["pin_salt"]
    assert session["pin_sha256"]


def test_verify_pin_approves_and_poll_delivers_token_once() -> None:
    _enable_pin_connector()
    client = TestClient(server_mod.app)
    started = _start(client)
    cr = _complete(client, started)
    assert cr.status_code == 200, cr.text
    assert cr.json() == {
        "session_id": started["session_id"],
        "state": "awaiting_approval",
        "requires_pin": True,
    }

    approved = _verify_pin(client, started, started["pin"])
    assert approved.status_code == 200, approved.text
    assert approved.json() == {"state": "approved"}

    status = _poll(
        client,
        started["session_id"],
        started["pairing_payload"]["pairing_token"],
    )
    assert status["state"] == "approved"
    assert status["session_token"]
    assert status["device_id"]
    again = _poll(
        client,
        started["session_id"],
        started["pairing_payload"]["pairing_token"],
    )
    assert again["state"] == "consumed"
    assert again.get("session_token") is None


def test_verify_pin_wrong_attempts_burn_session() -> None:
    _enable_pin_connector()
    client = TestClient(server_mod.app)
    started = _start(client)
    assert _complete(client, started).status_code == 200
    wrong_pin = "000000" if started["pin"] != "000000" else "111111"

    for remaining in [4, 3, 2, 1]:
        miss = _verify_pin(client, started, wrong_pin)
        assert miss.status_code == 401, miss.text
        assert miss.json() == {
            "detail": "pairing_pin_mismatch",
            "attempts_remaining": remaining,
        }

    locked = _verify_pin(client, started, wrong_pin)
    assert locked.status_code == 429, locked.text
    assert locked.json()["detail"] == "pairing_pin_locked"
    status = _poll(
        client,
        started["session_id"],
        started["pairing_payload"]["pairing_token"],
    )
    assert status["state"] == "denied"


def test_verify_pin_wrong_token_counts_as_pin_failure() -> None:
    _enable_pin_connector()
    client = TestClient(server_mod.app)
    started = _start(client)
    assert _complete(client, started).status_code == 200

    r = client.post("/mobile/v1/pairing/verify-pin", json={
        "session_id": started["session_id"],
        "pairing_token": "wrong-token",
        "pin": started["pin"],
    })

    assert r.status_code == 401
    assert r.json()["attempts_remaining"] == 4


def test_verify_pin_requires_phone_to_complete_first() -> None:
    _enable_pin_connector()
    client = TestClient(server_mod.app)
    started = _start(client)

    r = _verify_pin(client, started, started["pin"])

    assert r.status_code == 409
    assert r.json()["detail"] == "pairing_not_awaiting_approval"


def test_manual_approve_refuses_pin_required_session() -> None:
    _enable_pin_connector()
    client = TestClient(server_mod.app)
    started = _start(client)
    assert _complete(client, started).status_code == 200

    r = client.post(
        f"/settings/mobile-connector/pairing/{started['session_id']}/approve",
        headers=_tauri(),
    )

    assert r.status_code == 409
    assert r.json()["detail"] == "pairing_pin_required"


def test_desktop_pairing_status_redacts_pin_and_token() -> None:
    _enable_pin_connector()
    client = TestClient(server_mod.app)
    started = _start(client)
    assert _complete(client, started, display_name="Visible Phone").status_code == 200

    r = client.get(
        f"/settings/mobile-connector/pairing/{started['session_id']}",
        headers=_tauri(),
    )

    assert r.status_code == 200, r.text
    body = r.json()["pairing"]
    assert body["state"] == "awaiting_approval"
    assert body["device_draft"]["display_name"] == "Visible Phone"
    serialized = json.dumps(body)
    assert started["pin"] not in serialized
    assert started["pairing_payload"]["pairing_token"] not in serialized


def test_deny_blocks_token_delivery() -> None:
    _enable_connector()
    client = TestClient(server_mod.app)
    started = _start(client)
    _complete(client, started)
    client.post(f"/settings/mobile-connector/pairing/{started['session_id']}/deny",
                headers=_tauri())
    status = _poll(client, started["session_id"], started["pairing_payload"]["pairing_token"])
    assert status["state"] == "denied"
    assert "session_token" not in status


def test_approve_requires_awaiting_approval() -> None:
    _enable_connector()
    client = TestClient(server_mod.app)
    started = _start(client)  # not completed yet
    r = client.post(f"/settings/mobile-connector/pairing/{started['session_id']}/approve",
                    headers=_tauri())
    assert r.status_code == 409
    assert r.json()["detail"] == "pairing_not_awaiting_approval"


def test_cancelled_token_cannot_complete() -> None:
    _enable_connector()
    client = TestClient(server_mod.app)
    started = _start(client)
    client.post(f"/settings/mobile-connector/pairing/{started['session_id']}/cancel",
                headers=_tauri())
    cr = _complete(client, started)
    assert cr.status_code == 400 and cr.json()["detail"] == "pairing_token_cancelled"


def test_expired_token_cannot_complete(monkeypatch) -> None:
    _enable_connector()
    client = TestClient(server_mod.app)
    t0 = dt.datetime(2026, 6, 14, 12, 0, tzinfo=dt.timezone.utc)
    monkeypatch.setattr(mobile_pairing, "_now_dt", lambda: t0)
    started = _start(client)
    monkeypatch.setattr(mobile_pairing, "_now_dt", lambda: t0 + dt.timedelta(seconds=301))
    cr = _complete(client, started)
    assert cr.status_code == 400 and cr.json()["detail"] == "pairing_token_expired"


def test_tls_fingerprint_mismatch_rejected() -> None:
    # Generate a real cert so the session carries a fingerprint to pin against.
    from errorta_mobile import tls as mobile_tls
    mobile_config.save({
        "enabled": True, "bind_mode": "lan", "lan_bind_address": "127.0.0.1",
        "pairing_enabled": True, "require_tls": True,
    })
    mobile_tls.ensure_self_signed("127.0.0.1", mobile_config.mobile_dir() / "tls")
    client = TestClient(server_mod.app)
    started = _start(client)
    assert started["pairing_payload"]["tls_cert_sha256"]  # fingerprint present
    r = client.post("/mobile/v1/pairing/complete", json={
        "pairing_token": started["pairing_payload"]["pairing_token"],
        "tls_cert_sha256": "wrong", "display_name": "x", "platform": "ios",
        "public_key": "pk",
    })
    assert r.status_code == 400 and r.json()["detail"] == "pairing_tls_fingerprint_mismatch"


def test_lan_host_candidate_is_the_bound_ip_not_hostname() -> None:
    # F065 fix: the payload must advertise the IP the listener is bound to (and
    # that the TLS cert's SAN covers), not socket.gethostname()'s `*.local`
    # mDNS name, which the phone may not resolve.
    mobile_config.save({
        "enabled": True, "bind_mode": "lan", "lan_bind_address": "192.0.2.14",
        "port": 8788, "pairing_enabled": True, "require_tls": False,
    })
    client = TestClient(server_mod.app)
    started = _start(client)
    hosts = started["pairing_payload"]["hosts"]
    lan = [h for h in hosts if h["kind"] == "lan"]
    assert lan, f"expected a lan host candidate, got {hosts}"
    assert lan[0]["host"] == "192.0.2.14"
    assert started["pairing_payload"]["port"] == 8788


# --- rate limiting + caps -----------------------------------------------------

def test_brute_force_complete_locks_out() -> None:
    _enable_connector()
    client = TestClient(server_mod.app)
    last = None
    for _ in range(12):
        last = client.post("/mobile/v1/pairing/complete", json={
            "pairing_token": "bogus-token", "tls_cert_sha256": "x",
            "display_name": "x", "platform": "ios", "public_key": "pk",
        })
    assert last.status_code == 429
    assert last.json()["detail"] == "pairing_rate_limited"


def test_max_pending_approvals_capped() -> None:
    _enable_connector()
    client = TestClient(server_mod.app)
    # Fill the pending-approval queue to the cap.
    for _ in range(mobile_pairing.MAX_PENDING_APPROVALS):
        s = _start(client)
        assert _complete(client, s).status_code == 200
    # One more completed pairing should be refused.
    s = _start(client)
    r = _complete(client, s)
    assert r.status_code == 400 and r.json()["detail"] == "pairing_too_many_pending"


def test_require_tls_without_cert_refuses_pairing() -> None:
    mobile_config.save({
        "enabled": True, "bind_mode": "lan", "lan_bind_address": "127.0.0.1",
        "pairing_enabled": True, "require_tls": True,  # but no cert generated
    })
    client = TestClient(server_mod.app)
    r = client.post("/settings/mobile-connector/pairing/start", json={}, headers=_tauri())
    assert r.status_code == 400
    assert r.json()["detail"] == "mobile_tls_unavailable"


# --- revoke + capability + public projection ---------------------------------

def test_revoked_device_cannot_read() -> None:
    _enable_connector()
    client = TestClient(server_mod.app)
    paired = _pair_fully(client)
    assert client.get("/mobile/v1/runs", headers=_auth(paired)).status_code == 200
    client.post(f"/settings/mobile-connector/devices/{paired['device_id']}/revoke",
                headers=_tauri())
    denied = client.get("/mobile/v1/runs", headers=_auth(paired))
    assert denied.status_code == 401 and denied.json()["detail"] == "mobile_device_revoked"


def test_connection_info_lists_hosts_for_paired_device() -> None:
    # F076 — the phone learns the desktop's current hosts to roam without re-pair.
    _enable_connector()
    client = TestClient(server_mod.app)
    paired = _pair_fully(client)
    info = client.get("/mobile/v1/connection-info", headers=_auth(paired))
    assert info.status_code == 200, info.text
    body = info.json()
    assert isinstance(body.get("hosts"), list)
    assert isinstance(body.get("port"), int)
    # Unauthenticated is rejected.
    assert client.get("/mobile/v1/connection-info").status_code == 401


def test_delete_device_removes_record_and_requires_tauri() -> None:
    _enable_connector()
    client = TestClient(server_mod.app)
    paired = _pair_fully(client)
    did = paired["device_id"]
    # Non-Tauri origin is refused.
    assert client.delete(f"/settings/mobile-connector/devices/{did}").status_code == 403
    # Deleting drops the record entirely (not just a revoked tombstone).
    r = client.delete(f"/settings/mobile-connector/devices/{did}", headers=_tauri())
    assert r.status_code == 200 and r.json()["device_id"] == did
    listing = client.get("/settings/mobile-connector", headers=_tauri()).json()
    assert all(d["device_id"] != did for d in listing["devices"])
    # The phone's token no longer works.
    assert client.get("/mobile/v1/runs", headers=_auth(paired)).status_code == 401
    # Deleting an unknown device is a 404.
    assert client.delete(f"/settings/mobile-connector/devices/{did}", headers=_tauri()).status_code == 404


def test_capability_grant_then_read() -> None:
    _enable_connector()
    client = TestClient(server_mod.app)
    paired = _pair_fully(client)
    # read_runs is granted by default; revoke it and confirm 403.
    client.patch(f"/settings/mobile-connector/devices/{paired['device_id']}",
                 json={"capabilities": {"read_runs": False}}, headers=_tauri())
    denied = client.get("/mobile/v1/runs", headers=_auth(paired))
    assert denied.status_code == 403
    assert denied.json()["detail"] == "mobile_capability_forbidden:read_runs"


def test_settings_device_list_public_only() -> None:
    _enable_connector()
    client = TestClient(server_mod.app)
    _pair_fully(client, public_key="visible-pk")
    body = client.get("/settings/mobile-connector").json()
    assert body["device_count"] == 1
    assert body["devices"][0]["public_key_fingerprint"]
    assert "public_key" not in body["devices"][0]
    assert "session_token_sha256" not in json.dumps(body)
    assert "visible-pk" not in json.dumps(body)
