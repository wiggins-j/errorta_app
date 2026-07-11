"""F087-13 WS-3 — per-project locks for the Coding Team run lifecycle.

The run lifecycle has mutable shared state with no concurrency control: the
``_RUNS`` thread registry, the ``run_state.json`` read-modify-write, and the
boot/status recovery transition. FastAPI runs sync routes in a threadpool, so
two requests for the same project genuinely execute on different threads. A
single per-project ``threading.Lock`` — keyed by the project's resolved ledger
directory so every ``LedgerStore`` instance for that project shares one lock —
serializes:

* the start-run critical section (alive-check -> set running -> register -> start),
* every ``set_run_state`` read-modify-write,
* the ``recover_orphaned_run`` running->interrupted transition.

The in-process ``threading.RLock`` (``lock_for_dir``) covers threads inside one
sidecar. **F147 S9a adds a cross-*process* layer** so two sidecars sharing one
``ERRORTA_HOME`` (a future GUI+CLI co-drive) cannot corrupt each other's runs:

* ``cross_process_lock(ledger_dir)`` — an ``fcntl.flock`` on a per-project lock
  file that serializes the run critical section + the recovery transition ACROSS
  processes, so two ``_start_run`` critical sections can't interleave their
  ``run_state.json`` read-modify-writes and a recovery can't fire mid-write.
* ``run_owned_by_live_process(state, ...)`` — a pure liveness predicate over the
  ``owner_pid`` persisted in ``run_state``. Mutual exclusion of the critical
  section alone does NOT stop process B from *logically* reconciling process A's
  still-live run to ``interrupted`` (A releases the flock the instant its short
  critical section ends). This predicate is what lets B's start-guard 409 and B's
  ``GET /run`` recovery recognise "a run is live in another process" and stand
  down.

**Lock-ordering contract (avoids the AB/BA deadlock):** ``cross_process_lock``
is ALWAYS acquired *outside* ``lock_for_dir``/``store.lock`` — never the reverse.
Both are reentrant per (process, dir), so the start-run path (which holds
``store.lock`` and then calls ``_reconcile_run_state`` which re-enters
``cross_process_lock``) does not double-acquire the flock and does not
self-deadlock. Callers must preserve this ordering.

**Windows fallback:** ``fcntl`` is POSIX-only. On a platform without it,
``cross_process_lock`` degrades to the in-process reentrant guard alone (exactly
the pre-S9a, process-local behavior) and logs a one-time warning. This is the
same graceful-degradation stance the rest of the run lifecycle already takes.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, Optional, Union

try:  # POSIX only — absent on Windows.
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - exercised only on Windows
    _fcntl = None  # type: ignore[assignment]

_LOG = logging.getLogger("errorta.coding.locks")

_META = threading.Lock()
_LOCKS: "dict[str, threading.RLock]" = {}

# Per-dir cross-process lock objects (shared across every caller for one dir).
_XPROC_META = threading.Lock()
_XPROC: "dict[str, _CrossProcessLock]" = {}
_WIN_FALLBACK_WARNED = False

# The lock file lives NEXT TO the project ledger dir (``<dir>/.run.lock``) so it
# rides with the project and inherits its ERRORTA_HOME residency automatically.
_LOCK_FILENAME = ".run.lock"

# Default bounded wait for the run critical section. Under the sole-owner model
# in force today the flock is always immediately free (single process), so this
# never actually blocks; it only bites when a second sidecar genuinely contends,
# in which case a bounded wait + clear signal beats an unbounded hang.
DEFAULT_LOCK_TIMEOUT_SECONDS = 10.0


def _key(ledger_dir: Union[str, Path]) -> str:
    try:
        return str(Path(ledger_dir).resolve())
    except Exception:
        return str(ledger_dir)


def lock_for_dir(ledger_dir: Union[str, Path]) -> "threading.RLock":
    """Return the process-wide REENTRANT lock keyed by a project's ledger
    directory. Reentrant so a holder of the start-run / recovery critical section
    can call ``set_run_state`` (which re-acquires the same lock) without
    deadlocking."""
    k = _key(ledger_dir)
    with _META:
        lock = _LOCKS.get(k)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[k] = lock
        return lock


class RunLockBusy(RuntimeError):
    """Raised when the cross-process run lock is held by ANOTHER process and the
    bounded acquire window elapsed. The caller maps this to a 409 (start) or a
    skip-recovery (reconcile) — never to a run-state mutation."""


class _CrossProcessLock:
    """A per-dir reentrant lock that composes the in-process guard with an
    ``fcntl.flock`` on a lock file.

    Reentrancy: an in-process reentrant ``RLock`` guards the depth counter and is
    held for the whole ``with`` body, so nested acquisitions on the same thread
    (start-run -> reconcile) never re-flock and never deadlock. The flock is taken
    only on the 0->1 transition and released only on the 1->0 transition.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._guard = threading.RLock()
        self._depth = 0
        self._fd: Optional[int] = None

    def acquire(self, *, timeout: float) -> None:
        # Take the in-process guard FIRST (reentrant). Its ordering relative to
        # ``store.lock`` is fixed by the caller contract (this is acquired
        # outermost), so no cross-lock cycle is possible.
        self._guard.acquire()
        try:
            if self._depth == 0:
                self._acquire_flock(timeout)
            self._depth += 1
        except BaseException:
            self._guard.release()
            raise

    def release(self) -> None:
        try:
            if self._depth <= 0:  # pragma: no cover - defensive
                return
            self._depth -= 1
            if self._depth == 0 and self._fd is not None:
                fd, self._fd = self._fd, None
                try:
                    if _fcntl is not None:
                        _fcntl.flock(fd, _fcntl.LOCK_UN)
                finally:
                    try:
                        os.close(fd)
                    except OSError:  # pragma: no cover - defensive
                        pass
        finally:
            self._guard.release()

    def _acquire_flock(self, timeout: float) -> None:
        if _fcntl is None:
            _warn_windows_fallback()
            return  # in-process guard only — degrade gracefully
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self._path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            deadline = time.monotonic() + max(0.0, timeout)
            while True:
                try:
                    _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RunLockBusy(
                            f"run lock held by another process: {self._path}")
                    time.sleep(0.02)
        except BaseException:
            try:
                os.close(fd)
            except OSError:  # pragma: no cover - defensive
                pass
            raise
        self._fd = fd


