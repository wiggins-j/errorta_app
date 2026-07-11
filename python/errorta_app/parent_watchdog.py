"""F063 A3 — parent-death watchdog (F147 S9a — reference-counted clients).

The Tauri shell spawns this sidecar as a child and is supposed to kill it on
exit. But a SIGKILL'd / crashed / replaced-on-disk shell never runs its
cleanup, leaving the sidecar orphaned (reparented to PID 1) forever. macOS has
no ``PR_SET_PDEATHSIG``, so we poll: the shell passes its PID via
``ERRORTA_PARENT_PID`` at spawn, and a daemon thread exits the sidecar once no
registered client remains alive.

**F147 S9a — reference counting.** The original watchdog watched exactly one
``ERRORTA_PARENT_PID`` and exited the moment it died. That is wrong for a
*shared* sidecar (the single-instance contract, §13.1): a sidecar adopted by
several front-ends must outlive any one of them and only exit when the LAST
client is gone. So the watchdog now tracks a SET of client pids — the spawning
parent (``ERRORTA_PARENT_PID``) plus any client that registered a pidfile under
``${ERRORTA_HOME}/sidecar-clients/`` — and exits only when none is alive and a
grace period has elapsed.

Backward compatible: with only the spawning parent registered (the case today —
no front-end registers a client pidfile yet), the live set is ``{parent_pid}``
and the default grace of 0s reproduces the exact pre-S9a behavior ("quit the app
-> sidecar exits promptly"). ``should_exit`` is a pure function so the policy is
unit-testable without spawning processes.

No-op when ``ERRORTA_PARENT_PID`` is unset AND no client pidfiles exist
(standalone ``python -m errorta_app.server`` dev runs), so it only ever fires
under a real front-end.
"""
from __future__ import annotations

import contextlib
import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

_LOG = logging.getLogger("errorta_app.parent_watchdog")

DEFAULT_INTERVAL_SECONDS = 3.0
# Solo default: 0s grace reproduces the pre-S9a immediate-exit-on-parent-death
# UX. A shared/adopted sidecar can pass a small positive grace so a fast
# app-restart handoff doesn't briefly leave zero live clients and kill the
# sidecar out from under the reconnecting front-end.
DEFAULT_GRACE_SECONDS = 0.0

_CLIENTS_DIRNAME = "sidecar-clients"


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


def clients_dir() -> Path:
    from errorta_app.paths import errorta_home

    p = errorta_home() / _CLIENTS_DIRNAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def register_client(pid: Optional[int] = None) -> Optional[int]:
    """Register a front-end pid as a live client of this sidecar (write a
    pidfile). Idempotent. Best-effort — returns the pid on success, else None.
    Not wired into a front-end in S9a; the co-drive slice calls it."""
    want = int(pid if pid is not None else os.getpid())
    try:
        (clients_dir() / str(want)).write_text(str(want), encoding="utf-8")
        return want
    except Exception as exc:  # noqa: BLE001 - best effort
        _LOG.warning("could not register client pid %d: %s", want, exc)
        return None


def unregister_client(pid: Optional[int] = None) -> None:
    """Remove a client's pidfile (a clean disconnect). Best-effort."""
    want = int(pid if pid is not None else os.getpid())
    try:
        (clients_dir() / str(want)).unlink()
    except FileNotFoundError:
        return
    except Exception as exc:  # noqa: BLE001 - best effort
        _LOG.warning("could not unregister client pid %d: %s", want, exc)


def registered_client_pids(
    *,
    reap: bool = False,
    alive_fn: Callable[[int], bool] = parent_alive,
) -> set[int]:
    """The pids registered under the clients dir. Ignores malformed entries.
    Best-effort — returns an empty set on any error.

    F147 S9 follow-up (review LOW-5): when ``reap=True`` (the watchdog's live
    poll), a pidfile whose pid is NOT alive is unlinked and excluded from the
    result, so a crashed client that never ran ``unregister_client`` — especially
    combined with later pid reuse — can't keep a shared sidecar alive forever. A
    LIVE client's pidfile is never reaped, and a pid we can't prove dead
    (``alive_fn`` raises) is kept, so the refcount only ever loses genuinely-dead
    clients. Default ``reap=False`` keeps this a pure reader for its other
    callers/tests."""
    out: set[int] = set()
    try:
        d = clients_dir()
    except Exception:  # noqa: BLE001
        return out
    try:
        for entry in d.iterdir():
            try:
                pid = int(entry.name)
            except (ValueError, OSError):
                continue
            if reap:
                try:
                    dead = not alive_fn(pid)
                except Exception:  # noqa: BLE001 — can't prove dead → keep it
                    dead = False
                if dead:
                    # Reap the stale pidfile of a dead client. Best-effort: a
                    # concurrent unlink (another poll / a clean disconnect) is
                    # fine — the pid is excluded from the refcount either way.
                    with contextlib.suppress(FileNotFoundError, OSError):
                        entry.unlink()
                    continue
            out.add(pid)
    except Exception:  # noqa: BLE001
        return out
    return out


