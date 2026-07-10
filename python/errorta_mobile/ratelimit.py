"""F065 — in-memory rate limiting / lockout for the pairing surface.

On a LAN listener, pairing endpoints are reachable by anything on the network.
Per-source-IP keys are advisory only (an attacker can rotate IPs), so we ALSO
keep a global attempt budget. State is in-memory + fail-closed + resets on
process restart — the 256-bit pairing token is the primary brute-force defense;
this just blunts flooding of the owner-approval queue.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class _Bucket:
    failures: int = 0
    window_start: float = 0.0
    locked_until: float = 0.0


@dataclass
class RateLimiter:
    """Sliding-window failure counter with lockout, per key + a global key."""

    max_failures: int = 8
    window_seconds: float = 60.0
    lockout_seconds: float = 300.0
    _buckets: dict[str, _Bucket] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _now(self) -> float:
        return time.monotonic()

    def check(self, key: str) -> None:
        """Raise RateLimited if ``key`` (or the global bucket) is locked out."""
        now = self._now()
        with self._lock:
            for k in (key, "*global*"):
                b = self._buckets.get(k)
                if b and b.locked_until > now:
                    raise RateLimited(int(b.locked_until - now))

    def record_failure(self, key: str) -> None:
        now = self._now()
        with self._lock:
            for k in (key, "*global*"):
                b = self._buckets.setdefault(k, _Bucket(window_start=now))
                if now - b.window_start > self.window_seconds:
                    b.failures = 0
                    b.window_start = now
                b.failures += 1
                if b.failures >= self.max_failures:
                    b.locked_until = now + self.lockout_seconds

    def record_success(self, key: str) -> None:
        with self._lock:
            self._buckets.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


class RateLimited(Exception):
    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("rate_limited")
        self.retry_after_seconds = retry_after_seconds


# Module-level limiter shared by the pairing endpoints.
pairing_limiter = RateLimiter()


__all__ = ["RateLimited", "RateLimiter", "pairing_limiter"]
