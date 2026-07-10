"""F-INFRA-12 — persistent residency mode + connection config.

Public API:
    ResidencyMode  # Literal["local", "ssh-remote", "cloud"]
    ResidencyState # frozen dataclass
    load() -> ResidencyState
    save(state: ResidencyState) -> None
    update(**fields) -> ResidencyState
"""
from __future__ import annotations

from .config import ResidencyMode, ResidencyState, load, save, update

__all__ = ["ResidencyMode", "ResidencyState", "load", "save", "update"]
