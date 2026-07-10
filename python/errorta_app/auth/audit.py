"""Append-only audit log for the Service API auth boundary."""

from __future__ import annotations

import json
import os
from typing import Any

from errorta_app.paths import auth_audit_path

from .store import now_iso


def record_event(event: str, **fields: Any) -> dict[str, Any]:
    """Append a redaction-safe Service API audit event.

    Callers must pass identifiers and metadata only. Raw tokens, prompts, and
    answer content do not belong in this log.
    """

    payload = {
        "ts": now_iso(),
        "event": event,
        **{key: value for key, value in fields.items() if value is not None},
    }
    path = auth_audit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            json.dump(payload, fh, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
    finally:
        if os.name == "posix":
            os.chmod(path, 0o600)
    return payload


def read_events() -> list[dict[str, Any]]:
    try:
        lines = auth_audit_path().read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    out: list[dict[str, Any]] = []
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


__all__ = ["read_events", "record_event"]
