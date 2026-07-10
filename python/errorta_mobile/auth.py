"""Authentication guards for mobile connector routes."""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request

from . import config as mobile_config
from . import devices


def require_enabled() -> dict[str, Any]:
    cfg = mobile_config.load()
    if not cfg.get("enabled") or cfg.get("bind_mode") == "disabled":
        raise HTTPException(status_code=503, detail="mobile_connector_disabled")
    return cfg


def require_paired_device(request: Request) -> dict[str, Any]:
    """Validate a paired device session.

    MVP F057 uses a bearer session token returned at pairing completion.
    The device record also stores a public key fingerprint so a later signed
    nonce flow can replace bearer sessions without changing route handlers.
    """
    require_enabled()
    device_id = request.headers.get("x-errorta-mobile-device-id", "").strip()
    session_token = _session_token(request)
    if not device_id or not session_token:
        raise HTTPException(status_code=401, detail="mobile_device_auth_required")
    try:
        return devices.authenticate(device_id, session_token)
    except devices.DeviceAuthError as exc:
        raise HTTPException(status_code=401, detail=exc.code) from exc


def require_capability(request: Request, capability: str) -> dict[str, Any]:
    record = require_paired_device(request)
    try:
        devices.require_capability(record, capability)
    except devices.DeviceAuthError as exc:
        raise HTTPException(status_code=403, detail=exc.code) from exc
    return record


def _session_token(request: Request) -> str:
    explicit = request.headers.get("x-errorta-mobile-session", "").strip()
    if explicit:
        return explicit
    auth = request.headers.get("authorization", "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


__all__ = ["require_capability", "require_enabled", "require_paired_device"]
