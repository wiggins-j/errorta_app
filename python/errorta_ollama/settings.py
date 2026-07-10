"""Persistent settings for the Ollama integration.

Stored as JSON at ~/.errorta/ollama.json so the `managed_by_errorta` flag
survives across launches and the restart-on-relaunch logic only touches
installs that Errorta owns.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional


def _state_dir() -> Path:
    """Compatibility shim. New code should import from ``errorta_app.paths``."""
    from errorta_app.paths import errorta_home
    return errorta_home()


def _settings_path() -> Path:
    from errorta_app.paths import ollama_settings_path
    return ollama_settings_path()


DEFAULT_HOST = "http://localhost:11434"


@dataclass
class OllamaSettings:
    host: str = DEFAULT_HOST
    storage_path: Optional[str] = None
    managed_by_errorta: bool = False
    installed_version: Optional[str] = None
    last_install_at: Optional[str] = None
    # When True, we attempted to start a managed Ollama and it should be
    # restarted on next launch if it isn't reachable.
    expect_running: bool = False
    extra: dict = field(default_factory=dict)


def load() -> OllamaSettings:
    p = _settings_path()
    if not p.exists():
        return OllamaSettings()
    try:
        raw = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return OllamaSettings()
    # Tolerate forward/backward schema drift.
    known = {f for f in OllamaSettings.__dataclass_fields__}
    clean = {k: v for k, v in raw.items() if k in known}
    extra = {k: v for k, v in raw.items() if k not in known}
    if extra:
        clean["extra"] = extra
    return OllamaSettings(**clean)


def save(settings: OllamaSettings) -> None:
    p = _settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(asdict(settings), indent=2, sort_keys=True))
    os.replace(tmp, p)


def update(**fields: object) -> OllamaSettings:
    """Partial update — load, merge, save, return."""
    cur = load()
    for k, v in fields.items():
        if k in cur.__dataclass_fields__:
            setattr(cur, k, v)
    save(cur)
    return cur
