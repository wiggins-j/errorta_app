"""Tests for errorta_watch.coordinator.

These tests assert the coordinator starts/stops cleanly with no
daemon-thread leaks — active_count() must return to its baseline.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Iterator

import pytest

from errorta_watch import coordinator as coord_mod
from errorta_watch.coordinator import WatchCoordinator, get_coordinator
from errorta_watch.state import save_state, WatchState


@pytest.fixture
def coordinator(tmp_errorta_home: Path) -> Iterator[WatchCoordinator]:
    c = WatchCoordinator()
    try:
        yield c
    finally:
        c.shutdown()
        # Give threads a moment to drain.
        time.sleep(0.05)


def _wait_join(target_count: int, timeout: float = 2.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        cur = threading.active_count()
        if cur <= target_count:
            return cur
        time.sleep(0.05)
    return threading.active_count()


def test_start_then_stop_no_thread_leak(coordinator: WatchCoordinator, tmp_path: Path) -> None:
    baseline = threading.active_count()
    watched = tmp_path / "watched"
    watched.mkdir()

    coordinator.start("corp1", str(watched))
    assert coordinator.status("corp1")["alive"] is True

    assert coordinator.stop("corp1") is True
    after = _wait_join(baseline)
    assert after <= baseline, f"thread leak: baseline={baseline} after={after}"


def test_shutdown_stops_all(coordinator: WatchCoordinator, tmp_path: Path) -> None:
    baseline = threading.active_count()
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    coordinator.start("a", str(tmp_path / "a"))
    coordinator.start("b", str(tmp_path / "b"))
    coordinator.shutdown()
    after = _wait_join(baseline)
    assert after <= baseline


def test_start_twice_raises(coordinator: WatchCoordinator, tmp_path: Path) -> None:
    (tmp_path / "x").mkdir()
    coordinator.start("x", str(tmp_path / "x"))
    with pytest.raises(ValueError):
        coordinator.start("x", str(tmp_path / "x"))


def test_stop_unknown_returns_false(coordinator: WatchCoordinator) -> None:
    assert coordinator.stop("never-started") is False


def test_pause_resume(coordinator: WatchCoordinator, tmp_path: Path) -> None:
    (tmp_path / "pp").mkdir()
    coordinator.start("pp", str(tmp_path / "pp"))
    assert coordinator.pause("pp") is True
    assert coordinator.resume("pp") is True
    assert coordinator.pause("missing") is False
    assert coordinator.resume("missing") is False


def test_status_unknown_returns_not_watching(coordinator: WatchCoordinator) -> None:
    out = coordinator.status("ghost")
    assert out["corpus"] == "ghost"
    assert out["watching"] is False


def test_set_deletion_policy_invalid_raises(
    coordinator: WatchCoordinator, tmp_path: Path
) -> None:
    (tmp_path / "d").mkdir()
    coordinator.start("d", str(tmp_path / "d"))
    with pytest.raises(ValueError):
        coordinator.set_deletion_policy("d", "explode")


def test_change_path_no_state_raises(coordinator: WatchCoordinator, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        coordinator.change_path("never", str(tmp_path))


def test_restore_from_disk_restarts_persisted(
    coordinator: WatchCoordinator, tmp_path: Path
) -> None:
    watched = tmp_path / "restored"
    watched.mkdir()
    save_state(
        WatchState(
            corpus="restored",
            watched_path=str(watched),
            started_at="2026-06-07T00:00:00+00:00",
        )
    )
    restored = coordinator.restore_from_disk()
    assert "restored" in restored
    assert coordinator.status("restored")["alive"] is True

    # Idempotent: a second call does not double-spawn.
    again = coordinator.restore_from_disk()
    assert again == []
    assert len(coordinator._pollers) == 1


def test_get_coordinator_returns_singleton(
    tmp_errorta_home: Path,
    watch_coordinator_cleanup: None,
) -> None:
    # Reset the module-level singleton so this test is hermetic.
    coord_mod._coordinator = None
    try:
        a = get_coordinator()
        b = get_coordinator()
        assert a is b
        assert isinstance(a, WatchCoordinator)
    finally:
        c = coord_mod._coordinator
        if c is not None:
            c.shutdown()
        coord_mod._coordinator = None


def test_pause_resume_does_not_restart_thread(
    coordinator: WatchCoordinator, tmp_path: Path
) -> None:
    watched = tmp_path / "pr"
    watched.mkdir()
    coordinator.start("pr", str(watched))
    poller = coordinator._pollers["pr"]
    thread_before = poller._thread
    assert thread_before is not None and thread_before.is_alive()

    coordinator.pause("pr")
    assert coordinator.status("pr")["paused"] is True
    coordinator.resume("pr")
    assert coordinator.status("pr")["paused"] is False

    # Same thread — pause/resume is an in-place toggle, not a restart.
    assert poller._thread is thread_before
    assert thread_before.is_alive()


def test_ingest_hook_invoked_once_per_new_file(
    tmp_errorta_home: Path,
    tmp_path: Path,
    watch_coordinator_cleanup: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    watched = tmp_path / "ingest"
    watched.mkdir()
    (watched / "a.txt").write_text("alpha", encoding="utf-8")
    (watched / "b.txt").write_text("beta", encoding="utf-8")

    calls: list[str] = []
    call_lock = threading.Lock()

    def hook(_corpus: str, path: str) -> dict:
        import os as _os
        with call_lock:
            calls.append(path)
        st = _os.stat(path)
        return {
            "mtime": st.st_mtime,
            "size": st.st_size,
            "sha256": "stub-" + _os.path.basename(path),
            "file_id": "stub:" + _os.path.basename(path),
            "chunk_ids": [],
        }

    # The coordinator captures ingest_via_pipeline at module-import time as
    # the default ingest hook for each new poller. Patch it before starting.
    monkeypatch.setattr(coord_mod, "ingest_via_pipeline", hook)

    c = WatchCoordinator()
    try:
        c.start("ingest", str(watched))

        # Initial scan ran synchronously inside start() — one call per file.
        assert sorted(calls) == sorted([
            str(watched / "a.txt"),
            str(watched / "b.txt"),
        ])
        assert len(calls) == 2

        # A second reconciliation pass with no changes must not re-invoke.
        c._pollers["ingest"].run_once()
        assert len(calls) == 2
    finally:
        c.shutdown()
