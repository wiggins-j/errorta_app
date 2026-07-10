"""F087 Slice 0 — a process-wide asyncio loop bridge for blocking callers.

The coding runner calls the async model gateway from synchronous worker threads.
The old path used ``asyncio.run(coro)`` per call, which creates a FRESH event
loop every time. That deadlocks under concurrency: the provider concurrency
gates are module-level ``asyncio.Semaphore``s, and a semaphore awaited across
two different loops never releases the waiter (reproduced in the spec review).

This module runs ONE event loop on a daemon thread for the whole process and
lets any thread submit a coroutine to it via ``run_coro`` (a thin wrapper over
``asyncio.run_coroutine_threadsafe``). Because every gateway call now runs on the
same loop, the provider semaphores bind to that single loop and bound
concurrency correctly instead of deadlocking.
"""
from __future__ import annotations

import asyncio
import threading
from typing import Awaitable, TypeVar

_T = TypeVar("_T")

_loop: asyncio.AbstractEventLoop | None = None
_lock = threading.Lock()


def _ensure_loop() -> asyncio.AbstractEventLoop:
    global _loop
    with _lock:
        if _loop is None or _loop.is_closed():
            loop = asyncio.new_event_loop()
            thread = threading.Thread(
                target=loop.run_forever,
                name="errorta-gateway-loop",
                daemon=True,
            )
            thread.start()
            _loop = loop
        return _loop


def run_coro(coro: Awaitable[_T], *, timeout: float | None = None) -> _T:
    """Run ``coro`` to completion on the shared loop from a sync caller (any
    thread) and return its result. Safe to call concurrently from many threads —
    that is the whole point. Exceptions propagate to the caller."""
    loop = _ensure_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)  # type: ignore[arg-type]
    return future.result(timeout)


def _shutdown_for_tests() -> None:
    """Tear the shared loop down (tests only — the daemon thread otherwise lives
    for the process lifetime)."""
    global _loop
    with _lock:
        if _loop is not None and not _loop.is_closed():
            _loop.call_soon_threadsafe(_loop.stop)
        _loop = None


__all__ = ["run_coro"]
