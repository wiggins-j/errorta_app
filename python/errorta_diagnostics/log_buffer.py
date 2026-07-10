"""In-memory ring-buffer log capture for the diagnostic bundle.

The sidecar attaches a ``LogBuffer`` to the root and uvicorn loggers at
startup; the diagnostics endpoint reads its contents into ``sidecar.log``.
"""
from __future__ import annotations

import logging
import os
import threading
from collections import deque
from typing import Deque, Iterable

DEFAULT_CAPACITY_MB = 5


def _resolve_capacity_bytes(default_mb: int = DEFAULT_CAPACITY_MB) -> int:
    raw = os.environ.get("ERRORTA_LOG_CAPTURE_MB")
    if raw:
        try:
            mb = int(raw)
            if mb > 0:
                return mb * 1024 * 1024
        except ValueError:
            pass
    return default_mb * 1024 * 1024


class LogBuffer:
    """Thread-safe ring buffer of log lines capped by total byte size.

    Lines are stored as already-formatted strings (no encoding). When the
    sum of byte lengths would exceed ``capacity_bytes``, oldest lines are
    discarded one at a time until it fits again.
    """

    def __init__(self, capacity_bytes: int | None = None) -> None:
        self._capacity = (
            capacity_bytes if capacity_bytes is not None else _resolve_capacity_bytes()
        )
        self._lines: Deque[str] = deque()
        self._size = 0
        self._lock = threading.Lock()

    @property
    def capacity_bytes(self) -> int:
        return self._capacity

    @property
    def size_bytes(self) -> int:
        with self._lock:
            return self._size

    def append(self, line: str) -> None:
        if not line:
            return
        line_bytes = len(line.encode("utf-8", errors="replace"))
        with self._lock:
            self._lines.append(line)
            self._size += line_bytes
            while self._size > self._capacity and self._lines:
                dropped = self._lines.popleft()
                self._size -= len(dropped.encode("utf-8", errors="replace"))
                if self._size < 0:
                    self._size = 0

    def extend(self, lines: Iterable[str]) -> None:
        for line in lines:
            self.append(line)

    def snapshot(self) -> list[str]:
        with self._lock:
            return list(self._lines)

    def tail(self, n: int) -> list[str]:
        if n <= 0:
            return []
        with self._lock:
            return list(self._lines)[-n:]

    def text(self) -> str:
        return "\n".join(self.snapshot())

    def clear(self) -> None:
        with self._lock:
            self._lines.clear()
            self._size = 0


class _BufferHandler(logging.Handler):
    """``logging.Handler`` that writes formatted records into a ``LogBuffer``."""

    def __init__(self, buffer: LogBuffer) -> None:
        super().__init__()
        self._buffer = buffer

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - trivial
        try:
            msg = self.format(record)
            self._buffer.append(msg)
        except Exception:
            # Never raise from logging.
            pass


def install_buffer(
    buffer: LogBuffer,
    logger: logging.Logger | None = None,
    *,
    level: int = logging.INFO,
    fmt: str = "%(asctime)s %(levelname)s %(name)s: %(message)s",
) -> _BufferHandler:
    """Attach a buffer-backed handler to ``logger`` (default: root)."""
    target = logger if logger is not None else logging.getLogger()
    handler = _BufferHandler(buffer)
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(fmt))
    target.addHandler(handler)
    return handler
