"""Slice 8 — soft vs required build-EOL (retire stale builds)."""
from __future__ import annotations

import time

import pytest

from errorta_alpha import client as alpha_client
from errorta_alpha import device
from errorta_alpha import license as license_store
from errorta_alpha import state as alpha_state
from errorta_alpha.license import LicenseRecord
from errorta_alpha.state import AlphaState


def _seed(alpha_keys, *, now, **kw):
    did = device.get_or_create_device_id()
    grace = now + 10 * 86400
    tok = alpha_keys.mint(device_id=did, grace_until=grace)
    license_store.store(LicenseRecord(token=tok, grace_until=grace, max_seen_epoch=now, **kw))
    return did


def test_soft_eol_is_active_unlocked_with_banner_signal(alpha_home, alpha_keys):
    now = int(time.time())
    _seed(alpha_keys, now=now, build_eol=True, build_eol_required=False,
          update_url="https://errorta.app/dl")
    st = alpha_state.current_status(now=now)
    assert st.state is AlphaState.ACTIVE
    assert st.locked is False
    assert st.build_eol is True
    assert st.build_eol_required is False
    assert st.update_url == "https://errorta.app/dl"


def test_required_eol_locks(alpha_home, alpha_keys):
    now = int(time.time())
    _seed(alpha_keys, now=now, build_eol=True, build_eol_required=True,
          update_url="https://errorta.app/dl")
    st = alpha_state.current_status(now=now)
    assert st.locked is True
    assert st.reason == "build_eol"
    assert st.build_eol is True


@pytest.fixture
def canned(monkeypatch, alpha_keys, alpha_home):
    now = int(time.time())
    did = device.get_or_create_device_id()
    tok = alpha_keys.mint(device_id=did, grace_until=now + 10 * 86400)
    license_store.store(LicenseRecord(token=tok, grace_until=now + 10 * 86400,
                                      last_heartbeat=now - 7200, max_seen_epoch=now))
    box = {"resp": (200, {})}
    monkeypatch.setattr(alpha_client, "_post_json", lambda p, b: box["resp"])
    return box, now, tok, did


def test_heartbeat_soft_eol_sets_flag_not_required(canned):
    box, now, tok, did = canned
    box["resp"] = (200, {"status": "build_eol", "required": False, "update_url": "https://x/dl"})
    out = alpha_client.heartbeat(now=now)
    assert out.kind == "build_eol"
    rec = license_store.load()
    assert rec.build_eol is True and rec.build_eol_required is False


def test_heartbeat_required_eol_sets_both(canned):
    box, now, tok, did = canned
    box["resp"] = (200, {"status": "build_eol", "required": True, "update_url": "https://x/dl"})
    alpha_client.heartbeat(now=now)
    rec = license_store.load()
    assert rec.build_eol is True and rec.build_eol_required is True


def test_clean_active_heartbeat_clears_eol(canned, alpha_keys):
    box, now, tok, did = canned
    # First a soft EOL...
    box["resp"] = (200, {"status": "build_eol", "required": False, "update_url": "https://x/dl"})
    alpha_client.heartbeat(now=now, force=True)
    assert license_store.load().build_eol is True
    # ...then the build recovers: a clean active heartbeat clears the EOL + url.
    fresh = alpha_keys.mint(device_id=did, grace_until=now + 14 * 86400)
    box["resp"] = (200, {"status": "active", "token": fresh, "grace_days": 14})
    alpha_client.heartbeat(now=now, force=True)
    rec = license_store.load()
    assert rec.build_eol is False and rec.build_eol_required is False
    assert rec.update_url is None
