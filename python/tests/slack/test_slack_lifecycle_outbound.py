"""Slice 5b Task 6 — the outbound progress loop is actually scheduled.

`outbound.run_loop` shipped fully built and had ZERO production callers: the
bridge started its socket connection and never polled anything, so nothing
unprompted was ever posted into a bound channel.

Two things this file pins:

1. The poster handed to the outbound loop must be SYNCHRONOUS. `poll_once`
   calls `poster.post_message(...)` without awaiting it, so an `async def`
   poster (which is what the bridge's own ingress path uses) would create a
   coroutine, discard it, and post nothing at all -- with only a RuntimeWarning
   to show for it.
2. Starting twice must not leave two loops polling one channel.
"""
from __future__ import annotations

import inspect
import time
import threading
from typing import Any

from errorta_app import slack_lifecycle


def test_sync_poster_is_not_a_coroutine_function() -> None:
    """The contract poll_once needs. An async poster here posts nothing."""
    # Built without touching the network: only the WebClient constructor runs.
    poster = slack_lifecycle._build_sync_poster("xoxb-fake")

    assert not inspect.iscoroutinefunction(poster.post_message)


def test_start_outbound_runs_the_loop_and_stop_ends_it() -> None:
    started = threading.Event()
    seen: dict[str, Any] = {}

    async def _fake_run_loop(**kwargs: Any) -> None:
        seen.update(kwargs)
        started.set()
        stop_event = kwargs["stop_event"]
        while not stop_event.is_set():
            await _sleep_briefly()

    async def _sleep_briefly() -> None:
        import asyncio
        await asyncio.sleep(0.01)

    try:
        slack_lifecycle._start_outbound(object(), run_loop_fn=_fake_run_loop)
        assert started.wait(timeout=5), "the outbound loop never started"
        assert slack_lifecycle._outbound_thread is not None
        assert slack_lifecycle._outbound_thread.is_alive()
        # It polls whatever is currently bound, not a snapshot taken at start.
        assert callable(seen["bindings_provider"])
    finally:
        slack_lifecycle._stop_outbound()

    assert slack_lifecycle._outbound_thread is None


def test_starting_twice_does_not_leave_two_loops() -> None:
    """A sync() restart must not double-post into every bound channel."""
    live: list[str] = []

    async def _fake_run_loop(**kwargs: Any) -> None:
        import asyncio
        token = str(len(live))
        live.append(token)
        stop_event = kwargs["stop_event"]
        try:
            while not stop_event.is_set():
                await asyncio.sleep(0.01)
        finally:
            live.remove(token)

    def _wait_for_live(n: int) -> bool:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if len(live) == n:
                return True
            time.sleep(0.01)
        return False

    try:
        slack_lifecycle._start_outbound(object(), run_loop_fn=_fake_run_loop)
        assert _wait_for_live(1)
        first = slack_lifecycle._outbound_thread

        slack_lifecycle._start_outbound(object(), run_loop_fn=_fake_run_loop)

        assert first is not slack_lifecycle._outbound_thread
        assert first is not None and not first.is_alive(), \
            "the first outbound loop is still running"
        # Exactly one, not two: a sync() restart must not double-post into
        # every bound channel.
        assert _wait_for_live(1)
    finally:
        slack_lifecycle._stop_outbound()
