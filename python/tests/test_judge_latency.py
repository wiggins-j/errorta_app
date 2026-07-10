"""Tests for errorta_judge.latency.Stopwatch + stopwatch() context manager."""
from __future__ import annotations

import pytest

from errorta_judge import latency
from errorta_judge.latency import Stopwatch, stopwatch


def test_stopwatch_init_starts_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    ticks = iter([100.0, 100.5])

    def fake_monotonic() -> float:
        return next(ticks)

    monkeypatch.setattr(latency.time, "monotonic", fake_monotonic)
    sw = Stopwatch()
    # Pre-stop read uses the second monotonic tick.
    assert sw.elapsed_ms == pytest.approx(500.0)


def test_stopwatch_stop_freezes_elapsed(monkeypatch: pytest.MonkeyPatch) -> None:
    ticks = iter([10.0, 10.25, 99.9, 99.9])

    def fake_monotonic() -> float:
        return next(ticks)

    monkeypatch.setattr(latency.time, "monotonic", fake_monotonic)
    sw = Stopwatch()
    stopped = sw.stop()
    assert stopped == pytest.approx(250.0)
    # Repeated reads after stop must not advance.
    assert sw.elapsed_ms == pytest.approx(250.0)
    assert sw.elapsed_ms == pytest.approx(250.0)


def test_stopwatch_context_manager_stops_on_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    ticks = iter([0.0, 1.0, 2.0, 50.0])

    def fake_monotonic() -> float:
        return next(ticks)

    monkeypatch.setattr(latency.time, "monotonic", fake_monotonic)
    with stopwatch() as sw:
        mid = sw.elapsed_ms
    after = sw.elapsed_ms
    assert mid == pytest.approx(1000.0)
    # On __exit__ stop() ran with the next tick (2.0); subsequent reads return that frozen value.
    assert after == pytest.approx(2000.0)


def test_stopwatch_context_stops_even_on_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    ticks = iter([0.0, 0.5, 5.0])

    def fake_monotonic() -> float:
        return next(ticks)

    monkeypatch.setattr(latency.time, "monotonic", fake_monotonic)
    captured: list[Stopwatch] = []
    with pytest.raises(RuntimeError):
        with stopwatch() as sw:
            captured.append(sw)
            raise RuntimeError("boom")
    # finally branch stopped the watch at tick 0.5.
    assert captured[0].elapsed_ms == pytest.approx(500.0)
