"""F063 A3 — parent-death watchdog."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

from errorta_app.parent_watchdog import parent_alive, start_parent_death_watchdog


def test_parent_alive_true_for_self():
    assert parent_alive(os.getpid()) is True


def test_parent_alive_false_for_dead_pid():
    # Spawn a process, reap it, then its PID should report dead.
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    # The just-exited pid is (almost certainly) free now.
    assert parent_alive(p.pid) is False


def test_no_thread_when_env_unset(monkeypatch):
    monkeypatch.delenv("ERRORTA_PARENT_PID", raising=False)
    assert start_parent_death_watchdog() is None


def test_no_thread_for_invalid_or_init_pid():
    assert start_parent_death_watchdog(parent_pid=1) is None  # init / no real parent
    assert start_parent_death_watchdog(parent_pid="not-a-number") is None  # type: ignore[arg-type]


def test_watchdog_fires_exit_when_parent_dead():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()  # dead pid
    fired = threading.Event()
    stop = threading.Event()

    def fake_exit() -> None:
        fired.set()
        stop.set()  # let the loop end cleanly instead of os._exit

    t = start_parent_death_watchdog(
        parent_pid=p.pid, interval=0.02, exit_fn=fake_exit, stop_event=stop,
    )
    assert t is not None
    assert fired.wait(timeout=3.0), "watchdog did not detect the dead parent"
    t.join(timeout=2.0)


def test_watchdog_does_not_fire_while_parent_alive():
    fired = threading.Event()
    stop = threading.Event()
    t = start_parent_death_watchdog(
        parent_pid=os.getpid(), interval=0.02,
        exit_fn=fired.set, stop_event=stop,
    )
    assert t is not None
    time.sleep(0.15)  # several poll intervals
    stop.set()
    t.join(timeout=2.0)
    assert not fired.is_set(), "watchdog fired while parent (self) was alive"
