"""Invariant 5: the judge answering surface is locked server-side.

When the alpha gate is on and the app is locked, POST /judge/verdict must
return 403 alpha_locked *before* any pipeline work. With the gate off it is a
no-op.
"""
from __future__ import annotations

import time

import pytest
from fastapi import HTTPException

from errorta_alpha import device
from errorta_alpha import license as license_store
from errorta_alpha.license import LicenseRecord
from errorta_app.routes import coding as coding_routes
from errorta_app.routes import council as council_routes
from errorta_app.routes import judge as judge_routes


def test_verdict_blocked_when_unactivated(alpha_home, alpha_keys):
    # gate on (alpha_home), no license -> UNACTIVATED -> locked.
    req = judge_routes.VerdictRequest(prompt="hi", corpus="welcome")
    with pytest.raises(HTTPException) as ei:
        judge_routes.run_verdict(req)
    assert ei.value.status_code == 403
    assert ei.value.detail["error"] == "alpha_locked"
    assert ei.value.detail["state"] == "unactivated"


def test_verdict_blocked_when_revoked(alpha_home, alpha_keys):
    now = int(time.time())
    did = device.get_or_create_device_id()
    tok = alpha_keys.mint(device_id=did, grace_until=now + 5 * 86400)
    license_store.store(LicenseRecord(
        token=tok, grace_until=now + 5 * 86400, status="revoked",
        revoke_reason="left program",
    ))
    req = judge_routes.VerdictRequest(prompt="hi", corpus="welcome")
    with pytest.raises(HTTPException) as ei:
        judge_routes.run_verdict(req)
    assert ei.value.status_code == 403
    assert ei.value.detail["state"] == "revoked"


def test_enforce_is_noop_when_gate_off(tmp_path, monkeypatch):
    # Gate off (production posture): the lock check must never raise, regardless
    # of any license state on disk.
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    monkeypatch.delenv("ERRORTA_ALPHA_GATE", raising=False)
    from errorta_alpha.state import enforce_not_locked
    enforce_not_locked()  # no exception


def test_council_run_start_is_locked_before_store_access(alpha_home, alpha_keys):
    body = council_routes._CreateRun(room_id="missing", prompt="hi")
    with pytest.raises(HTTPException) as ei:
        import asyncio
        asyncio.run(council_routes.create_run(body))
    assert ei.value.status_code == 403
    assert ei.value.detail["error"] == "alpha_locked"


def test_coding_run_start_is_locked_before_ledger_access(alpha_home, alpha_keys):
    with pytest.raises(HTTPException) as ei:
        coding_routes._start_run("missing", {}, resume=False)
    assert ei.value.status_code == 403
    assert ei.value.detail["error"] == "alpha_locked"
