"""F006 — managed child-process inventory.

The Tauri shell spawns two long-lived children: the Errorta Python sidecar
(this process) and, optionally, an Ollama daemon (see F003). This module
introspects the running sidecar and any registered Ollama PID so the Settings
pane can render a live process-health view.

The set of registered "managed" PIDs is intentionally tiny for v0.1 — the
sidecar's own PID is always included, and any extra PIDs (e.g. an Ollama
binary Errorta started) can be registered via `register_managed_pid`.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Iterable

import psutil

# Module-level registry of extra managed PIDs (besides this process).
# F003 will populate this when it spawns Ollama.
_extra_pids: set[int] = set()


def register_managed_pid(pid: int) -> None:
    """Mark a PID as Errorta-managed. Idempotent."""
    if pid > 0:
        _extra_pids.add(int(pid))


def unregister_managed_pid(pid: int) -> None:
    _extra_pids.discard(int(pid))


@dataclass
class ProcessInfo:
    pid: int
    name: str
    role: str  # "sidecar" | "ollama" | "child"
    status: str
    cpu_percent: float
    rss_bytes: int
    started_at: float | None  # epoch seconds

    def to_dict(self) -> dict:
        return {
            "pid": self.pid,
            "name": self.name,
            "role": self.role,
            "status": self.status,
            "cpu_percent": round(self.cpu_percent, 2),
            "rss_bytes": int(self.rss_bytes),
            "started_at": self.started_at,
        }


def _inspect(pid: int, role: str) -> ProcessInfo | None:
    try:
        p = psutil.Process(pid)
        with p.oneshot():
            return ProcessInfo(
                pid=pid,
                name=p.name(),
                role=role,
                status=p.status(),
                cpu_percent=p.cpu_percent(interval=None),
                rss_bytes=p.memory_info().rss,
                started_at=p.create_time(),
            )
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


def list_managed() -> list[ProcessInfo]:
    """Return ProcessInfo entries for the sidecar and any registered children."""
    out: list[ProcessInfo] = []
    sidecar = _inspect(os.getpid(), "sidecar")
    if sidecar is not None:
        out.append(sidecar)
    for pid in sorted(_extra_pids):
        info = _inspect(pid, "ollama" if _looks_like_ollama(pid) else "child")
        if info is not None:
            out.append(info)
    return out


def _looks_like_ollama(pid: int) -> bool:
    try:
        return "ollama" in psutil.Process(pid).name().lower()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def uptime_seconds() -> float:
    """How long this sidecar process has been alive."""
    try:
        return max(0.0, time.time() - psutil.Process(os.getpid()).create_time())
    except psutil.Error:
        return 0.0


def to_payload(infos: Iterable[ProcessInfo]) -> list[dict]:
    return [i.to_dict() for i in infos]
