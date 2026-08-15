"""Persistent configuration for the Slack PM bridge.

Mirrors `errorta_mobile/config.py`'s atomic-write + `${ERRORTA_HOME}`
resolution pattern. This module MUST NOT import `slack_sdk` or any other
optional dependency at module load time — the Slack bridge is strictly
optional and disabled by default, so the rest of the sidecar must keep
working when `slack-sdk` isn't installed.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from errorta_app.paths import errorta_home

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": False,
    "bindings": [],
    "window": 20,
    "timeout_minutes": 30,
}


def slack_dir() -> Path:
    p = errorta_home() / "slack"
    p.mkdir(parents=True, exist_ok=True)
    os.chmod(p, 0o700)
    return p


def config_path() -> Path:
    return slack_dir() / "config.json"


def _bool(value: Any) -> bool:
    return bool(value)


def _int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _bindings(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def normalize(raw: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(DEFAULT_CONFIG)
    if raw:
        merged.update(raw)
    return {
        "enabled": _bool(merged.get("enabled")),
        "bindings": _bindings(merged.get("bindings")),
        "window": _int(merged.get("window"), default=int(DEFAULT_CONFIG["window"])),
        "timeout_minutes": _int(
            merged.get("timeout_minutes"),
            default=int(DEFAULT_CONFIG["timeout_minutes"]),
        ),
    }


def load() -> dict[str, Any]:
    path = config_path()
    if not path.exists():
        save(dict(DEFAULT_CONFIG))
        return dict(DEFAULT_CONFIG)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULT_CONFIG)
    if not isinstance(raw, dict):
        return dict(DEFAULT_CONFIG)
    return normalize(raw)


def save(cfg: dict[str, Any]) -> None:
    normalized = normalize(cfg)
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".config-",
        suffix=".json",
        dir=str(path.parent),
        text=True,
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(normalized, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
        os.chmod(path, 0o600)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def is_enabled() -> bool:
    return bool(load().get("enabled"))


__all__ = [
    "DEFAULT_CONFIG",
    "config_path",
    "is_enabled",
    "load",
    "normalize",
    "save",
    "slack_dir",
]