def should_exit(
    client_pids: Iterable[int],
    alive_fn: Callable[[int], bool] = parent_alive,
) -> bool:
    """Pure policy: exit iff NO registered client pid is alive.

    Vacuously true on an empty set (the parent already gone and no clients
    registered -> nothing to keep us up). This is the testable core; the grace
    period is applied by the loop, not here.
    """
    return not any(alive_fn(pid) for pid in client_pids)


def _live_client_set(
    parent_pid: Optional[int],
    *,
    reap: bool = False,
    alive_fn: Callable[[int], bool] = parent_alive,
) -> set[int]:
    pids: set[int] = set(registered_client_pids(reap=reap, alive_fn=alive_fn))
    if parent_pid is not None:
        pids.add(int(parent_pid))
    return pids


def start_parent_death_watchdog(
    *,
    parent_pid: int | None = None,
    interval: float = DEFAULT_INTERVAL_SECONDS,
    grace: float = DEFAULT_GRACE_SECONDS,
    exit_fn: Callable[[], None] | None = None,
    stop_event: threading.Event | None = None,
    clients_fn: Callable[[], set[int]] | None = None,
    alive_fn: Callable[[int], bool] = parent_alive,
) -> threading.Thread | None:
    """Start the watchdog daemon thread, or return None if not applicable.

    Returns None (no thread) when there is no parent PID AND no registered client
    to watch — the standalone dev case. ``exit_fn`` defaults to ``os._exit(0)`` (a
    hard exit that bypasses lifespan teardown — intentional: the last client is
    gone, there is nothing to coordinate a graceful shutdown with, and a lingering
    orphan is the worse outcome). ``stop_event`` / ``exit_fn`` / ``clients_fn`` /
    ``alive_fn`` are injectable for tests.
    """
    raw = parent_pid if parent_pid is not None else os.environ.get("ERRORTA_PARENT_PID")
    ppid: Optional[int]
    if raw in (None, ""):
        ppid = None
    else:
        try:
            ppid = int(raw)
        except (TypeError, ValueError):
            _LOG.warning("ignoring invalid ERRORTA_PARENT_PID=%r", raw)
            ppid = None
        else:
            if ppid <= 1:
                # 0/1 means "no real parent" (already orphaned / init).
                ppid = None

    def _clients() -> set[int]:
        if clients_fn is not None:
            return set(clients_fn())
        # F147 S9 follow-up (review LOW-5): reap dead client pidfiles on the live
        # poll using the same liveness probe the exit policy uses, so a crashed
        # client can't linger until an unrelated process reuses its pid.
        return _live_client_set(ppid, reap=True, alive_fn=alive_fn)

    # Nothing to watch: no parent and no registered client. Preserve the pre-S9a
    # standalone-dev no-op.
    if ppid is None and not _clients():
        return None

    _exit = exit_fn or (lambda: os._exit(0))
    stop = stop_event or threading.Event()

    def _loop() -> None:
        gone_since: Optional[float] = None
        # stop.wait returns True only if the event is set (test teardown);
        # otherwise it sleeps `interval` and we re-check.
        while not stop.wait(interval):
            pids = _clients()
            if not should_exit(pids, alive_fn):
                gone_since = None
                continue
            now = time.monotonic()
            if gone_since is None:
                gone_since = now
            if now - gone_since >= max(0.0, grace):
                _LOG.warning(
                    "no live client remains (watched=%s); exiting sidecar",
                    sorted(pids) or "none")
                _exit()
                return

    thread = threading.Thread(
        target=_loop, name="errorta-parent-watchdog", daemon=True,
    )
    thread.start()
    return thread


__all__ = [
    "DEFAULT_GRACE_SECONDS",
    "DEFAULT_INTERVAL_SECONDS",
    "clients_dir",
    "parent_alive",
    "register_client",
    "registered_client_pids",
    "should_exit",
    "start_parent_death_watchdog",
    "unregister_client",
]
