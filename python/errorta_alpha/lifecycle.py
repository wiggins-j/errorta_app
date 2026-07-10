"""F-DIST-01 slice 6 — the background check-in loop.

Runs ONLY when the alpha gate is on (started from the sidecar lifespan). It
records one launch, then periodically calls ``client.sync`` to drain the floor
deltas (via heartbeat) and the extras queue (via metrics). Every iteration is
best-effort; a transport failure is logged and retried on the next tick. The
loop is a daemon thread with a stop event so shutdown never blocks.
"""
from __future__ import annotations

import logging
import threading

from . import client, telemetry

log = logging.getLogger(__name__)

# The heartbeat itself is deduped to once/hour server-side (client._HEARTBEAT_
# MIN_INTERVAL); this tick just wakes often enough to flush a freshly-queued
# extra reasonably soon without busy-looping.
_DEFAULT_INTERVAL_SECONDS = 900  # 15 minutes


def start_background_sync(*, interval_seconds: int = _DEFAULT_INTERVAL_SECONDS) -> threading.Event:
    """Start the daemon sync loop and return a stop Event. Idempotent per call —
    callers hold the returned Event and ``.set()`` it on shutdown."""
    stop = threading.Event()

    def _loop() -> None:
        telemetry.record_launch()
        # Kick an immediate first sync so a launch/extra isn't stuck for a full
        # interval, then settle into the periodic cadence.
        while True:
            try:
                client.sync()
            except Exception as exc:  # noqa: BLE001 — never let the loop die
                log.info("alpha: background sync error: %s", exc)
            if stop.wait(interval_seconds):
                break

    thread = threading.Thread(target=_loop, name="errorta-alpha-sync", daemon=True)
    thread.start()
    return stop
