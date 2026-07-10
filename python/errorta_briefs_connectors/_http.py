"""Shared HTTP retry helper for brief source connectors.

F008-NTRS track. Centralizes the retry/backoff policy so each connector can
delegate transient-failure handling to a single, testable function. The policy
mirrors what the NTRS spec calls out:

* 429 / 408 / 5xx                → RetryableError, exponential backoff (1s, 2s,
                                   4s, 8s, 16s, cap 30s), up to 5 retries.
* httpx.ConnectError / ReadTimeout → same.
* Other 4xx (including 404)      → FatalError, no retries.

The connector passes in its own `httpx.Client` (already politeness-gated by the
caller) so the helper is pure: it only translates statuses and exceptions, then
either re-raises or returns the final 2xx Response.
"""
from __future__ import annotations

import time
from typing import Callable, Optional

import httpx

from errorta_briefs_connectors import FatalError, RetryableError

# Backoff schedule in seconds. Capped at 30s, 5 retries max (6 total attempts).
_BACKOFF_SCHEDULE: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 16.0)
_BACKOFF_CAP_S: float = 30.0
_RETRYABLE_STATUSES: frozenset[int] = frozenset({408, 429, 500, 502, 503, 504})


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def request_with_retry(
    send: Callable[[], httpx.Response],
    *,
    max_retries: int = 5,
    sleep: Optional[Callable[[float], None]] = None,
) -> httpx.Response:
    # Resolve sleep lazily so tests that monkeypatch `_http.time.sleep` after
    # import still take effect.
    if sleep is None:
        sleep = time.sleep
    """Invoke `send()`, retrying transient failures with exponential backoff.

    `send` is a zero-arg callable that performs one HTTP request and returns a
    Response. The caller is responsible for politeness gating *before* `send`
    is invoked — this helper only adds backoff *between* retries.

    Returns the first 2xx response. Raises FatalError on permanent failure or
    RetryableError if the retry budget is exhausted.
    """
    attempt = 0
    last_retry_after: Optional[float] = None
    while True:
        try:
            resp = send()
        except (httpx.ConnectError, httpx.ReadTimeout) as exc:
            if attempt >= max_retries:
                raise RetryableError(f"network error after {attempt} retries: {exc}") from exc
            sleep(_backoff_for(attempt, retry_after=None))
            attempt += 1
            continue

        status = resp.status_code
        if 200 <= status < 300:
            return resp

        if status in _RETRYABLE_STATUSES:
            last_retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
            if attempt >= max_retries:
                raise RetryableError(
                    f"HTTP {status} after {attempt} retries",
                    retry_after_s=last_retry_after,
                )
            sleep(_backoff_for(attempt, retry_after=last_retry_after))
            attempt += 1
            continue

        if 400 <= status < 500:
            raise FatalError(f"HTTP {status}: {resp.text[:200]}")

        # Anything else (e.g. exotic 5xx outside the table) → fatal.
        raise FatalError(f"unexpected HTTP {status}: {resp.text[:200]}")


def _backoff_for(attempt: int, *, retry_after: Optional[float]) -> float:
    """Return the sleep interval before retry number `attempt` (0-indexed).

    Honors a server-supplied Retry-After (capped at _BACKOFF_CAP_S) when
    present; otherwise consults the static schedule.
    """
    if retry_after is not None and retry_after > 0:
        return min(retry_after, _BACKOFF_CAP_S)
    if attempt < len(_BACKOFF_SCHEDULE):
        return _BACKOFF_SCHEDULE[attempt]
    return _BACKOFF_CAP_S
