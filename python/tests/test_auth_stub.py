"""F009-01 — real Service API pairing routes."""

from __future__ import annotations

import datetime as _dt
import re

import pytest
from fastapi.testclient import TestClient

from errorta_app.auth import audit, pairing, store
from errorta_app.server import app

TOKEN_RE = re.compile(r"^ert_[0-9a-f]{32}$")
OWNER_HEADERS = {"x-errorta-origin": "tauri-ui"}


@pytest.fixture
def client(tmp_errorta_home):
    store.reset_state_for_tests()
    pairing.reset_state_for_tests()
    return TestClient(app)


def _pair_body(**overrides) -> dict:
    body = {
        "app_slug": "demo-app",
        "app_name": "Demo App",
        "requested_corpora": ["welcome"],
        "requested_scopes": ["prompt", "meta"],
    }
    body.update(overrides)
    return body


def test_pair_returns_pending_session_without_minting_token(client):
    resp = client.post("/api/auth/pair", json=_pair_body())
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "pending"
    assert isinstance(data["session_id"], str) and data["session_id"]
    expires = _dt.datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
    delta = expires - _dt.datetime.now(_dt.timezone.utc)
    assert _dt.timedelta(minutes=4) < delta <= _dt.timedelta(minutes=5, seconds=5)
    assert store.load_tokens() == []


def test_pair_rejects_empty_app_slug(client):
    resp = client.post("/api/auth/pair", json=_pair_body(app_slug=""))
    assert resp.status_code == 400


