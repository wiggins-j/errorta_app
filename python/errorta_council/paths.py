"""Residency-aware Council path helpers (F031-26 subset).

Every Council file lives under ``errorta_app.paths.errorta_home() / "council"``
so the active sidecar — local, SSH-remote, or future cloud — owns the data
(invariant 8). No hardcoded ``~/.errorta`` anywhere in Council code.
"""
from __future__ import annotations

from pathlib import Path

from errorta_app.paths import errorta_home


def council_root() -> Path:
    p = errorta_home() / "council"
    p.mkdir(parents=True, exist_ok=True)
    return p


def rooms_dir() -> Path:
    p = council_root() / "rooms"
    p.mkdir(parents=True, exist_ok=True)
    return p


def deleted_rooms_dir() -> Path:
    p = rooms_dir() / "deleted"
    p.mkdir(parents=True, exist_ok=True)
    return p


def runs_dir() -> Path:
    p = council_root() / "runs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def token_calibration_path() -> Path:
    p = council_root() / "token-calibration.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p
