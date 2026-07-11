"""CLI-owned single-instance lifecycle: adopt-vs-spawn, sidecar.json, foreign.

The HTTP probe (``probe_healthz``) and the process launch (``_launch``) are
monkeypatched so the decision logic and the foreign-app refusal are exercised
without a real sidecar or a real subprocess. One test that needs the REAL
sidecar to boot (``__serve__`` → uvicorn) is skipped when the engine stack isn't
importable, mirroring ``python/tests/test_sidecar_boot_smoke.py``.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from errorta_cli import sidecar
from errorta_cli.errors import ForeignSidecar, SidecarUnreachable

# Mirror the boot-smoke skip pattern: only the real-boot test needs these.
try:
    import uvicorn  # noqa: F401

    import errorta_app.server  # noqa: F401

    _REAL_SIDECAR = True
except Exception:  # ImportError or a heavy transitive failure
    _REAL_SIDECAR = False


class _FakeProc:
    def __init__(self, pid: int = 4321) -> None:
        self.pid = pid
        self.returncode = None

    def poll(self):  # still running
        return None


# --------------------------------------------------------------------------- #
# sidecar.json read/write.
# --------------------------------------------------------------------------- #

def test_record_roundtrip(tmp_path: Path) -> None:
    assert sidecar.read_record(tmp_path) is None
    record = {"port": 5555, "pid": 99, "commit": "abc", "started_by": "cli"}
    sidecar.write_record(tmp_path, record)
    assert sidecar.read_record(tmp_path) == record
    sidecar.clear_record(tmp_path)
    assert sidecar.read_record(tmp_path) is None


# --------------------------------------------------------------------------- #
# resolve(): adopt vs spawn.
# --------------------------------------------------------------------------- #

def test_spawn_when_no_record(monkeypatch, tmp_path: Path) -> None:
    launched: dict[str, object] = {}

    def fake_launch(argv, env):
        launched["argv"] = argv
        launched["env"] = env
        launched["port"] = int(env["ERRORTA_SIDECAR_PORT"])
        return _FakeProc(pid=4321)

    monkeypatch.setattr(sidecar, "_launch", fake_launch)
    # Answer /healthz only on the freshly-spawned port so the pre-spawn
    # foreign-app probe of 8770 stays negative.
    monkeypatch.setattr(
        sidecar,
        "probe_healthz",
        lambda port, **k: {"build": {"commit": "abc"}}
        if port == launched.get("port")
        else None,
    )
    monkeypatch.setattr(sidecar, "_scan_errorta_processes", lambda **k: [])

    handle = sidecar.resolve(tmp_path, our_commit="abc")

    assert handle.adopted is False
    assert handle.started_by == "cli"
    record = sidecar.read_record(tmp_path)
    assert record["started_by"] == "cli"
    assert record["pid"] == 4321
    # ERRORTA_HOME + a chosen port were handed to the child.
    assert launched["env"]["ERRORTA_HOME"] == str(tmp_path)
    assert "ERRORTA_SIDECAR_PORT" in launched["env"]


def test_adopt_live_cli_record_does_not_spawn(monkeypatch, tmp_path: Path) -> None:
    sidecar.write_record(
        tmp_path, {"port": 5555, "pid": 77, "commit": "abc", "started_by": "cli"}
    )
    monkeypatch.setattr(
        sidecar,
        "probe_healthz",
        lambda port, **k: {"build": {"commit": "abc"}} if port == 5555 else None,
    )

    def boom(*a, **k):
        raise AssertionError("resolve must adopt, not spawn")

    monkeypatch.setattr(sidecar, "_launch", boom)

    handle = sidecar.resolve(tmp_path, our_commit="abc")
    assert handle.adopted is True
    assert handle.port == 5555
    assert handle.commit_mismatch is False


def test_adopt_flags_commit_mismatch(monkeypatch, tmp_path: Path) -> None:
    # Our OWN prior cli sidecar with a mismatched (upgraded) commit: still adopt
    # with a warning — the CLI is sole owner, so co-drive is not in play.
    sidecar.write_record(
        tmp_path, {"port": 5555, "pid": 77, "commit": "old", "started_by": "cli"}
    )
    monkeypatch.setattr(
        sidecar, "probe_healthz", lambda port, **k: {"build": {"commit": "old"}}
    )
    monkeypatch.setattr(sidecar, "_launch", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    handle = sidecar.resolve(tmp_path, our_commit="new")
    assert handle.adopted is True
    assert handle.commit_mismatch is True


def test_adopt_app_started_matching_sidecar_does_not_spawn(
    monkeypatch, tmp_path: Path
) -> None:
    """S9b co-drive: the CLI adopts the desktop APP's sidecar (started_by=app)
    when its build commit matches — one shared, coordinated sidecar, no spawn."""
    sidecar.write_record(
        tmp_path, {"port": 5555, "pid": 77, "commit": "abc", "started_by": "app"}
    )
    monkeypatch.setattr(
        sidecar,
        "probe_healthz",
        lambda port, **k: {"build": {"commit": "abc"}} if port == 5555 else None,
    )
    def _no_spawn(*a, **k):
        raise AssertionError("must adopt, not spawn")

    monkeypatch.setattr(sidecar, "_launch", _no_spawn)

    handle = sidecar.resolve(tmp_path, our_commit="abc")

    assert handle.adopted is True
    assert handle.started_by == "app"
    assert handle.port == 5555
    assert handle.commit_mismatch is False


def test_refuses_version_skewed_app_sidecar(monkeypatch, tmp_path: Path) -> None:
    """A live but VERSION-SKEWED app sidecar is neither adopted nor spawned-next-
    to: we can't confirm a coordinated build → refuse (safe fallback)."""
    sidecar.write_record(
        tmp_path, {"port": 5555, "pid": 77, "commit": "old", "started_by": "app"}
    )
    monkeypatch.setattr(
        sidecar, "probe_healthz", lambda port, **k: {"build": {"commit": "old"}}
    )
    monkeypatch.setattr(
        sidecar, "_launch", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not spawn"))
    )
    with pytest.raises(ForeignSidecar):
        sidecar.resolve(tmp_path, our_commit="new")


