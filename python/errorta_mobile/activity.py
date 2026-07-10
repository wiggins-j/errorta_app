"""F074 — track the most recent run a mobile device touched, so the desktop
Council pane can auto-surface it ("pop it up") when you message or start a run
from your phone.

In-memory + process-local + monotonic counter. The counter (not a wall clock)
is what the desktop polls against, so it can reliably detect "new activity"
without clock-skew or equal-timestamp ambiguity. Resets on restart — that's
fine; it only drives a transient UI affordance.
"""
from __future__ import annotations

import threading
from typing import Any

_lock = threading.Lock()
_seq = 0
_latest: dict[str, Any] | None = None


def record(run_id: str, kind: str) -> None:
    """Note that a mobile device acted on ``run_id`` (kind: 'start' | 'message')."""
    global _seq, _latest
    if not run_id:
        return
    with _lock:
        _seq += 1
        _latest = {"run_id": str(run_id), "kind": str(kind), "seq": _seq}


def latest() -> dict[str, Any]:
    """The most recent mobile-touched run, or ``{run_id: None, seq: <n>}``.

    ``seq`` is always returned (even with no activity) so the desktop can seed
    its baseline and only react to strictly-newer activity."""
    with _lock:
        if _latest is None:
            return {"run_id": None, "seq": _seq}
        return dict(_latest)


def reset() -> None:
    """Test hook."""
    global _seq, _latest
    with _lock:
        _seq = 0
        _latest = None


__all__ = ["record", "latest", "reset"]
