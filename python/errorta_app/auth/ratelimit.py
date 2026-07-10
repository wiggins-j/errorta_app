"""In-memory rate limiting for Service API auth surfaces."""

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
    max_failures: int = 8
    window_seconds: float = 60.0
    lockout_seconds: float = 300.0
    _buckets: dict[str, _Bucket] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _now(self) -> float:
        return time.monotonic()

    def check(self, key: str) -> None:
        now = self._now()
        with self._lock:
            for item in (key, "*global*"):
                bucket = self._buckets.get(item)
                if bucket and bucket.locked_until > now:
                    raise RateLimited(int(bucket.locked_until - now))

    def record_failure(self, key: str) -> None:
        now = self._now()
        with self._lock:
            for item in (key, "*global*"):
                bucket = self._buckets.setdefault(item, _Bucket(window_start=now))
                if now - bucket.window_start > self.window_seconds:
                    bucket.failures = 0
                    bucket.window_start = now
                bucket.failures += 1
                if bucket.failures >= self.max_failures:
                    bucket.locked_until = now + self.lockout_seconds

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


pairing_limiter = RateLimiter(max_failures=8)
auth_failure_limiter = RateLimiter(max_failures=12)

__all__ = ["RateLimited", "RateLimiter", "auth_failure_limiter", "pairing_limiter"]