def test_refuses_unknown_commit_foreign_sidecar(monkeypatch, tmp_path: Path) -> None:
    """LOW-3 safe fallback: a FOREIGN (app-started) sidecar whose build commit is
    UNKNOWN can't be positively confirmed as coordinated, so it is NOT adopted —
    it refuses (mirrors the Rust app's empty-commit → never-adopt). Previously the
    ``not mismatch`` rule adopted an unknown-commit foreign sidecar (unsafe)."""
    sidecar.write_record(
        tmp_path, {"port": 5555, "pid": 77, "commit": None, "started_by": "app"}
    )
    # Live /healthz but no build.commit → commit is unknown on the advert side.
    monkeypatch.setattr(
        sidecar, "probe_healthz", lambda port, **k: {"service": "errorta-sidecar"}
    )
    monkeypatch.setattr(
        sidecar, "_launch", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not spawn"))
    )
    with pytest.raises(ForeignSidecar):
        sidecar.resolve(tmp_path, our_commit="abc")


def test_refuses_foreign_sidecar_when_our_commit_unknown(
    monkeypatch, tmp_path: Path
) -> None:
    """LOW-3: unknown commit on OUR side also can't confirm a match against a
    foreign app sidecar → refuse (not adopt)."""
    sidecar.write_record(
        tmp_path, {"port": 5555, "pid": 77, "commit": "abc", "started_by": "app"}
    )
    monkeypatch.setattr(
        sidecar, "probe_healthz", lambda port, **k: {"build": {"commit": "abc"}}
    )
    monkeypatch.setattr(
        sidecar, "_launch", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not spawn"))
    )
    with pytest.raises(ForeignSidecar):
        sidecar.resolve(tmp_path, our_commit=None)


def test_adopts_own_cli_sidecar_with_unknown_commit(monkeypatch, tmp_path: Path) -> None:
    """LOW-3: our OWN cli-started sidecar stays adoptable even when the build
    commit is unknown — a sidecar this CLI started is coordinated by construction
    (sole owner), so the stricter foreign rule must NOT apply to it."""
    sidecar.write_record(
        tmp_path, {"port": 5555, "pid": 77, "commit": None, "started_by": "cli"}
    )
    # Live but reports no build.commit (unknown on both sides).
    monkeypatch.setattr(
        sidecar, "probe_healthz", lambda port, **k: {"service": "errorta-sidecar"}
    )
    monkeypatch.setattr(
        sidecar, "_launch", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not spawn"))
    )
    handle = sidecar.resolve(tmp_path, our_commit=None)
    assert handle.adopted is True
    assert handle.started_by == "cli"
    assert handle.port == 5555


