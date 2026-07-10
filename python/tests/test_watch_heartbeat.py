"""F005-PROD tests: heartbeat, stale detection, ingest retry backpressure."""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest

from errorta_watch import coordinator as coord_mod
from errorta_watch import poller as poller_mod
from errorta_watch.coordinator import (
    HEARTBEAT_STALE_SECONDS,
    WatchCoordinator,
    _heartbeat_age_seconds,
    _state_summary,
)
from errorta_watch.poller import FolderPoller
from errorta_watch.state import WatchState, load_state


def _make_state(corpus: str, watched: Path) -> WatchState:
    return WatchState(
        corpus=corpus,
        watched_path=str(watched),
        started_at="2026-06-07T00:00:00+00:00",
    )


def test_heartbeat_set_on_successful_run_once(
    tmp_errorta_home: Path, tmp_path: Path
) -> None:
    watched = tmp_path / "hb"
    watched.mkdir()
    (watched / "a.txt").write_text("hello", encoding="utf-8")

    state = _make_state("hb", watched)
    p = FolderPoller(state, ingest_hook=lambda _c, _p: {})
    assert state.last_heartbeat is None

    p.run_once()
    assert state.last_heartbeat is not None
    # Heartbeat must round-trip through disk.
    loaded = load_state("hb")
    assert loaded is not None
    assert loaded.last_heartbeat == state.last_heartbeat


def test_stale_detection_after_simulated_time_advance(
    tmp_errorta_home: Path, tmp_path: Path
) -> None:
    watched = tmp_path / "stale"
    watched.mkdir()
    state = _make_state("stale", watched)
    # Heartbeat from well before the stale threshold.
    old = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(
        seconds=HEARTBEAT_STALE_SECONDS + 30
    )
    state.last_heartbeat = old.isoformat(timespec="seconds")

    summary = _state_summary(state, alive=True)
    assert summary["stale"] is True
    assert summary["heartbeat_age_seconds"] is not None
    assert summary["heartbeat_age_seconds"] > HEARTBEAT_STALE_SECONDS

    # Fresh heartbeat → not stale.
    state.last_heartbeat = _dt.datetime.now(_dt.timezone.utc).isoformat(
        timespec="seconds"
    )
    summary2 = _state_summary(state, alive=True)
    assert summary2["stale"] is False

    # Dead watcher is never "stale" — alive=False shadows the check.
    state.last_heartbeat = old.isoformat(timespec="seconds")
    summary3 = _state_summary(state, alive=False)
    assert summary3["stale"] is False


def test_heartbeat_age_helper_handles_none_and_bad_input() -> None:
    assert _heartbeat_age_seconds(None) is None
    assert _heartbeat_age_seconds("not-a-timestamp") is None


def test_ingest_retry_then_fail_preserves_replay_state(
    tmp_errorta_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed ingest must NOT advance manifest mtime/size — so the next poll retries."""
    # Make the backoff sleeps instant so the test stays fast.
    monkeypatch.setattr(poller_mod.time, "sleep", lambda _s: None)

    watched = tmp_path / "ret"
    watched.mkdir()
    src = watched / "doc.txt"
    src.write_text("payload", encoding="utf-8")

    calls: list[str] = []

    def always_fail(_corpus: str, path: str) -> dict:
        calls.append(path)
        raise RuntimeError("simulated ingest failure")

    state = _make_state("ret", watched)
    p = FolderPoller(state, ingest_hook=always_fail)
    summary = p.run_once()

    # 3 attempts on the one file.
    assert len(calls) == 3
    assert summary["ingest_failures"] == 1
    assert state.last_scan_ok is False
    assert state.last_error is not None
    assert "simulated ingest failure" in state.last_error

    # Manifest entry exists but is marked replay-ready: mtime=0/size=0/missing.
    entry = state.manifest[str(src)]
    assert entry.source_missing is True
    assert entry.mtime == 0.0
    assert entry.size == 0

    # Second pass with hook still failing must re-attempt (changed=True because
    # st.mtime > 0).
    calls.clear()
    p.run_once()
    assert len(calls) == 3, "manifest replay state lost — next poll did not retry"


def test_force_rescan_runs_immediately(
    tmp_errorta_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    watched = tmp_path / "force"
    watched.mkdir()

    # Use a no-op ingest so the test is hermetic.
    monkeypatch.setattr(coord_mod, "ingest_via_pipeline", lambda _c, _p: {})
    c = WatchCoordinator()
    try:
        c.start("force", str(watched))
        # Add a file after the initial scan; force_rescan must pick it up.
        (watched / "new.txt").write_text("x", encoding="utf-8")
        status = c.force_rescan("force")
        assert status["file_count"] == 1
        assert status["last_heartbeat"] is not None
    finally:
        c.shutdown()


def test_force_rescan_unknown_corpus_raises(tmp_errorta_home: Path) -> None:
    c = WatchCoordinator()
    try:
        with pytest.raises(ValueError):
            c.force_rescan("nope")
    finally:
        c.shutdown()