def test_pair_rejects_unsupported_scope(client):
    resp = client.post(
        "/api/auth/pair",
        json=_pair_body(requested_scopes=["answer:read"]),
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "scope_unsupported"


def test_pair_errors_are_rate_limited(client):
    for _ in range(8):
        resp = client.post(
            "/api/auth/pair",
            json=_pair_body(requested_scopes=["answer:read"]),
        )
        assert resp.status_code == 400

    limited = client.post(
        "/api/auth/pair",
        json=_pair_body(requested_scopes=["answer:read"]),
    )
    assert limited.status_code == 429
    assert limited.json()["detail"] == "pairing_rate_limited"
    assert int(limited.headers["Retry-After"]) >= 1


def test_pair_pending_cap_failures_are_rate_limited(client):
    for idx in range(pairing.MAX_PENDING):
        resp = client.post("/api/auth/pair", json=_pair_body(app_slug=f"demo-{idx}"))
        assert resp.status_code == 200

    for _ in range(8):
        resp = client.post("/api/auth/pair", json=_pair_body(app_slug="overflow"))
        assert resp.status_code == 400
        assert resp.json()["detail"] == "pairing_too_many_pending"

    limited = client.post("/api/auth/pair", json=_pair_body(app_slug="overflow"))
    assert limited.status_code == 429
    assert limited.json()["detail"] == "pairing_rate_limited"
    assert int(limited.headers["Retry-After"]) >= 1


def test_pair_status_unknown_returns_404(client):
    resp = client.get("/api/auth/pair-status/does-not-exist")
    assert resp.status_code == 404


def test_pair_status_unknown_sessions_are_rate_limited(client):
    for _ in range(8):
        resp = client.get("/api/auth/pair-status/does-not-exist")
        assert resp.status_code == 404

    limited = client.get("/api/auth/pair-status/does-not-exist")
    assert limited.status_code == 429
    assert limited.json()["detail"] == "pairing_rate_limited"
    assert int(limited.headers["Retry-After"]) >= 1


def test_pair_status_stays_pending_until_owner_approves(client):
    pair = client.post("/api/auth/pair", json=_pair_body()).json()
    resp = client.get(f"/api/auth/pair-status/{pair['session_id']}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "pending"
    assert "token" not in resp.json()


def test_owner_approve_mints_and_delivers_token_once(client):
    pair = client.post("/api/auth/pair", json=_pair_body()).json()
    approve = client.post(
        f"/api/auth/pair/{pair['session_id']}/approve",
        json={"corpora": ["welcome"], "scopes": ["prompt", "meta"]},
        headers=OWNER_HEADERS,
    )
    assert approve.status_code == 200
    assert store.load_tokens()[0]["corpora"] == ["welcome"]

    first = client.get(f"/api/auth/pair-status/{pair['session_id']}").json()
    assert first["status"] == "accepted"
    assert TOKEN_RE.match(first["token"])
    assert first["corpora"] == ["welcome"]
    assert first["scopes"] == ["prompt", "meta"]

    second = client.get(f"/api/auth/pair-status/{pair['session_id']}").json()
    assert second["status"] == "consumed"
    assert "token" not in second
    events = audit.read_events()
    assert events[-1]["event"] == "pair.accepted"
    assert events[-1]["token_id"] == store.load_tokens()[0]["id"]
    assert first["token"] not in events[-1].values()


def test_accepted_token_delivery_expires_without_reissue(client):
    pair = client.post("/api/auth/pair", json=_pair_body()).json()
    approve = client.post(
        f"/api/auth/pair/{pair['session_id']}/approve",
        json={},
        headers=OWNER_HEADERS,
    )
    assert approve.status_code == 200
    assert len(store.load_tokens()) == 1
    sessions = pairing.load_sessions()
    sessions[0]["delivery_expires_at"] = (
        _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=1)
    ).isoformat(timespec="seconds").replace("+00:00", "Z")
    pairing.save_sessions(sessions)

    first = client.get(f"/api/auth/pair-status/{pair['session_id']}").json()
    assert first["status"] == "accepted"
    assert first["token_lost"] is True
    assert "token" not in first
    assert len(store.load_tokens()) == 1
    stored = pairing.load_sessions()[0]
    assert stored["state"] == "consumed"
    assert stored["token_delivery_state"] == "lost"

    second = client.get(f"/api/auth/pair-status/{pair['session_id']}").json()
    assert second["status"] == "consumed"
    assert "token" not in second


def test_owner_routes_require_tauri_origin(client):
    pair = client.post("/api/auth/pair", json=_pair_body()).json()
    resp = client.post(f"/api/auth/pair/{pair['session_id']}/approve", json={})
    assert resp.status_code == 403
    assert resp.json()["detail"] == "tauri origin required"


def test_owner_can_deny_pairing(client):
    pair = client.post("/api/auth/pair", json=_pair_body()).json()
    resp = client.post(f"/api/auth/pair/{pair['session_id']}/deny", headers=OWNER_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["pairing"]["status"] == "denied"
    assert client.get(f"/api/auth/pair-status/{pair['session_id']}").json()["status"] == "denied"


def test_expired_session_returns_expired(client):
    pair = client.post("/api/auth/pair", json=_pair_body()).json()
    sessions = pairing.load_sessions()
    sessions[0]["expires_at"] = (
        _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=1)
    ).isoformat(timespec="seconds").replace("+00:00", "Z")
    pairing.save_sessions(sessions)
    resp = client.get(f"/api/auth/pair-status/{pair['session_id']}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "expired"


def test_restart_after_approve_reports_lost_token_without_double_issue(client):
    pair = client.post("/api/auth/pair", json=_pair_body()).json()
    client.post(
        f"/api/auth/pair/{pair['session_id']}/approve",
        json={},
        headers=OWNER_HEADERS,
    )
    assert len(store.load_tokens()) == 1
    pairing.reset_state_for_tests()

    resp = client.get(f"/api/auth/pair-status/{pair['session_id']}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "accepted"
    assert data["token_lost"] is True
    assert "token" not in data
    assert len(store.load_tokens()) == 1


def test_tokens_list_requires_owner_header(client):
    resp = client.get("/api/auth/tokens")
    assert resp.status_code == 403


def test_owner_can_list_and_revoke_tokens_without_raw_secret(client):
    pair = client.post("/api/auth/pair", json=_pair_body()).json()
    client.post(
        f"/api/auth/pair/{pair['session_id']}/approve",
        json={},
        headers=OWNER_HEADERS,
    )
    first = client.get(f"/api/auth/pair-status/{pair['session_id']}").json()
    raw_token = first["token"]
    token_id = store.load_tokens()[0]["id"]

    listing = client.get("/api/auth/tokens", headers=OWNER_HEADERS)
    assert listing.status_code == 200
    assert listing.json()["tokens"][0]["id"] == token_id
    assert "token_sha256" not in listing.text
    assert raw_token not in listing.text

    revoke = client.delete(f"/api/auth/tokens/{token_id}", headers=OWNER_HEADERS)
    assert revoke.status_code == 200
    assert revoke.json() == {"id": token_id, "status": "revoked"}
    assert client.get("/api/auth/tokens", headers=OWNER_HEADERS).json()["tokens"] == []

    audit_text = "\n".join(str(item) for item in audit.read_events())
    assert "token.revoked" in audit_text
    assert token_id in audit_text
    assert raw_token not in audit_text
