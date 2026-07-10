"""Check-in client — activate/heartbeat over a mocked transport.

Every failure path (offline, 5xx, transient 404) must be a state-preserving
no-op (invariant 4). The real network is never touched.
"""
from __future__ import annotations

import time

import pytest

from errorta_alpha import client as alpha_client
from errorta_alpha import device
from errorta_alpha import license as license_store
from errorta_alpha.license import LicenseRecord


@pytest.fixture
def canned(monkeypatch):
    """Install a scripted (status_code, json) response for the next _post_json."""
    box = {"resp": (200, {}), "calls": []}

    def fake_post(path, body):
        box["calls"].append((path, body))
        resp = box["resp"]
        if isinstance(resp, Exception):
            raise resp
        return resp

    monkeypatch.setattr(alpha_client, "_post_json", fake_post)
    return box


def _seed_active_license(alpha_keys, *, grace_until, last_heartbeat, max_seen=0):
    did = device.get_or_create_device_id()
    tok = alpha_keys.mint(device_id=did, grace_until=grace_until)
    license_store.store(LicenseRecord(
        token=tok, grace_until=grace_until, last_heartbeat=last_heartbeat,
        max_seen_epoch=max_seen,
    ))
    return did


def test_activate_success_persists_license(alpha_home, alpha_keys, canned):
    now = int(time.time())
    did = device.get_or_create_device_id()
    tok = alpha_keys.mint(device_id=did, grace_until=now + 14 * 86400)
    canned["resp"] = (200, {"status": "active", "token": tok, "grace_days": 14})

    res = alpha_client.activate("ERRT-TEST-0001", now=now)

    assert res.ok is True
    rec = license_store.load()
    assert rec is not None and rec.status == "active"
    assert rec.grace_until == now + 14 * 86400  # taken from the signed token
    assert rec.max_seen_epoch == now
    assert canned["calls"][0][0] == "/v1/activate"


def test_activate_rejected_code_surfaces_error(alpha_home, alpha_keys, canned):
    canned["resp"] = (409, {"error": "code_exhausted"})
    res = alpha_client.activate("ERRT-TEST-0001")
    assert res.ok is False
    assert res.error_code == "code_exhausted"
    assert license_store.load() is None


def test_activate_offline_is_soft_failure(alpha_home, alpha_keys, canned):
    canned["resp"] = RuntimeError("connection refused")
    res = alpha_client.activate("ERRT-TEST-0001")
    assert res.ok is False
    assert res.error_code == "offline"


def test_heartbeat_active_refreshes_grace_and_highwater(alpha_home, alpha_keys, canned):
    now = int(time.time())
    _seed_active_license(alpha_keys, grace_until=now, last_heartbeat=now - 7200)
    did = device.read_device_id()
    new_tok = alpha_keys.mint(device_id=did, grace_until=now + 14 * 86400)
    canned["resp"] = (200, {"status": "active", "token": new_tok, "grace_days": 14})

    out = alpha_client.heartbeat({"launches": 2}, now=now)

    rec = license_store.load()
    assert out.kind == "active" and out.changed
    assert rec.grace_until == now + 14 * 86400
    assert rec.max_seen_epoch == now
    assert rec.last_heartbeat == now


def test_heartbeat_revoked_sets_status(alpha_home, alpha_keys, canned):
    now = int(time.time())
    _seed_active_license(alpha_keys, grace_until=now + 5 * 86400, last_heartbeat=now - 7200)
    canned["resp"] = (200, {"status": "revoked", "reason": "left program"})

    out = alpha_client.heartbeat(now=now)

    assert out.kind == "revoked"
    rec = license_store.load()
    assert rec.status == "revoked" and rec.revoke_reason == "left program"


def test_heartbeat_build_eol_sets_flag(alpha_home, alpha_keys, canned):
    now = int(time.time())
    _seed_active_license(alpha_keys, grace_until=now + 5 * 86400, last_heartbeat=now - 7200)
    canned["resp"] = (200, {"status": "build_eol", "required": True,
                            "update_url": "https://errorta.app/dl"})

    out = alpha_client.heartbeat(now=now)

    assert out.kind == "build_eol"
    rec = license_store.load()
    assert rec.build_eol_required is True
    assert rec.update_url == "https://errorta.app/dl"


def test_heartbeat_404_does_not_mutate_license(alpha_home, alpha_keys, canned):
    now = int(time.time())
    _seed_active_license(alpha_keys, grace_until=now + 5 * 86400, last_heartbeat=now - 7200)
    before = license_store.load().to_dict()
    canned["resp"] = (404, {})

    out = alpha_client.heartbeat(now=now)

    assert out.kind == "unknown_device"
    assert license_store.load().to_dict() == before  # untouched (invariant 4)


def test_heartbeat_offline_does_not_mutate_license(alpha_home, alpha_keys, canned):
    now = int(time.time())
    _seed_active_license(alpha_keys, grace_until=now + 5 * 86400, last_heartbeat=now - 7200)
    before = license_store.load().to_dict()
    canned["resp"] = RuntimeError("timeout")

    out = alpha_client.heartbeat(now=now)

    assert out.kind == "offline"
    assert license_store.load().to_dict() == before


def test_heartbeat_deduped_within_interval(alpha_home, alpha_keys, canned):
    now = int(time.time())
    _seed_active_license(alpha_keys, grace_until=now + 5 * 86400, last_heartbeat=now - 60)
    out = alpha_client.heartbeat(now=now)
    assert out.kind == "active" and out.changed is False
    assert canned["calls"] == []  # never hit the network


def test_platform_tag_has_no_machine_identifiers(alpha_home):
    tag = alpha_client.platform_tag()
    assert "-" in tag and tag == tag.lower()
