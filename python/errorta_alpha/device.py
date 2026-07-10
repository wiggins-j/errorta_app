"""First-run device identity.

A random UUIDv4 generated on first run and stored 0600 (spec §7). Chosen over
hardware fingerprinting because it is privacy-friendly (no machine identifiers),
stable across OS updates (won't false-revoke), and trivially rotatable —
deleting ``device.json`` yields a new identity, which is exactly the behavior we
keep at v1.0 as the anonymous, resettable install id.
"""
from __future__ import annotations

import uuid

from errorta_app.paths import alpha_device_path

from .storage import read_json, write_json_0600


def _is_valid_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def read_device_id() -> str | None:
    """Return the stored device id, or ``None`` if unset/corrupt."""
    data = read_json(alpha_device_path())
    if not data:
        return None
    device_id = data.get("device_id")
    return device_id if _is_valid_uuid(device_id) else None


def get_or_create_device_id() -> str:
    """Return the device id, generating and persisting one on first call.

    Idempotent: a valid existing id is returned unchanged. A missing or corrupt
    file is (re)written with a fresh UUIDv4.
    """
    existing = read_device_id()
    if existing is not None:
        return existing
    device_id = str(uuid.uuid4())
    write_json_0600(alpha_device_path(), {"device_id": device_id})
    return device_id
