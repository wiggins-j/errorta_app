"""The SOLE network egress to ``api.errorta.app`` (invariant 1).

No other sidecar module makes this call. ``errorta_council`` never imports this
package (locked by ``tests/alpha/test_no_egress_guard.py``). Every failure mode
(offline, timeout, 5xx, transient 404) is turned into a no-op that preserves the
local license record, so losing the network never bricks a tester (invariant 4).
"""
from __future__ import annotations

import logging
import platform as _platform
import sys
import time
from dataclasses import dataclass
from typing import Any

from errorta_app import __version__ as _app_version

from . import config, device, telemetry
from . import license as license_store
from . import token as token_mod
from .license import LicenseRecord

log = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 8.0
_CLIENT_HEADER = {"X-Errorta-Client": "errorta-desktop"}
# Skip a launch heartbeat if the last successful one was within this window,
# so rapid relaunches don't spam the endpoint (spec §6.2).
_HEARTBEAT_MIN_INTERVAL = 3600


def platform_tag() -> str:
    """A coarse ``os-arch`` tag, e.g. ``macos-arm64`` — no machine identifiers."""
    os_name = {"darwin": "macos", "win32": "windows", "linux": "linux"}.get(
        sys.platform, sys.platform
    )
    arch = (_platform.machine() or "").lower() or "unknown"
    return f"{os_name}-{arch}"


def app_version() -> str:
    return _app_version


# ---- transport (monkeypatched in tests) -------------------------------------

