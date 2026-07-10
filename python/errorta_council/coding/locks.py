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

Locks are process-local (the sidecar owns all coding runs); cross-process
coordination is out of scope.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Union

_META = threading.Lock()
_LOCKS: "dict[str, threading.RLock]" = {}


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


__all__ = ["lock_for_dir"]
