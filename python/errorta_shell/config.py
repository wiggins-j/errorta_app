"""F006 — small in-process config store for shell-level settings.

Persists shell-tier settings (Ollama host override, cold-start timing) to a
JSON file under the user's Errorta data dir. F003 owns the canonical Ollama
settings — this store is intentionally minimal and forward-compatible with
that future integration.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import urlparse

_MAX_HOST_LEN = 256

_DEFAULT_OLLAMA_HOST = "http://127.0.0.1:11434"


def _data_dir() -> Path:
    """Compatibility shim. New code should import from ``errorta_app.paths``."""
    from errorta_app.paths import errorta_home
    return errorta_home()


def _config_path() -> Path:
    return _data_dir() / "shell.json"


_lock = Lock()
# Cold-start measurement is captured the moment this module first loads; the
# Tauri shell sends a final `mark_ready` once the frontend is interactive.
_PROCESS_START = time.time()
_ready_at: float | None = None


def _load() -> dict[str, Any]:
    p = _config_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save(data: dict[str, Any]) -> None:
    p = _config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    tmp.replace(p)


def get_ollama_host() -> str:
    with _lock:
        return str(_load().get("ollama_host", _DEFAULT_OLLAMA_HOST))


def set_ollama_host(host: str) -> str:
    host = host.strip()
    if not host:
        raise ValueError("ollama_host must be non-empty")
    if len(host) > _MAX_HOST_LEN:
        raise ValueError(f"ollama_host must be <= {_MAX_HOST_LEN} characters")
    parsed = urlparse(host)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("ollama_host must start with http:// or https://")
    if not parsed.netloc:
        raise ValueError("ollama_host must include a host (e.g. http://127.0.0.1:11434)")
    with _lock:
        data = _load()
        data["ollama_host"] = host
        _save(data)
        return host


def mark_ready() -> float:
    """Called by the frontend the first time it renders. Records cold-start."""
    global _ready_at
    with _lock:
        if _ready_at is None:
            _ready_at = time.time()
        return _ready_at - _PROCESS_START


def cold_start_seconds() -> float | None:
    """Return measured cold-start (seconds) if `mark_ready` was called."""
    if _ready_at is None:
        return None
    return max(0.0, _ready_at - _PROCESS_START)


def process_start() -> float:
    return _PROCESS_START
