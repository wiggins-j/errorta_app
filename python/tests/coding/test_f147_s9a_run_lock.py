"""F147 S9a — cross-process run lock + owner-liveness coordination.

Two mechanisms cooperate to make the run lifecycle safe across two sidecars on
one ERRORTA_HOME:

* ``cross_process_lock`` — an ``fcntl.flock`` that serializes the run critical
  section + the recovery transition across processes (reentrant within a
  process; Windows-fallback degrades to process-local).
* ``run_owned_by_live_process`` — the ``owner_pid`` liveness predicate that makes
  the start-guard 409 and ``GET /run`` recovery stand down when a run is live in
  another sidecar.

These tests simulate "another process" with a raw second ``flock`` handle on the
same lock file (the module allows real subprocesses or two flock handles).
"""
from __future__ import annotations

import os

import pytest

from errorta_council.coding import locks

_HAVE_FCNTL = getattr(locks, "_fcntl", None) is not None
pytestmark = pytest.mark.skipif(
    not _HAVE_FCNTL, reason="fcntl unavailable (cross-process lock is a no-op)"
)


def _hold_foreign_flock(lock_path):
    """Return an fd holding an exclusive flock on ``lock_path`` (a stand-in for a
    live run in ANOTHER process)."""
    import fcntl

    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def _release(fd) -> None:
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)


# --- cross_process_lock ------------------------------------------------------

def test_reentrant_same_thread_no_deadlock(tmp_path) -> None:
    d = tmp_path / "proj"
    d.mkdir()
    with locks.cross_process_lock(d):
        with locks.cross_process_lock(d):  # nested — must not deadlock/re-flock
            pass


def test_second_holder_is_blocked(tmp_path) -> None:
    d = tmp_path / "proj"
    d.mkdir()
    fd = _hold_foreign_flock(str(d / ".run.lock"))
    try:
        with pytest.raises(locks.RunLockBusy):
            with locks.cross_process_lock(d, timeout=0.2):
                pass
    finally:
        _release(fd)
    # Free again once the foreign holder releases.
    with locks.cross_process_lock(d, timeout=0.5):
        pass


def test_windows_fallback_degrades_to_process_local(tmp_path, monkeypatch) -> None:
    """With no fcntl, cross_process_lock still works (in-process guard only) and a
    foreign flock does NOT block it — the documented graceful degradation."""
    monkeypatch.setattr(locks, "_fcntl", None)
    monkeypatch.setattr(locks, "_WIN_FALLBACK_WARNED", False)
    # Fresh dir so we don't reuse a cached composite that already opened an fd.
    d = tmp_path / "winproj"
    d.mkdir()
    with locks.cross_process_lock(d, timeout=0.1):
        pass
    assert locks._WIN_FALLBACK_WARNED is True


# --- run_owned_by_live_process ----------------------------------------------

def test_owner_predicate_matrix() -> None:
    me = os.getpid()
    alive = lambda p: True  # noqa: E731
    dead = lambda p: False  # noqa: E731
    running = {"status": "running", "owner_pid": 4321}

    # our own pid -> not "another" process
    assert locks.run_owned_by_live_process(
        {"status": "running", "owner_pid": me}, my_pid=me, alive_fn=alive) is False
    # different, alive -> owned live elsewhere
    assert locks.run_owned_by_live_process(running, my_pid=me, alive_fn=alive) is True
    # different, dead -> not owned
    assert locks.run_owned_by_live_process(running, my_pid=me, alive_fn=dead) is False
    # not running -> never owned-live
    assert locks.run_owned_by_live_process(
        {"status": "stopped", "owner_pid": 4321}, my_pid=me, alive_fn=alive) is False
    # missing / malformed owner_pid -> False
    assert locks.run_owned_by_live_process(
        {"status": "running"}, my_pid=me, alive_fn=alive) is False
    assert locks.run_owned_by_live_process(
        {"status": "running", "owner_pid": "x"}, my_pid=me, alive_fn=alive) is False


# --- reconcile inhibition (route-level, single interpreter) ------------------

def _mk_running_project(project_id: str, *, owner_pid: int):
    from errorta_council.coding.ledger import LedgerStore

    store = LedgerStore(project_id)
    store.create_project(north_star="n", definition_of_done="d", target="new",
                         repo_path=None)
    store.set_run_state(status="running", owner_pid=owner_pid)
    return store


def test_reconcile_inhibited_when_owner_alive_elsewhere(
    tmp_errorta_home, monkeypatch
) -> None:
    """A run whose owner_pid is a live OTHER process is NOT reconciled to
    interrupted by GET /run recovery."""
    from errorta_app.routes import coding as coding_routes

    store = _mk_running_project("plive", owner_pid=999123)
    # No live worker thread in THIS process; owner appears alive.
    monkeypatch.setattr("errorta_app.parent_watchdog.parent_alive", lambda p: True)
    state = coding_routes._reconcile_run_state("plive", store)
    assert state["status"] == "running"  # recovery stood down


def test_reconcile_fires_when_owner_dead(tmp_errorta_home, monkeypatch) -> None:
    """A genuinely orphaned run (owner_pid dead, no live thread) IS reconciled —
    the pre-S9a behavior is preserved for real orphans."""
    from errorta_app.routes import coding as coding_routes

    store = _mk_running_project("pdead", owner_pid=999124)
    monkeypatch.setattr("errorta_app.parent_watchdog.parent_alive", lambda p: False)
    state = coding_routes._reconcile_run_state("pdead", store)
    assert state["status"] == "interrupted"


def test_reconcile_inhibited_while_lock_held(tmp_errorta_home, monkeypatch) -> None:
    """While ANOTHER process holds the run lock, reconcile skips recovery entirely
    (never mutates), even for an owner that looks dead."""
    from errorta_app.routes import coding as coding_routes

    store = _mk_running_project("pheld", owner_pid=999125)
    monkeypatch.setattr("errorta_app.parent_watchdog.parent_alive", lambda p: False)
    monkeypatch.setattr(coding_routes, "_RECONCILE_LOCK_TIMEOUT_SECONDS", 0.2)
    fd = _hold_foreign_flock(str(store.dir / ".run.lock"))
    try:
        state = coding_routes._reconcile_run_state("pheld", store)
        assert state["status"] == "running"  # recovery inhibited (lock busy)
    finally:
        _release(fd)


# --- start critical section 409 across processes ----------------------------

def test_run_critical_section_409_when_lock_held(tmp_errorta_home, monkeypatch) -> None:
    """The start critical section 409s when another process holds the run lock."""
    from fastapi import HTTPException

    from errorta_app.routes import coding as coding_routes
    from errorta_council.coding.ledger import LedgerStore

    store = LedgerStore("pstart")
    store.create_project(north_star="n", definition_of_done="d", target="new",
                         repo_path=None)
    monkeypatch.setattr(coding_routes, "_RUN_LOCK_TIMEOUT_SECONDS", 0.2)
    fd = _hold_foreign_flock(str(store.dir / ".run.lock"))
    try:
        with pytest.raises(HTTPException) as ei:
            with coding_routes._run_critical_section(store):
                pass
        assert ei.value.status_code == 409
        assert ei.value.detail == "a run is already in progress"
    finally:
        _release(fd)
