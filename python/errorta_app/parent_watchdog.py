"""F063 A3 — parent-death watchdog.

The Tauri shell spawns this sidecar as a child and is supposed to kill it on
exit. But a SIGKILL'd / crashed / replaced-on-disk shell never runs its
cleanup, leaving the sidecar orphaned (reparented to PID 1) forever. macOS has
no ``PR_SET_PDEATHSIG``, so we poll: the shell passes its PID via
``ERRORTA_PARENT_PID`` at spawn, and a daemon thread exits the sidecar once that
PID is gone.

No-op when ``ERRORTA_PARENT_PID`` is unset (standalone
``python -m errorta_app.server`` dev runs), so it only ever fires under the
Tauri shell.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Callable

_LOG = logging.getLogger("errorta_app.parent_watchdog")

DEFAULT_INTERVAL_SECONDS = 3.0


def parent_alive(pid: int) -> bool:
    """Whether process ``pid`` currently exists (signal 0 = existence check)."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but owned by another user — still alive.
        return True
    except OSError:
        return False


def start_parent_death_watchdog(
    *,
    parent_pid: int | None = None,
    interval: float = DEFAULT_INTERVAL_SECONDS,
    exit_fn: Callable[[], None] | None = None,
    stop_event: threading.Event | None = None,
) -> threading.Thread | None:
    """Start the watchdog daemon thread, or return None if not applicable.

    Returns None (no thread) when no valid parent PID is available — the
    standalone dev case. ``exit_fn`` defaults to ``os._exit(0)`` (a hard exit
    that bypasses lifespan teardown — intentional: the parent is already gone,
    there is nothing to coordinate a graceful shutdown with, and a lingering
    orphan is the worse outcome). ``stop_event`` + ``exit_fn`` are injectable
    for tests.
    """
    raw = parent_pid if parent_pid is not None else os.environ.get("ERRORTA_PARENT_PID")
    if raw in (None, ""):
        return None
    try:
        ppid = int(raw)
    except (TypeError, ValueError):
        _LOG.warning("ignoring invalid ERRORTA_PARENT_PID=%r", raw)
        return None
    if ppid <= 1:
        # 0/1 means "no real parent" (already orphaned / init); nothing to watch.
        return None

    _exit = exit_fn or (lambda: os._exit(0))
    stop = stop_event or threading.Event()

    def _loop() -> None:
        # stop.wait returns True only if the event is set (test teardown);
        # otherwise it sleeps `interval` and we re-check.
        while not stop.wait(interval):
            if not parent_alive(ppid):
                _LOG.warning("parent process %d is gone; exiting sidecar", ppid)
                _exit()
                return

    thread = threading.Thread(
        target=_loop, name="errorta-parent-watchdog", daemon=True,
    )
    thread.start()
    return thread


__all__ = [
    "DEFAULT_INTERVAL_SECONDS",
    "parent_alive",
    "start_parent_death_watchdog",
]
