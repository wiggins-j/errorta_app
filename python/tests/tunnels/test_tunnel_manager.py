"""F089 Slice 0 — SSH tunnel manager: argv hardening, validation, lifecycle.

The lifecycle tests never spawn a real ssh: a fake spawn opens a dummy 127.0.0.1
listener on the requested local port and returns a fake child, so ensure()/the
watcher/reconnect/teardown are exercised without touching the network.
"""
from __future__ import annotations

import socket
import threading
import time

import pytest

from errorta_tunnels import (
    STATE_UP,
    TunnelManager,
    TunnelSpec,
    TunnelValidationError,
    build_ssh_argv,
)
from errorta_tunnels.manager import _alloc_local_port

# --- argv + validation ------------------------------------------------------

def test_argv_has_all_hardening_flags() -> None:
    spec = TunnelSpec(ssh_host="example-host", remote_port=8766).validated()
    argv = build_ssh_argv(spec, 50000)
    joined = " ".join(argv)
    assert "-N" in argv
    for flag in (
        "BatchMode=yes", "ExitOnForwardFailure=yes", "ServerAliveInterval=15",
        "ServerAliveCountMax=3", "ConnectTimeout=10", "StrictHostKeyChecking=accept-new",
    ):
        assert flag in joined
    # Loopback-only forward + host last.
    assert "-L" in argv and "127.0.0.1:50000:127.0.0.1:8766" in argv
    assert argv[-1] == "example-host"


def test_argv_includes_overrides_only_when_set(tmp_path) -> None:
    key = tmp_path / "id_ed25519"
    key.write_text("x")
    spec = TunnelSpec(
        ssh_host="host", remote_port=9000, remote_host="127.0.0.1",
        ssh_port=2222, ssh_username="me", ssh_key_path=str(key)).validated()
    argv = build_ssh_argv(spec, 51000)
    assert "-p" in argv and "2222" in argv
    assert "-i" in argv and str(key) in argv
    assert argv[-1] == "me@host"


def test_validation_rejects_flag_injection_and_bad_inputs(tmp_path) -> None:
    with pytest.raises(TunnelValidationError):
        TunnelSpec(ssh_host="-oProxyCommand=evil", remote_port=8766).validated()
    with pytest.raises(TunnelValidationError):
        TunnelSpec(ssh_host="ok", remote_port=70000).validated()
    with pytest.raises(TunnelValidationError):
        TunnelSpec(ssh_host="ok", remote_port=22, remote_host="a b").validated()
    with pytest.raises(TunnelValidationError):
        TunnelSpec(ssh_host="ok", remote_port=22,
                   ssh_key_path=str(tmp_path / "missing")).validated()


# --- lifecycle with a fake ssh ---------------------------------------------

class _FakeListener:
    """A dummy TCP listener standing in for the local end of an ssh forward."""

    def __init__(self, port: int) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", port))
        self._sock.listen(8)
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._serve, daemon=True)
        self._t.start()

    def _serve(self) -> None:
        self._sock.settimeout(0.2)
        while not self._stop.is_set():
            try:
                c, _ = self._sock.accept()
                c.close()
            except OSError:
                continue

    def close(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass


class _FakeChild:
    """Stands in for the ssh subprocess: opens the forward's local listener."""

    def __init__(self, argv: list[str]) -> None:
        # Parse the -L 127.0.0.1:<local>:... to learn which port to open.
        i = argv.index("-L")
        local_port = int(argv[i + 1].split(":")[1])
        self._listener = _FakeListener(local_port)
        self._dead = threading.Event()

    def poll(self):
        return 1 if self._dead.is_set() else None

    def die(self) -> None:
        self._listener.close()
        self._dead.set()

    def kill(self) -> None:
        self._listener.close()
        self._dead.set()

    def stderr_tail(self) -> str:
        return "fake ssh exited"


def _fast_manager() -> tuple[TunnelManager, list[_FakeChild]]:
    children: list[_FakeChild] = []

    def spawn(argv):
        c = _FakeChild(argv)
        children.append(c)
        return c

    return TunnelManager(spawn=spawn, watch_interval=0.05, connect_budget=3.0), children


def _wait(predicate, *, timeout=4.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.03)
    return False


def test_ensure_brings_tunnel_up_and_returns_stable_port() -> None:
    mgr, _ = _fast_manager()
    spec = TunnelSpec(ssh_host="example-host", remote_port=8766)
    try:
        port = mgr.ensure(spec)
        assert 1 <= port <= 65535
        assert mgr.status_for(spec)["state"] == STATE_UP
        # Idempotent: same spec -> same shared port, one child.
        assert mgr.ensure(spec) == port
    finally:
        mgr.teardown()


def test_dedup_shares_one_child() -> None:
    mgr, children = _fast_manager()
    spec = TunnelSpec(ssh_host="h", remote_port=8766)
    try:
        mgr.ensure(spec)
        mgr.ensure(spec)
        assert len(children) == 1  # deduped
    finally:
        mgr.teardown()


def test_watcher_reconnects_on_child_death_same_port() -> None:
    mgr, children = _fast_manager()
    spec = TunnelSpec(ssh_host="h", remote_port=8766)
    try:
        port = mgr.ensure(spec)
        assert _wait(lambda: mgr.status_for(spec)["state"] == STATE_UP)
        children[0].die()  # link drops
        # Watcher respawns on the SAME local port and returns to UP.
        assert _wait(lambda: len(children) >= 2, timeout=5)
        assert _wait(lambda: mgr.status_for(spec)["state"] == STATE_UP, timeout=5)
        assert mgr.status_for(spec)["local_port"] == port
    finally:
        mgr.teardown()


def test_reconnect_forces_respawn() -> None:
    mgr, children = _fast_manager()
    spec = TunnelSpec(ssh_host="h", remote_port=8766)
    try:
        mgr.ensure(spec)
        assert _wait(lambda: mgr.status_for(spec)["state"] == STATE_UP)
        assert mgr.reconnect(spec) is True
        assert _wait(lambda: len(children) >= 2, timeout=5)
        assert mgr.reconnect(TunnelSpec(ssh_host="other", remote_port=1)) is False
    finally:
        mgr.teardown()


def test_teardown_kills_children_and_clears() -> None:
    mgr, children = _fast_manager()
    spec = TunnelSpec(ssh_host="h", remote_port=8766)
    mgr.ensure(spec)
    assert _wait(lambda: mgr.status_for(spec)["state"] == STATE_UP)
    mgr.teardown()
    assert mgr.status() == []
    assert all(c.poll() is not None for c in children)  # every child killed
    # The local port is free again after teardown.
    p = _alloc_local_port()
    assert 1 <= p <= 65535
