"""Atomic 0600 JSON read/write for the alpha state files.

Mirrors the ``errorta_app.settings.save`` pattern (``mkstemp`` -> ``fsync`` ->
``chmod 0600`` -> ``os.replace``) rather than hand-rolling a second writer, so
``device.json`` / ``license.json`` / ``telemetry.json`` get the same
crash-safe, owner-only guarantees as the rest of Errorta's on-disk state.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def read_json(path: Path) -> dict[str, Any] | None:
    """Return the parsed JSON object, or ``None`` if missing/unreadable/not a dict.

    Never raises on a corrupt file — a garbage ``license.json`` should degrade to
    "unactivated", not crash the sidecar.
    """
    try:
        if not path.exists():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("alpha: failed to read %s: %s", path, exc)
        return None
    if not isinstance(raw, dict):
        log.warning("alpha: ignoring non-object payload at %s", path)
        return None
    return raw


def write_json_0600(path: Path, obj: dict[str, Any]) -> None:
    """Atomically persist ``obj`` as pretty JSON with mode 0600."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}-", suffix=".tmp", dir=str(path.parent), text=True
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
        os.chmod(path, 0o600)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
