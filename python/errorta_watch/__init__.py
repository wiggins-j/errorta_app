"""F005 — folder watch + auto-ingest.

Polling-based folder watcher. One coordinator supervises per-corpus pollers,
each of which scans its watched folder every 60s and reconciles with a manifest
persisted under ``~/.errorta/corpora/{name}/watch.json``.
"""
from __future__ import annotations

from .coordinator import WatchCoordinator, get_coordinator
from .state import WatchState, load_state, save_state, state_path
from .ignore import is_ignored, is_cloud_sync_path, DEFAULT_IGNORES

__all__ = [
    "WatchCoordinator",
    "get_coordinator",
    "WatchState",
    "load_state",
    "save_state",
    "state_path",
    "is_ignored",
    "is_cloud_sync_path",
    "DEFAULT_IGNORES",
]