def test_adopt_registers_client_pidfile(monkeypatch, tmp_path: Path) -> None:
    """Adopting a shared sidecar registers this CLI as a watchdog client so the
    sidecar refcounts us and doesn't exit under an in-flight run."""
    sidecar.write_record(
        tmp_path, {"port": 5555, "pid": 77, "commit": "abc", "started_by": "app"}
    )
    monkeypatch.setattr(
        sidecar, "probe_healthz", lambda port, **k: {"build": {"commit": "abc"}}
    )
    monkeypatch.setattr(sidecar, "_launch", lambda *a, **k: (_ for _ in ()).throw(AssertionError))

    sidecar.resolve(tmp_path, our_commit="abc")

    pidfile = tmp_path / "sidecar-clients" / str(os.getpid())
    assert pidfile.is_file()
    assert pidfile.read_text().strip() == str(os.getpid())


def test_spawn_registers_client_pidfile(monkeypatch, tmp_path: Path) -> None:
    def fake_launch(argv, env):
        fake_launch.port = int(env["ERRORTA_SIDECAR_PORT"])  # type: ignore[attr-defined]
        # The child's env carries started_by=cli so its advertisement is honest.
        assert env["ERRORTA_STARTED_BY"] == "cli"
        return _FakeProc(pid=4321)

    monkeypatch.setattr(sidecar, "_launch", fake_launch)
    monkeypatch.setattr(
        sidecar,
        "probe_healthz",
        lambda port, **k: {"build": {"commit": "abc"}}
        if port == getattr(fake_launch, "port", None)
        else None,
    )
    monkeypatch.setattr(sidecar, "_scan_errorta_processes", lambda **k: [])

    sidecar.resolve(tmp_path, our_commit="abc")

    assert (tmp_path / "sidecar-clients" / str(os.getpid())).is_file()