def _post_json(path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """POST JSON to ``{api_base}{path}`` and return ``(status_code, json)``.

    Raises on transport failure (caller wraps into a no-op). Split out so tests
    can monkeypatch it without a live network.
    """
    import httpx

    url = f"{config.api_base_url()}{path}"
    resp = httpx.post(url, json=body, headers=_CLIENT_HEADER, timeout=_TIMEOUT_SECONDS)
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001 — a non-JSON body is treated as empty
        data = {}
    return resp.status_code, (data if isinstance(data, dict) else {})


def _post_multipart(
    path: str, fields: dict[str, str], files: dict[str, tuple[str, bytes, str]] | None
) -> tuple[int, dict[str, Any]]:
    """POST multipart/form-data (for /v1/feedback). Split out for test mocking."""
    import httpx

    url = f"{config.api_base_url()}{path}"
    resp = httpx.post(
        url, data=fields, files=files, headers=_CLIENT_HEADER, timeout=_TIMEOUT_SECONDS
    )
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        data = {}
    return resp.status_code, (data if isinstance(data, dict) else {})


# ---- results ----------------------------------------------------------------

@dataclass
class ActivationResult:
    ok: bool
    error_code: str | None = None
    message: str | None = None


@dataclass
class FeedbackResult:
    ok: bool
    ticket_id: str | None = None
    error: str | None = None


@dataclass
class HeartbeatOutcome:
    # active | revoked | build_eol | unknown_device | offline
    kind: str
    changed: bool = False


# ---- grace helpers ----------------------------------------------------------

def _grace_until_from(token: str, grace_days: Any, now: int) -> int:
    """Prefer the server-signed ``grace_until`` in the token; fall back to
    ``now + grace_days*86400`` if the token can't be read with our key."""
    try:
        payload = token_mod.verify(token, config.license_public_key_raw())
    except Exception:  # noqa: BLE001
        payload = None
    if payload and isinstance(payload.get("grace_until"), int):
        return int(payload["grace_until"])
    try:
        days = int(grace_days)
    except (TypeError, ValueError):
        days = config.GRACE_DAYS
    return now + days * 86400


# ---- operations -------------------------------------------------------------

def activate(code: str, *, now: int | None = None) -> ActivationResult:
    """Redeem an invite code and persist the license on success."""
    now = int(time.time()) if now is None else int(now)
    device_id = device.get_or_create_device_id()
    body = {
        "code": code.strip(),
        "device_id": device_id,
        "platform": platform_tag(),
        "app_version": app_version(),
    }
    try:
        status_code, data = _post_json("/v1/activate", body)
    except Exception as exc:  # noqa: BLE001 — offline / transport failure
        log.info("alpha: activate transport error: %s", exc)
        return ActivationResult(ok=False, error_code="offline", message=str(exc))

    if status_code == 200 and data.get("status") == "active" and data.get("token"):
        token = str(data["token"])
        record = LicenseRecord(
            token=token,
            grace_until=_grace_until_from(token, data.get("grace_days"), now),
            status="active",
            code=code.strip(),
            last_heartbeat=now,
            max_seen_epoch=now,
        )
        license_store.store(record)
        return ActivationResult(ok=True, message=data.get("message"))

    return ActivationResult(
        ok=False,
        error_code=str(data.get("error") or data.get("detail") or f"http_{status_code}"),
        message=data.get("message"),
    )


def _should_skip_heartbeat(record: LicenseRecord, now: int) -> bool:
    return (
        record.last_heartbeat is not None
        and now - record.last_heartbeat < _HEARTBEAT_MIN_INTERVAL
    )


def heartbeat(
    floor: dict[str, int] | None = None,
    *,
    now: int | None = None,
    force: bool = False,
) -> HeartbeatOutcome:
    """Best-effort check-in. Updates the local license record from the server
    response; every failure path is a state-preserving no-op."""
    now = int(time.time()) if now is None else int(now)
    device_id = device.read_device_id()
    record = license_store.load()
    if device_id is None or record is None:
        return HeartbeatOutcome(kind="offline")  # nothing to check in yet
    if not force and _should_skip_heartbeat(record, now):
        return HeartbeatOutcome(kind="active")  # too soon; treat as unchanged

    body = {
        "device_id": device_id,
        "app_version": app_version(),
        "platform": platform_tag(),
        "floor": floor or {},
    }
    try:
        status_code, data = _post_json("/v1/heartbeat", body)
    except Exception as exc:  # noqa: BLE001 — offline: preserve state
        log.info("alpha: heartbeat transport error: %s", exc)
        return HeartbeatOutcome(kind="offline")

    # Unknown device (server reset / row dropped). Do NOT lock — a transient
    # 404 within grace must never destroy a valid local seat (invariant 4).
    if status_code == 404:
        return HeartbeatOutcome(kind="unknown_device")
    if status_code != 200:
        return HeartbeatOutcome(kind="offline")

    server_status = data.get("status")

    if server_status == "revoked":
        record.status = "revoked"
        record.revoke_reason = str(data.get("reason") or "revoked")
        record.last_heartbeat = now
        record.max_seen_epoch = max(record.max_seen_epoch, now)
        license_store.store(record)
        return HeartbeatOutcome(kind="revoked", changed=True)

    if server_status == "build_eol":
        # `required` locks answering; otherwise it's a soft "update available"
        # nudge that leaves the app usable but shows a banner.
        record.build_eol = True
        record.build_eol_required = bool(data.get("required"))
        update_url = data.get("update_url")
        record.update_url = update_url if isinstance(update_url, str) else None
        record.last_heartbeat = now
        record.max_seen_epoch = max(record.max_seen_epoch, now)
        # A build_eol response keeps the existing token; refresh grace if one
        # was included.
        if data.get("token"):
            record.token = str(data["token"])
            record.grace_until = _grace_until_from(record.token, data.get("grace_days"), now)
        license_store.store(record)
        return HeartbeatOutcome(kind="build_eol", changed=True)

    # Default: active — refresh token + grace, advance the clock high-water mark.
    # A clean active response clears any prior EOL signal (build recovered).
    if data.get("token"):
        record.token = str(data["token"])
    record.grace_until = _grace_until_from(record.token, data.get("grace_days"), now)
    record.status = "active"
    record.build_eol = False
    record.build_eol_required = False
    record.update_url = None
    record.revoke_reason = None
    record.last_heartbeat = now
    record.max_seen_epoch = max(record.max_seen_epoch, now)
    license_store.store(record)
    return HeartbeatOutcome(kind="active", changed=True)


def send_metrics(*, now: int | None = None) -> str:
    """Drain the Tier-2 extras queue to /v1/metrics when extras are enabled.

    Returns an outcome tag: ``disabled`` / ``empty`` / ``offline`` / ``sent``.
    Only clears the queue prefix that was actually accepted (202), so an offline
    or 5xx attempt loses nothing.
    """
    del now  # metrics carry no server-authoritative timestamp
    if not telemetry.extras_enabled():
        return "disabled"
    device_id = device.read_device_id()
    if device_id is None:
        return "offline"
    events = telemetry.snapshot_queue()
    if not events:
        return "empty"
    body = {
        "device_id": device_id,
        "app_version": app_version(),
        "platform": platform_tag(),
        "events": events,
    }
    try:
        status_code, _ = _post_json("/v1/metrics", body)
    except Exception as exc:  # noqa: BLE001 — offline: keep the queue
        log.info("alpha: metrics transport error: %s", exc)
        return "offline"
    if status_code == 202:
        telemetry.drop_queue_prefix(len(events))
        return "sent"
    return "offline"


def sync(*, now: int | None = None) -> HeartbeatOutcome:
    """One full check-in: heartbeat carrying the floor deltas, then drain extras.

    The floor delta is snapshotted, sent on the heartbeat, and cleared only when
    the heartbeat reached the server (a 200 → ``changed``). Extras follow via
    ``send_metrics`` when enabled. This is the single entry point the background
    scheduler and any manual sync call use.
    """
    now = int(time.time()) if now is None else int(now)
    floor = telemetry.snapshot_floor()
    outcome = heartbeat(floor, now=now)
    if outcome.changed:
        telemetry.clear_floor(floor)
    send_metrics(now=now)
    return outcome


def send_feedback(
    *, kind: str, message: str, bundle_path: str | None = None
) -> FeedbackResult:
    """POST a feedback ticket (+ optional redacted bundle) to /v1/feedback.

    An explicit, user-initiated action — works with or without a device id (the
    service accepts anonymous reports, so a locked/unactivated tester can still
    reach us). The bundle bytes are the F-INFRA-06 *redacted* tarball the tester
    already previewed.
    """
    from pathlib import Path as _Path

    fields = {"kind": kind, "message": message, "app_version": app_version()}
    device_id = device.read_device_id()
    if device_id:
        fields["device_id"] = device_id
    files: dict[str, tuple[str, bytes, str]] | None = None
    if bundle_path:
        p = _Path(bundle_path)
        if p.is_file():
            files = {"bundle": (p.name, p.read_bytes(), "application/zip")}
    try:
        status_code, data = _post_multipart("/v1/feedback", fields, files)
    except Exception as exc:  # noqa: BLE001 — offline / transport failure
        log.info("alpha: feedback transport error: %s", exc)
        return FeedbackResult(ok=False, error="offline")
    if status_code == 201 and data.get("ticket_id"):
        return FeedbackResult(ok=True, ticket_id=str(data["ticket_id"]))
    return FeedbackResult(ok=False, error=str(data.get("error") or f"http_{status_code}"))
