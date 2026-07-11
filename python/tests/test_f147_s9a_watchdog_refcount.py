"""F147 S9a — parent-death watchdog reference counting.

Covers the pure ``should_exit`` policy, the client pidfile registry, and the
grace-aware loop: a shared sidecar survives while ANY client is alive and exits
only once none is alive and the grace elapses. The solo (single-parent) default
still exits promptly.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from errorta_app import parent_watchdog as pw

# --- pure policy -------------------------------------------------------------

def test_should_exit_survives_while_any_client_alive() -> None:
    alive = {10, 11}
    assert pw.should_exit([10, 11, 12], lambda p: p in alive) is False
    # only one alive -> still survive
    assert pw.should_exit([12, 11], lambda p: p in alive) is False


def test_should_exit_when_no_client_alive() -> None:
    assert pw.should_exit([12, 13], lambda p: False) is True


def test_should_exit_empty_set_is_true() -> None:
    # vacuously: nothing keeps us up
    assert pw.should_exit([], lambda p: True) is True


# --- registry ----------------------------------------------------------------

def test_register_unregister_client(tmp_errorta_home: Path) -> None:
    assert pw.register_client(4242) == 4242
    assert 4242 in pw.registered_client_pids()
    pw.unregister_client(4242)
    assert 4242 not in pw.registered_client_pids()


def test_registered_ignores_malformed_entries(tmp_errorta_home: Path) -> None:
    d = pw.clients_dir()
    (d / "not-a-pid").write_text("x", encoding="utf-8")
    (d / "77").write_text("77", encoding="utf-8")
    assert pw.registered_client_pids() == {77}


# --- loop --------------------------------------------------------------------

def test_loop_no_thread_when_nothing_to_watch(monkeypatch) -> None:
    monkeypatch.delenv("ERRORTA_PARENT_PID", raising=False)
    t = pw.start_parent_death_watchdog(
        parent_pid=None, clients_fn=lambda: set(), interval=0.01)
    assert t is None


def test_loop_survives_while_client_alive_then_exits_after_grace() -> None:
    exited = threading.Event()
    stop = threading.Event()
    state = {"alive": True}

    t = pw.start_parent_death_watchdog(
        parent_pid=999999,  # a "client" pid we control via alive_fn
        clients_fn=lambda: {999999},
        alive_fn=lambda p: state["alive"],
        interval=0.02,
        grace=0.08,
        exit_fn=exited.set,
        stop_event=stop,
    )
    assert t is not None
    try:
        # Still alive: must NOT exit.
        assert not exited.wait(0.15)
        # Client dies -> after grace, exit fires.
        state["alive"] = False
        assert exited.wait(1.0)
    finally:
        stop.set()


def test_loop_grace_zero_exits_promptly_solo_case() -> None:
    """Default grace=0 preserves the pre-S9a 'parent dies -> exit promptly' UX."""
    exited = threading.Event()
    stop = threading.Event()

    t = pw.start_parent_death_watchdog(
        parent_pid=999998,
        clients_fn=lambda: {999998},
        alive_fn=lambda p: False,  # already gone
        interval=0.02,
        grace=0.0,
        exit_fn=exited.set,
        stop_event=stop,
    )
    assert t is not None
    try:
        assert exited.wait(1.0)
    finally:
        stop.set()


def test_loop_recovers_if_client_returns_within_grace() -> None:
    """A transient zero-live-clients window inside the grace does NOT exit."""
    exited = threading.Event()
    stop = threading.Event()
    t0 = time.monotonic()

    def alive(_pid: int) -> bool:
        # dead for the first ~60ms, then alive again — a fast handoff.
        return (time.monotonic() - t0) > 0.06

    t = pw.start_parent_death_watchdog(
        parent_pid=999997,
        clients_fn=lambda: {999997},
        alive_fn=alive,
        interval=0.02,
        grace=0.2,
        exit_fn=exited.set,
        stop_event=stop,
    )
    assert t is not None
    try:
        assert not exited.wait(0.4)
    finally:
        stop.set()
