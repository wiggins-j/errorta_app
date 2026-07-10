"""The offline grace / lock state machine (spec §8).

Pure evaluation over the persisted device + license records plus the current
clock. This is the single authority the answering routes consult via
``enforce_not_locked``.

States:
  disabled     — the build's alpha gate is off (production posture); never locks.
  unactivated  — no device/token yet, or the token failed verification. Locked
                 (must activate) but Settings/export/feedback stay reachable.
  active       — token valid and within grace. Unlocked.
  expired      — offline past ``grace_until`` (clock-rollback-guarded). Locked,
                 recoverable by one successful heartbeat.
  revoked      — server said ``revoked``. Locked.

``build_eol_required`` rides on top of ``active`` and locks answering with a
distinct reason (a soft, non-required EOL does not lock).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum

from fastapi import HTTPException

from . import config, device
from . import license as license_store
from . import token as token_mod


class AlphaState(str, Enum):
    DISABLED = "disabled"
    UNACTIVATED = "unactivated"
    ACTIVE = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"


@dataclass
class AlphaStatus:
    state: AlphaState
    locked: bool
    reason: str | None = None
    grace_until: int | None = None
    device_id: str | None = None
    build_eol: bool = False
    build_eol_required: bool = False
    update_url: str | None = None

    def to_dict(self) -> dict:
        return {
            "gate_enabled": self.state is not AlphaState.DISABLED,
            "state": self.state.value,
            "locked": self.locked,
            "reason": self.reason,
            "grace_until": self.grace_until,
            "device_id": self.device_id,
            "build_eol": self.build_eol,
            "build_eol_required": self.build_eol_required,
            "update_url": self.update_url,
        }


def current_status(now: int | None = None) -> AlphaStatus:
    """Evaluate the current alpha state. ``now`` defaults to the system clock."""
    if not config.gate_enabled():
        return AlphaStatus(state=AlphaState.DISABLED, locked=False)

    now = int(time.time()) if now is None else int(now)
    device_id = device.read_device_id()
    record = license_store.load()

    # No identity or no license yet -> must activate.
    if device_id is None or record is None:
        return AlphaStatus(
            state=AlphaState.UNACTIVATED, locked=True, reason="not_activated",
            device_id=device_id,
        )

    # A tampered/forged token can never unlock the app.
    payload = token_mod.verify(record.token, config.license_public_key_raw())
    if payload is None:
        return AlphaStatus(
            state=AlphaState.UNACTIVATED, locked=True, reason="invalid_token",
            device_id=device_id,
        )

    signed_device_id = payload.get("device_id")
    signed_grace_until = payload.get("grace_until")
    if (
        payload.get("v") != 1
        or signed_device_id != device_id
        or isinstance(signed_grace_until, bool)
        or not isinstance(signed_grace_until, int)
    ):
        return AlphaStatus(
            state=AlphaState.UNACTIVATED, locked=True, reason="invalid_token_claims",
            device_id=device_id,
        )

    if record.status == "revoked":
        return AlphaStatus(
            state=AlphaState.REVOKED, locked=True,
            reason=record.revoke_reason or "revoked",
            grace_until=record.grace_until, device_id=device_id,
        )

    # Clock-rollback guard: never let a backwards clock grant extra grace.
    effective_now = max(now, record.max_seen_epoch)
    if effective_now > signed_grace_until:
        return AlphaStatus(
            state=AlphaState.EXPIRED, locked=True, reason="grace_expired",
            grace_until=signed_grace_until, device_id=device_id,
        )

    # Within grace: active. A *required* build-EOL still locks answering.
    if record.build_eol_required:
        return AlphaStatus(
            state=AlphaState.ACTIVE, locked=True, reason="build_eol",
            grace_until=signed_grace_until, device_id=device_id,
            build_eol=True, build_eol_required=True, update_url=record.update_url,
        )

    # A *soft* build-EOL leaves the app fully usable but surfaces a non-blocking
    # "update available" banner in the shell.
    return AlphaStatus(
        state=AlphaState.ACTIVE, locked=False,
        grace_until=signed_grace_until, device_id=device_id,
        build_eol=record.build_eol, update_url=record.update_url,
    )


def is_locked(now: int | None = None) -> bool:
    return current_status(now).locked


def enforce_not_locked() -> None:
    """Raise ``403 alpha_locked`` if the alpha gate is on and the app is locked.

    Called by the answering surfaces (judge/council/coding run start) so the lock
    is enforced server-side, not just in the UI (invariant 5). A no-op when the
    gate is off.
    """
    status = current_status()
    if status.locked:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "alpha_locked",
                "state": status.state.value,
                "reason": status.reason,
            },
        )
