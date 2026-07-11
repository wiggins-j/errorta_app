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
        return _FakeProc(pid=4321)

    monkeypatch.setattr(sidecar, "_launch", fake_launch)
    monkeypatch.setattr(
        sidecar, "probe_healthz", lambda port, **k: {"build": {"commit": "abc"}}
    )

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
    with pytest.raises(SidecarUnreachable):
        sidecar.resolve(tmp_path, our_commit="abc")


# --------------------------------------------------------------------------- #
# Foreign-app detection + the sole-owner refusal.
# --------------------------------------------------------------------------- #

def test_foreign_sidecar_detected_and_refused(monkeypatch, tmp_path: Path) -> None:
    # Our CLI sidecar is on 6001; a foreign one answers on the app's 8770.
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