def test_stale_record_respawns(monkeypatch, tmp_path: Path) -> None:
    sidecar.write_record(
        tmp_path, {"port": 5555, "pid": 77, "commit": "abc", "started_by": "cli"}
    )
    # Dead sidecar on 5555; the freshly-spawned one answers on its new port.
    spawned_port: dict[str, int] = {}

    def fake_launch(argv, env):
        spawned_port["port"] = int(env["ERRORTA_SIDECAR_PORT"])
        return _FakeProc(pid=9001)

    def probe(port, **k):
        return {"build": {"commit": "abc"}} if port == spawned_port.get("port") else None

    monkeypatch.setattr(sidecar, "_launch", fake_launch)
    monkeypatch.setattr(sidecar, "probe_healthz", probe)
    # The recorded pid (77) is genuinely dead → clear + respawn.
    monkeypatch.setattr(sidecar, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(sidecar, "_scan_errorta_processes", lambda **k: [])

    handle = sidecar.resolve(tmp_path, our_commit="abc")
    assert handle.adopted is False
    assert handle.pid == 9001
    assert sidecar.read_record(tmp_path)["pid"] == 9001


def test_no_spawn_raises_when_nothing_live(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sidecar, "probe_healthz", lambda *a, **k: None)
    monkeypatch.setattr(
        sidecar, "_launch", lambda *a, **k: (_ for _ in ()).throw(AssertionError)
    )
    with pytest.raises(SidecarUnreachable):
        sidecar.resolve(tmp_path, allow_spawn=False)


def test_spawn_raises_when_child_exits(monkeypatch, tmp_path: Path) -> None:
    class _DeadProc:
        pid = 5
        returncode = 1

        def poll(self):
            return 1

    monkeypatch.setattr(sidecar, "_launch", lambda *a, **k: _DeadProc())
    monkeypatch.setattr(sidecar, "probe_healthz", lambda *a, **k: None)
    monkeypatch.setattr(sidecar, "_scan_errorta_processes", lambda **k: [])
    with pytest.raises(SidecarUnreachable):
        sidecar.resolve(tmp_path, our_commit="abc")


def test_transient_probe_failure_keeps_live_record_and_does_not_duplicate(
    monkeypatch, tmp_path: Path
) -> None:
    """A live-but-slow sidecar (probe times out, pid alive) is never nuked/duplicated."""
    sidecar.write_record(
        tmp_path, {"port": 5555, "pid": 4242, "commit": "abc", "started_by": "cli"}
    )
    # /healthz never answers (transient), but the recorded process IS alive.
    monkeypatch.setattr(sidecar, "probe_healthz", lambda *a, **k: None)
    monkeypatch.setattr(sidecar, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(
        sidecar, "_launch", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not spawn"))
    )
    # Keep the retry loop fast.
    monkeypatch.setattr(sidecar, "_ADOPT_PROBE_BACKOFF", 0.0)

    with pytest.raises(SidecarUnreachable):
        sidecar.resolve(tmp_path, our_commit="abc")

    # Record preserved (not clobbered) for the still-running sidecar.
    assert sidecar.read_record(tmp_path) is not None


def test_resolve_refuses_to_spawn_when_foreign_app_detected(
    monkeypatch, tmp_path: Path
) -> None:
    """No CLI record + a foreign Errorta process → refuse to spawn (all commands)."""
    monkeypatch.setattr(sidecar, "probe_healthz", lambda *a, **k: None)
    monkeypatch.setattr(sidecar, "_scan_errorta_processes", lambda **k: ["Errorta"])
    monkeypatch.setattr(
        sidecar, "_launch", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not spawn"))
    )
    with pytest.raises(ForeignSidecar):
        sidecar.resolve(tmp_path, our_commit="abc")
    # No second sidecar record was written.
    assert sidecar.read_record(tmp_path) is None


class _KillTrackingProc:
    def __init__(self, pid: int = 7777) -> None:
        self.pid = pid
        self.returncode = None
        self.killed = False
        self.waited = False

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        self.waited = True
        return self.returncode


def test_spawn_kills_child_on_readiness_timeout(monkeypatch, tmp_path: Path) -> None:
    """A spawn that never becomes ready must kill its detached child (no orphan)."""
    proc = _KillTrackingProc()
    monkeypatch.setattr(sidecar, "_launch", lambda *a, **k: proc)
    monkeypatch.setattr(sidecar, "probe_healthz", lambda *a, **k: None)
    monkeypatch.setattr(sidecar, "_scan_errorta_processes", lambda **k: [])
    # Shrink the readiness budget so the timeout path fires fast.
    monkeypatch.setattr(sidecar, "_SPAWN_READY_BUDGET", 0.05)
    monkeypatch.setattr(sidecar, "_SPAWN_POLL_INTERVAL", 0.01)

    with pytest.raises(SidecarUnreachable):
        sidecar.resolve(tmp_path, our_commit="abc")

    assert proc.killed is True
    assert proc.waited is True
    # No stale record for the child that never came up.
    assert sidecar.read_record(tmp_path) is None


def test_concurrent_resolve_spawns_exactly_once(monkeypatch, tmp_path: Path) -> None:
    """The flock guarantee: two concurrent resolve() calls spawn one sidecar.

    Fires two threads at resolve() with a slow `_launch` held under the home
    lock. The lock must serialize them so the second adopts the record the first
    wrote — `_launch` runs exactly once.
    """
    launch_calls: list[int] = []
    calls_lock = threading.Lock()
    spawned: dict[str, int] = {}

    def fake_launch(argv, env):
        with calls_lock:
            launch_calls.append(1)
        spawned["port"] = int(env["ERRORTA_SIDECAR_PORT"])
        time.sleep(0.25)  # simulate a slow boot while holding the home lock
        return _FakeProc(pid=1234)

    def probe(port, **k):
        return {"build": {"commit": "abc"}} if port == spawned.get("port") else None

    monkeypatch.setattr(sidecar, "_launch", fake_launch)
    monkeypatch.setattr(sidecar, "probe_healthz", probe)
    monkeypatch.setattr(sidecar, "_scan_errorta_processes", lambda **k: [])

    results: dict[int, sidecar.SidecarHandle] = {}
    errors: dict[int, BaseException] = {}

    def worker(i: int) -> None:
        try:
            results[i] = sidecar.resolve(tmp_path, our_commit="abc")
        except BaseException as exc:  # pragma: no cover — surfaced via assert below
            errors[i] = exc

    t1 = threading.Thread(target=worker, args=(0,))
    t2 = threading.Thread(target=worker, args=(1,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert not errors, errors
    assert len(launch_calls) == 1, "the flock must prevent a double-spawn"
    assert results[0].port == results[1].port
    # Exactly one spawned, one adopted the shared record.
    assert {results[0].adopted, results[1].adopted} == {True, False}


# --------------------------------------------------------------------------- #
# Foreign-app detection + the sole-owner refusal.
# --------------------------------------------------------------------------- #

def test_foreign_sidecar_detected_and_refused(monkeypatch, tmp_path: Path) -> None:
    # S9b: two UNCOORDINATED sidecars. We hold 6001, a DIFFERENT sidecar answers
    # on the app's 8770, and nothing on disk advertises our 6001 — so we are NOT
    # driving the single advertised sidecar → require_sole_owner refuses.
    monkeypatch.setattr(
        sidecar,
        "probe_healthz",
        lambda port, **k: {"service": "errorta-sidecar"} if port == 8770 else None,
    )
    monkeypatch.setattr(sidecar, "_scan_errorta_processes", lambda **k: [])

    reasons = sidecar.detect_foreign_sidecar(tmp_path, our_port=6001, our_pid=123)
    assert reasons and "8770" in reasons[0]

    handle = sidecar.SidecarHandle(
        base_url="http://127.0.0.1:6001",
        port=6001,
        pid=123,
        commit=None,
        started_by="cli",
        adopted=True,
    )
    with pytest.raises(ForeignSidecar):
        sidecar.require_sole_owner(tmp_path, handle)


def test_require_sole_owner_allows_driving_advertised_shared_sidecar(
    monkeypatch, tmp_path: Path
) -> None:
    """S9b co-drive: when we hold the ONE advertised sidecar, require_sole_owner
    is a no-op EVEN with a desktop-app process present — the app and CLI share it."""
    sidecar.write_record(
        tmp_path, {"port": 5555, "pid": 77, "commit": "abc", "started_by": "app"}
    )
    # A live /healthz on our (advertised) port reporting the SAME pid we drive
    # (LOW-4: identity, not just port), and an Errorta.app process running.
    monkeypatch.setattr(
        sidecar,
        "probe_healthz",
        lambda port, **k: {"service": "errorta-sidecar", "pid": 77}
        if port == 5555
        else None,
    )
    monkeypatch.setattr(sidecar, "_scan_errorta_processes", lambda **k: ["Errorta"])

    handle = sidecar.SidecarHandle(
        base_url="http://127.0.0.1:5555",
        port=5555,
        pid=77,
        commit="abc",
        started_by="app",
        adopted=True,
    )
    # Must NOT raise — we're driving the coordinated shared sidecar.
    sidecar.require_sole_owner(tmp_path, handle)


def test_require_sole_owner_refuses_when_port_swapped_to_different_pid(
    monkeypatch, tmp_path: Path
) -> None:
    """LOW-4: the advert names our port, but the sidecar now answering there is a
    DIFFERENT process (a swap that reused the port). Port equality alone used to
    pass the no-op; the pid cross-check now catches it → not driving → the foreign
    scan refuses."""
    sidecar.write_record(
        tmp_path, {"port": 5555, "pid": 77, "commit": "abc", "started_by": "app"}
    )
    # A live /healthz on 5555, but it reports a DIFFERENT pid than the one we
    # adopted/drive (77) — i.e. the process was swapped under us.
    monkeypatch.setattr(
        sidecar,
        "probe_healthz",
        lambda port, **k: {"service": "errorta-sidecar", "pid": 999}
        if port == 5555
        else None,
    )
    monkeypatch.setattr(sidecar, "_scan_errorta_processes", lambda **k: ["Errorta"])

    handle = sidecar.SidecarHandle(
        base_url="http://127.0.0.1:5555",
        port=5555,
        pid=77,
        commit="abc",
        started_by="app",
        adopted=True,
    )
    assert sidecar._driving_advertised_sidecar(tmp_path, handle) is False
    with pytest.raises(ForeignSidecar):
        sidecar.require_sole_owner(tmp_path, handle)


def test_require_sole_owner_refuses_when_a_different_sidecar_is_advertised(
    monkeypatch, tmp_path: Path
) -> None:
    """We hold 6001 but sidecar.json advertises a DIFFERENT sidecar on 8770 (an
    app that spawned its own) + a foreign process → a real second sidecar → refuse."""
    sidecar.write_record(
        tmp_path, {"port": 8770, "pid": 55, "commit": "abc", "started_by": "app"}
    )
    monkeypatch.setattr(
        sidecar,
        "probe_healthz",
        lambda port, **k: {"service": "errorta-sidecar"} if port == 8770 else None,
    )
    monkeypatch.setattr(sidecar, "_scan_errorta_processes", lambda **k: ["Errorta"])

    handle = sidecar.SidecarHandle(
        base_url="http://127.0.0.1:6001", port=6001, pid=123, commit="abc",
        started_by="cli", adopted=False,
    )
    with pytest.raises(ForeignSidecar):
        sidecar.require_sole_owner(tmp_path, handle)


def test_no_foreign_when_our_own_sidecar_is_on_8770(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        sidecar,
        "probe_healthz",
        lambda port, **k: {"service": "errorta-sidecar"} if port == 8770 else None,
    )
    monkeypatch.setattr(sidecar, "_scan_errorta_processes", lambda **k: [])
    reasons = sidecar.detect_foreign_sidecar(tmp_path, our_port=8770, our_pid=1)
    assert reasons == []
    # sole-owner guard passes (no raise).
    handle = sidecar.SidecarHandle(
        base_url="", port=8770, pid=1, commit=None, started_by="cli", adopted=False
    )
    sidecar.require_sole_owner(tmp_path, handle)


def test_process_scan_reason_included(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sidecar, "probe_healthz", lambda *a, **k: None)
    monkeypatch.setattr(sidecar, "_scan_errorta_processes", lambda **k: ["Errorta"])
    reasons = sidecar.detect_foreign_sidecar(tmp_path, our_port=6001, our_pid=1)
    assert any("Errorta" in r for r in reasons)


# --------------------------------------------------------------------------- #
# Explicit lifecycle controls.
# --------------------------------------------------------------------------- #

def test_status_reports_not_running_without_record(tmp_path: Path) -> None:
    info = sidecar.status(tmp_path)
    assert info["running"] is False
    assert info["record"] is None


def test_status_reports_running(monkeypatch, tmp_path: Path) -> None:
    sidecar.write_record(
        tmp_path, {"port": 5555, "pid": 1, "commit": "x", "started_by": "cli"}
    )
    monkeypatch.setattr(sidecar, "probe_healthz", lambda port, **k: {"service": "s"})
    info = sidecar.status(tmp_path)
    assert info["running"] is True


def test_stop_without_record_is_noop(tmp_path: Path) -> None:
    result = sidecar.stop(tmp_path)
    assert result["stopped"] is False


def test_restart_refuses_to_spawn_when_foreign_app_detected(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sidecar, "probe_healthz", lambda *a, **k: None)
    monkeypatch.setattr(sidecar, "_scan_errorta_processes", lambda **k: ["Errorta"])
    monkeypatch.setattr(
        sidecar,
        "_launch",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not spawn")),
    )

    with pytest.raises(ForeignSidecar):
        sidecar.restart(tmp_path, our_commit="abc")

    assert sidecar.read_record(tmp_path) is None


# --------------------------------------------------------------------------- #
# Real-boot smoke (skips without the engine stack) — mirrors boot-smoke.
# --------------------------------------------------------------------------- #

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.skipif(
    not _REAL_SIDECAR,
    reason="uvicorn / errorta_app.server not importable; install the full app stack",
)
def test_serve_reexec_boots_real_sidecar(tmp_path: Path) -> None:
    """`python -m errorta_cli __serve__` boots the embedded sidecar for real."""
    port = _free_port()
    home = tmp_path / "home"
    home.mkdir()
    env = {
        **os.environ,
        "ERRORTA_SIDECAR_PORT": str(port),
        "ERRORTA_HOME": str(home),
        "ERRORTA_LOG_LEVEL": "warning",
    }
    proc = subprocess.Popen(  # noqa: S603 — fixed argv, no shell
        [sys.executable, "-m", "errorta_cli", "__serve__"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 15.0
        body = None
        while time.monotonic() < deadline and proc.poll() is None:
            body = sidecar.probe_healthz(port)
            if body is not None:
                break
            time.sleep(0.1)
        assert body is not None, "embedded sidecar did not answer /healthz"
        assert body.get("service") == "errorta-sidecar"
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
