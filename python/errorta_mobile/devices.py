"""Durable paired-device records for the mobile connector."""
from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import json
import os
import secrets
import tempfile
from pathlib import Path
from typing import Any

from . import config as mobile_config

DEVICE_STORE_VERSION = 1

# F065: a freshly-paired device starts READ-ONLY. Start/cancel/message and any
# approval authority require a second, explicit grant in the desktop
# device-management UI (update_capabilities) — so one pairing tap can't hand a
# LAN device the ability to start paid runs or steer the council.
DEFAULT_CAPABILITIES: dict[str, bool] = {
    "read_runs": True,
    "start_runs": False,
    "send_messages": False,
    "cancel_runs": False,
    "read_coding_projects": False,
    "read_coding_activity": False,
    "read_coding_diffs": False,
    "send_coding_messages": False,
    "start_coding_runs": False,
    "resume_coding_runs": False,
    "cancel_coding_runs": False,
    "edit_coding_plan": False,
    "accept_coding_merge_back": False,
    "approve_low_risk": False,
    "approve_remote_egress": False,
    "approve_mcp_elicitation": False,
    "approve_code_exec": False,
    "approve_code_write": False,
    "approve_merge_back": False,
}


class DeviceAuthError(PermissionError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def token_hash(token: str) -> str:
    return _sha256(token)


def public_key_fingerprint(public_key: str) -> str:
    return _sha256(public_key)[:16]


def _normalize_capabilities(raw: dict[str, Any] | None = None) -> dict[str, bool]:
    caps = dict(DEFAULT_CAPABILITIES)
    if raw:
        for key, value in raw.items():
            if key in caps:
                caps[key] = bool(value)
    return caps


def _store_payload(devices: list[dict[str, Any]]) -> dict[str, Any]:
    return {"format_version": DEVICE_STORE_VERSION, "devices": devices}


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".devices-",
        suffix=".json",
        dir=str(path.parent),
        text=True,
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
        os.chmod(path, 0o600)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def load() -> list[dict[str, Any]]:
    path = mobile_config.devices_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return []
    if isinstance(raw, list):
        devices = raw
    elif isinstance(raw, dict) and isinstance(raw.get("devices"), list):
        devices = raw["devices"]
    else:
        return []
    out: list[dict[str, Any]] = []
    for item in devices:
        if not isinstance(item, dict):
            continue
        record = dict(item)
        record["capabilities"] = _normalize_capabilities(record.get("capabilities"))
        out.append(record)
    return out


def save(devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = []
    for item in devices:
        record = dict(item)
        record["capabilities"] = _normalize_capabilities(record.get("capabilities"))
        normalized.append(record)
    _write_atomic(mobile_config.devices_path(), _store_payload(normalized))
    return normalized


def list_public() -> list[dict[str, Any]]:
    return [public_projection(record) for record in load()]


def public_projection(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "device_id": record.get("device_id"),
        "display_name": record.get("display_name"),
        "platform": record.get("platform"),
        "public_key_fingerprint": record.get("public_key_fingerprint"),
        "paired_at": record.get("paired_at"),
        "last_seen_at": record.get("last_seen_at"),
        "last_ip_label": record.get("last_ip_label"),
        "capabilities": _normalize_capabilities(record.get("capabilities")),
        "revoked_at": record.get("revoked_at"),
    }


def get(device_id: str) -> dict[str, Any] | None:
    for record in load():
        if record.get("device_id") == device_id:
            return record
    return None


def create(
    *,
    display_name: str,
    platform: str,
    public_key: str,
    session_token: str,
) -> dict[str, Any]:
    records = load()
    now = _now()
    record = {
        "device_id": f"mob_dev_{secrets.token_urlsafe(16)}",
        "display_name": display_name.strip() or "iPhone",
        "platform": platform.strip() or "ios",
        "public_key": public_key,
        "public_key_fingerprint": public_key_fingerprint(public_key),
        "paired_at": now,
        "last_seen_at": None,
        "last_ip_label": None,
        "capabilities": dict(DEFAULT_CAPABILITIES),
        "revoked_at": None,
        "session_token_sha256": token_hash(session_token),
        "session_created_at": now,
    }
    records.append(record)
    save(records)
    return record


def revoke(device_id: str) -> dict[str, Any]:
    records = load()
    for idx, record in enumerate(records):
        if record.get("device_id") == device_id:
            updated = dict(record)
            updated["revoked_at"] = updated.get("revoked_at") or _now()
            records[idx] = updated
            save(records)
            return updated
    raise KeyError(device_id)


def delete(device_id: str) -> dict[str, Any]:
    """Forget a device entirely: drop its record (and its stored session-token
    hash). The phone keeps no access — a later request presents an unknown
    device id and is rejected. Returns the removed record."""
    records = load()
    for idx, record in enumerate(records):
        if record.get("device_id") == device_id:
            removed = records.pop(idx)
            save(records)
            return removed
    raise KeyError(device_id)


def update_capabilities(device_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(k for k in updates if k not in DEFAULT_CAPABILITIES)
    if unknown:
        raise ValueError(f"unknown_mobile_capability:{unknown[0]}")
    records = load()
    for idx, record in enumerate(records):
        if record.get("device_id") == device_id:
            updated = dict(record)
            caps = _normalize_capabilities(record.get("capabilities"))
            caps.update({key: bool(value) for key, value in updates.items()})
            updated["capabilities"] = caps
            records[idx] = updated
            save(records)
            return updated
    raise KeyError(device_id)


def authenticate(device_id: str, session_token: str) -> dict[str, Any]:
    record = get(device_id)
    if record is None:
        raise DeviceAuthError("mobile_device_auth_required")
    if record.get("revoked_at"):
        raise DeviceAuthError("mobile_device_revoked")
    stored_hash = str(record.get("session_token_sha256") or "")
    if not stored_hash or not hmac.compare_digest(
        stored_hash, token_hash(session_token)
    ):
        raise DeviceAuthError("mobile_device_auth_required")
    return record


def require_capability(record: dict[str, Any], capability: str) -> None:
    caps = _normalize_capabilities(record.get("capabilities"))
    if not caps.get(capability, False):
        raise DeviceAuthError(f"mobile_capability_forbidden:{capability}")


__all__ = [
    "DEFAULT_CAPABILITIES",
    "DEVICE_STORE_VERSION",
    "DeviceAuthError",
    "authenticate",
    "create",
    "get",
    "list_public",
    "load",
    "public_key_fingerprint",
    "public_projection",
    "require_capability",
    "revoke",
    "save",
    "token_hash",
    "update_capabilities",
]
