"""Tiny stopwatch helper used by the judge router to fill verdict.latency_ms."""
from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Iterator


class Stopwatch:
    """Monotonic ms stopwatch. ``elapsed_ms`` reads safe pre- and post-stop."""

    __slots__ = ("_start", "_end")

    def __init__(self) -> None:
        self._start: float = time.monotonic()
        self._end: float | None = None

    def stop(self) -> float:
        self._end = time.monotonic()
        return self.elapsed_ms

    @property
    def elapsed_ms(self) -> float:
        end = self._end if self._end is not None else time.monotonic()
        return (end - self._start) * 1000.0


@contextmanager
def stopwatch() -> Iterator[Stopwatch]:
    sw = Stopwatch()
    try:
        yield sw
    finally:
        sw.stop()