def _warn_windows_fallback() -> None:
    global _WIN_FALLBACK_WARNED
    if not _WIN_FALLBACK_WARNED:
        _WIN_FALLBACK_WARNED = True
        _LOG.warning(
            "fcntl is unavailable (non-POSIX platform); the coding run lock "
            "degrades to process-local behavior. Cross-process (multi-sidecar) "
            "run coordination is not enforced on this platform.")


def _xproc_for_dir(ledger_dir: Union[str, Path]) -> _CrossProcessLock:
    k = _key(ledger_dir)
    with _XPROC_META:
        lock = _XPROC.get(k)
        if lock is None:
            lock = _CrossProcessLock(Path(ledger_dir) / _LOCK_FILENAME)
            _XPROC[k] = lock
        return lock


class _CrossProcessLockCtx:
    def __init__(self, lock: _CrossProcessLock, timeout: float) -> None:
        self._lock = lock
        self._timeout = timeout

    def __enter__(self) -> "_CrossProcessLockCtx":
        self._lock.acquire(timeout=self._timeout)
        return self

    def __exit__(self, *exc: object) -> None:
        self._lock.release()


def cross_process_lock(
    ledger_dir: Union[str, Path],
    *,
    timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> _CrossProcessLockCtx:
    """Context manager for the per-project cross-process run lock.

    MUST be acquired OUTSIDE ``lock_for_dir``/``store.lock`` (see the module
    docstring's lock-ordering contract). Reentrant per (process, dir). Raises
    :class:`RunLockBusy` if another process holds the lock past ``timeout``.
    Degrades to an in-process reentrant guard when ``fcntl`` is unavailable.
    """
    return _CrossProcessLockCtx(_xproc_for_dir(ledger_dir), timeout)


def run_owned_by_live_process(
    state: object,
    *,
    my_pid: int,
    alive_fn: Callable[[int], bool],
) -> bool:
    """Pure predicate: does ``run_state`` describe a ``running`` run owned by a
    DIFFERENT, still-alive process?

    Used by the start-guard (409) and by ``GET /run`` recovery so neither
    clobbers a run that is live in another sidecar. Returns ``False`` for our own
    pid, a dead pid, a missing/blank ``owner_pid``, or any non-``running`` state —
    which means single-process operation (the only case today, where
    ``owner_pid == my_pid``) falls back to exactly the pre-S9a, thread-local
    liveness check. ``alive_fn`` is injected (kept pure + testable; the caller
    passes the sidecar's process-existence probe).
    """
    if not isinstance(state, dict):
        return False
    if str(state.get("status") or "") != "running":
        return False
    raw = state.get("owner_pid")
    try:
        owner_pid = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    if owner_pid <= 0 or owner_pid == int(my_pid):
        return False
    try:
        return bool(alive_fn(owner_pid))
    except Exception:  # noqa: BLE001 - a probe failure must not clobber a run
        # Fail toward "owned/live" so we never reconcile a run we couldn't prove
        # dead. A genuinely orphaned run is caught by boot recovery, which is
        # now owner-aware but fail-OPEN toward recovery (see
        # ``owner_is_live_peer_sidecar``) so it can never wedge a real orphan.
        return True


def owner_is_live_peer_sidecar(
    state: object,
    *,
    my_pid: int,
    alive_fn: Callable[[int], bool],
    advert: object,
    healthz_fn: Callable[[int], "Optional[dict]"],
) -> bool:
    """F147 S9b — is this ``running`` run owned by a DIFFERENT, live, *advertised*
    peer sidecar? The predicate BOOT recovery consults so it stands down instead
    of reconciling a run that is live in another sidecar (closing the S9a boot-
    recovery gap, §13.1).

    Returns ``True`` ONLY on a POSITIVE confirmation of a real peer:

    * ``owner_pid`` is a different, non-blank pid that is currently alive, AND
    * the on-disk ``sidecar.json`` advertisement (``advert``) names that *same*
      pid, AND
    * a ``/healthz`` probe of the advertised port answers with that *same* pid.

    Any uncertainty — no owner_pid, our own pid, a dead pid, a missing/mismatched
    advertisement, or a healthz probe that fails / times out / reports a different
    pid — returns ``False``, i.e. "treat as a recoverable orphan". This is the
    deliberate fail-OPEN direction for the orphan safety-net: a genuine orphan
    (its owner_pid dead, or a pid that got *reused* by an unrelated process that
    isn't the advertised sidecar) is ALWAYS still cleared, so recovery can never
    wedge a real orphan forever. The advert+healthz cross-check is what defeats
    the pid-reuse false positive that plain ``os.kill(pid, 0)`` liveness can't.

    ``advert`` is passed as data (the parsed ``sidecar.json`` dict, or ``None``)
    and ``healthz_fn``/``alive_fn`` are injected, so this stays a pure predicate
    with no import of ``errorta_app`` (the caller supplies the app-side seams).
    """
    if not isinstance(state, dict):
        return False
    if str(state.get("status") or "") != "running":
        return False
    raw = state.get("owner_pid")
    try:
        owner_pid = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    if owner_pid <= 0 or owner_pid == int(my_pid):
        return False
    # 1. The owning pid must currently exist. A dead owner_pid → orphan → recover.
    try:
        if not alive_fn(owner_pid):
            return False
    except Exception:  # noqa: BLE001 - can't prove alive → treat as orphan
        return False
    # 2. The live advertisement must name that same pid. A missing/mismatched
    #    advert means the alive owner_pid is a REUSED pid (or a crash left no
    #    advert) — not a real peer sidecar → recover.
    if not isinstance(advert, dict):
        return False
    try:
        advert_pid = int(advert.get("pid"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    if advert_pid != owner_pid:
        return False
    try:
        port = int(advert.get("port"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    # 3. A /healthz probe of the advertised port must confirm the SAME pid is
    #    serving there. A reused pid whose owner isn't actually a sidecar fails
    #    this; a probe error also fails it (fail-OPEN toward recovery).
    try:
        body = healthz_fn(port)
    except Exception:  # noqa: BLE001 - probe failure → treat as orphan
        return False
    if not isinstance(body, dict):
        return False
    try:
        return int(body.get("pid")) == owner_pid  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


__all__ = [
    "DEFAULT_LOCK_TIMEOUT_SECONDS",
    "RunLockBusy",
    "cross_process_lock",
    "lock_for_dir",
    "owner_is_live_peer_sidecar",
    "run_owned_by_live_process",
]
