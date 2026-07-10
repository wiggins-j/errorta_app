"""The offline grace / lock state machine (spec §8) + license record I/O."""
from __future__ import annotations

import stat
import time

from errorta_alpha import device
from errorta_alpha import license as license_store
from errorta_alpha import state as alpha_state
from errorta_alpha.license import LicenseRecord
from errorta_alpha.state import AlphaState
from errorta_app.paths import alpha_license_path


def _activate(alpha_keys, *, grace_until, status="active", now=None, **kw):
    """Create a device + stored license signed by the test key."""
    did = device.get_or_create_device_id()
    tok = alpha_keys.mint(device_id=did, grace_until=grace_until)
    rec = LicenseRecord(
        token=tok, grace_until=grace_until, status=status,
        last_heartbeat=now, max_seen_epoch=now or 0, **kw,
    )
    license_store.store(rec)
    return did


def test_gate_off_is_disabled_and_unlocked(alpha_home, alpha_keys, monkeypatch):
    monkeypatch.delenv("ERRORTA_ALPHA_GATE", raising=False)
    st = alpha_state.current_status()
    assert st.state is AlphaState.DISABLED
    assert st.locked is False


def test_no_device_or_license_is_unactivated_locked(alpha_home, alpha_keys):
    st = alpha_state.current_status()
    assert st.state is AlphaState.UNACTIVATED
    assert st.locked is True
    assert st.reason == "not_activated"


def test_valid_token_in_grace_is_active_unlocked(alpha_home, alpha_keys):
    now = int(time.time())
    _activate(alpha_keys, grace_until=now + 10 * 86400, now=now)
    st = alpha_state.current_status(now=now)
    assert st.state is AlphaState.ACTIVE
    assert st.locked is False


def test_offline_past_grace_is_expired_locked(alpha_home, alpha_keys):
    now = int(time.time())
    _activate(alpha_keys, grace_until=now - 100, now=now - 20 * 86400)
    st = alpha_state.current_status(now=now)
    assert st.state is AlphaState.EXPIRED
    assert st.locked is True
    assert st.reason == "grace_expired"


def test_revoked_status_is_locked(alpha_home, alpha_keys):
    now = int(time.time())
    _activate(alpha_keys, grace_until=now + 10 * 86400, status="revoked",
              now=now, revoke_reason="left program")
    st = alpha_state.current_status(now=now)
    assert st.state is AlphaState.REVOKED
    assert st.locked is True
    assert st.reason == "left program"


def test_invalid_token_falls_back_to_unactivated(alpha_home, alpha_keys):
    device.get_or_create_device_id()
    now = int(time.time())
    # A token signed by a different key won't verify against ERRORTA_ALPHA_PUBKEY.
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from errorta_alpha import token as token_mod
    bogus = token_mod.encode({"grace_until": now + 999}, Ed25519PrivateKey.generate())
    license_store.store(LicenseRecord(token=bogus, grace_until=now + 999))
    st = alpha_state.current_status(now=now)
    assert st.state is AlphaState.UNACTIVATED
    assert st.reason == "invalid_token"


def test_token_for_another_device_cannot_unlock(alpha_home, alpha_keys):
    device.get_or_create_device_id()
    now = int(time.time())
    tok = alpha_keys.mint(device_id="00000000-0000-4000-8000-000000000001",
                          grace_until=now + 999)
    license_store.store(LicenseRecord(token=tok, grace_until=now + 999))
    st = alpha_state.current_status(now=now)
    assert st.state is AlphaState.UNACTIVATED
    assert st.reason == "invalid_token_claims"


def test_unsigned_record_cannot_extend_signed_grace(alpha_home, alpha_keys):
    signed_grace = 1_000_000
    did = device.get_or_create_device_id()
    tok = alpha_keys.mint(device_id=did, grace_until=signed_grace)
    license_store.store(LicenseRecord(token=tok, grace_until=signed_grace + 999_999))
    st = alpha_state.current_status(now=signed_grace + 1)
    assert st.state is AlphaState.EXPIRED
    assert st.grace_until == signed_grace


def test_clock_rollback_cannot_extend_grace(alpha_home, alpha_keys):
    grace_until = 1_000_000
    # We successfully checked in later than grace_until (max_seen_epoch beyond it),
    # then the local clock is rolled *back* to before grace_until.
    device.get_or_create_device_id()
    did = device.read_device_id()
    tok = alpha_keys.mint(device_id=did, grace_until=grace_until)
    license_store.store(LicenseRecord(
        token=tok, grace_until=grace_until, max_seen_epoch=grace_until + 100,
    ))
    rolled_back_now = grace_until - 50  # naive check would call this ACTIVE
    st = alpha_state.current_status(now=rolled_back_now)
    assert st.state is AlphaState.EXPIRED, "rolling the clock back must not un-expire"


def test_build_eol_required_locks_answering(alpha_home, alpha_keys):
    now = int(time.time())
    _activate(alpha_keys, grace_until=now + 10 * 86400, now=now,
              build_eol_required=True, update_url="https://errorta.app/dl")
    st = alpha_state.current_status(now=now)
    assert st.state is AlphaState.ACTIVE
    assert st.locked is True
    assert st.reason == "build_eol"
    assert st.update_url == "https://errorta.app/dl"


def test_license_file_is_owner_only(alpha_home, alpha_keys):
    now = int(time.time())
    _activate(alpha_keys, grace_until=now + 10 * 86400, now=now)
    mode = stat.S_IMODE(alpha_license_path().stat().st_mode)
    assert mode == 0o600


def test_corrupt_license_degrades_to_unactivated(alpha_home, alpha_keys):
    device.get_or_create_device_id()
    alpha_license_path().write_text("{garbage", encoding="utf-8")
    assert license_store.load() is None
    st = alpha_state.current_status()
    assert st.state is AlphaState.UNACTIVATED


def test_record_rejects_bool_as_int_grace():
    # bool is an int subclass; a stray True must not become grace_until=1.
    assert LicenseRecord.from_dict({"token": "x.y", "grace_until": True}) is None
