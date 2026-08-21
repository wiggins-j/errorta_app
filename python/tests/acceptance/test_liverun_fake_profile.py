"""Live-run supervisor acceptance journey (spec §4, §6.1, §6.2).

Everything below the fake `ssh` binary is REAL: the profile loader and its
validator, the step primitives, `RunStore`/`LaunchLedger`, `TunnelManager`,
`RemoteToolRunner`, the `Supervisor` state machine and its daemon thread, and
`recover_on_boot`. Only two seams are faked, and both are faked at the edge of
the process rather than inside the module under test:

* `ssh` — a shell script that drops the ssh flags up to `--` and `eval`s the
  remote command locally. The remote path (argv build, `--` separator, the
  detach wrapper, `remote_signal`'s TERM/KILL escalation) all execute for real.
* `known_hosts_fn` — the profile's `localhost` host is declared known, so the
  validator's `ssh-keygen -F` probe doesn't decide whether this test runs.

The world the supervisor watches is two scripts in `liverun_fixtures/`: a
loopback HTTP "client" that reports `gameState` from a control file, and a
"brain" that logs for a few seconds, then goes quiet while staying alive — and
performs the safe logout on SIGTERM.
"""
from __future__ import annotations

import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from errorta_liverun import profile as P
from errorta_liverun.recovery import recover_on_boot
from errorta_liverun.state import LaunchLedger, RunStore
from errorta_liverun.supervisor import LiveRunManager
from errorta_tools.runner.remote import RemoteToolRunner
from errorta_tunnels.manager import TunnelManager

pytestmark = [
    pytest.mark.acceptance,
    pytest.mark.regression,
    pytest.mark.skipif(sys.platform == "win32", reason="posix only: fork, killpg, sh"),
]

FIXTURES = Path(__file__).parent / "liverun_fixtures"
FAKE_CLIENT = FIXTURES / "fake_client.py"
FAKE_BRAIN = FIXTURES / "fake_brain.py"

# Mirrors `errorta_liverun.profile._PATH_RE`: `pidfile`/`log` paths are
# validated against it, and a temp root outside that alphabet (macOS
# `$TMPDIR` is not guaranteed to stay inside it) would fail the profile load
# rather than the behaviour under test. See `_work_dir`.
_PATH_SAFE = re.compile(r"^[A-Za-z0-9~/._-]+$")

ACTIVE_S = 3.0          # how long the brain logs before going quiet
EVERY_S = 1.0           # watch cadence
STALL_AFTER_S = 3.0     # declared stall window
_TICK_S = 1.0           # `supervisor._TICK_S`: the machine's own tick floor

# Worst case for `stall` after the brain's last log line: the probe that
# observed that line can land up to one tick late (so `last_ok` is up to
# `_TICK_S` after the log went quiet), the stall then needs `STALL_AFTER_S` to
# elapse, and the probe that notices lands on the next tick. Plus 2 s of slack
# for `now_iso`'s whole-second truncation and process scheduling.
STALL_BUDGET_S = STALL_AFTER_S + EVERY_S + _TICK_S + 2.0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _epoch(iso: str) -> float:
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # exists, not ours — still "alive" for our purposes
        return True
    return True


def _wait_gone(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.1)
    return False


@pytest.fixture(scope="module", autouse=True)
def _no_leaked_fixture_processes():
    """Safety net. Every path in this module is supposed to reap its own
    children through the profile's teardown; if one ever doesn't, a stray fake
    brain or client would idle for the rest of the session (and hold a port).
    `pkill -f` on the fixture's absolute path can only match these scripts."""
    yield
    killed = []
    for script in (FAKE_BRAIN, FAKE_CLIENT):
        if subprocess.run(["pkill", "-f", str(script)], check=False,
                          stdin=subprocess.DEVNULL, capture_output=True).returncode == 0:
            killed.append(script.name)
    # A supervisor thread outliving its run would keep probing a torn-down
    # world for the rest of the session; `LiveRunManager.teardown_all` joins
    # every one it ever started, so there must be none left.
    leaked_threads = sorted(t.name for t in threading.enumerate()
                            if t.name.startswith("liverun-"))
    assert not killed, f"the safety net had to reap leaked fixture processes: {killed}"
    assert not leaked_threads, leaked_threads


@pytest.fixture
def work_dir(tmp_path: Path):
    """A directory whose path the profile validator will accept for
    `pidfile`/`log`. `tmp_path` normally qualifies; when the platform's temp
    root uses characters outside the validator's alphabet, fall back to one
    that does rather than skipping the test."""
    if _PATH_SAFE.match(str(tmp_path)):
        yield tmp_path
        return
    fallback = Path(tempfile.mkdtemp(prefix="errorta-liverun-acc-", dir="/tmp"))
    try:
        yield fallback
    finally:
        shutil.rmtree(fallback, ignore_errors=True)


class _Env:
    """Everything a test needs to drive and inspect one fake live run."""

    def __init__(self, **kw: object) -> None:
        self.__dict__.update(kw)


@pytest.fixture
def env(tmp_path: Path, work_dir: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))

    ssh = work_dir / "ssh"
    ssh.write_text('#!/bin/sh\nwhile [ "$1" != "--" ]; do shift; done\nshift\neval "$1"\n')
    ssh.chmod(ssh.stat().st_mode | stat.S_IEXEC)

    # `steps`' detach wrapper prefers `setsid` when the remote box has it. On
    # Linux that is util-linux's, which forks when the caller already leads a
    # process group — and then `$!` names the wrapper, not the brain. Shim it
    # to a plain `exec` so the pidfile names the same process on every
    # platform (macOS has no `setsid` at all and takes the `nohup` branch).
    bindir = work_dir / "bin"
    bindir.mkdir()
    setsid = bindir / "setsid"
    setsid.write_text('#!/bin/sh\nexec "$@"\n')
    setsid.chmod(setsid.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")

    port = _free_port()
    ctrl = work_dir / "ctrl"
    ctrl.write_text("LOGGED_IN")
    brain_log = work_dir / "brain.log"
    brain_pidfile = work_dir / "brain.pid"
    client_pidfile = work_dir / "client.pid"
    state_url = f"http://127.0.0.1:{port}/state"
    py = sys.executable

    doc = {
        "version": 1,
        "created_by": "operator",
        "hosts": {"box": {"ssh_host": "localhost"}},
        "tunnels": [],
        "launch": [
            {"name": "client",
             # The launcher exits as soon as the daemon is bound, like the real
             # `osascript`/`jagex-play` step; the check is what proves it is up.
             "local": {"argv": [py, str(FAKE_CLIENT), str(port), str(ctrl), str(client_pidfile)]},
             "check": {"http": {"url": state_url, "expect_status": 200}},
             "timeout_s": 15},
            {"name": "brain",
             "remote": {"host": "box",
                        "argv": [py, str(FAKE_BRAIN), str(brain_log), str(ACTIVE_S), str(ctrl)],
                        "detach": True, "pidfile": str(brain_pidfile),
                        "log": str(work_dir / "brain.out")},
             "check": {"remote_pid_alive": {"host": "box", "pidfile": str(brain_pidfile)}},
             "timeout_s": 15},
        ],
        "watch": [
            {"id": "client", "every_s": EVERY_S, "stall_after_s": STALL_AFTER_S,
             "on_stall": "stop", "probe": {"http": {"url": state_url}}},
            {"id": "brain-alive", "every_s": EVERY_S, "stall_after_s": STALL_AFTER_S,
             "on_stall": "stop",
             "probe": {"remote_pid_alive": {"host": "box", "pidfile": str(brain_pidfile)}}},
            {"id": "brain-log", "every_s": EVERY_S, "stall_after_s": STALL_AFTER_S,
             "on_stall": "stop",
             "probe": {"remote_stdout_advancing": {"host": "box",
                                                   "argv": ["tail", "-n", "1", str(brain_log)]}}},
        ],
        "evidence": [
            {"name": "log-tail", "remote": {"host": "box",
                                            "argv": ["tail", "-n", "5", str(brain_log)]}},
            {"name": "client-state", "http": {"url": state_url}},
        ],
        "teardown": [
            {"name": "brain-stop",
             "remote_signal": {"host": "box", "pidfile": str(brain_pidfile),
                               "signal": "TERM", "grace_s": 2, "then": "KILL"}},
            {"name": "logoff-wait",
             "check": {"http_json": {"url": state_url, "path": "gameState",
                                     "not_equals": "LOGGED_IN"}},
             "timeout_s": 5, "evidence_literal": "logoff_verified"},
            {"name": "client-stop",
             "remote_signal": {"host": "box", "pidfile": str(client_pidfile),
                               "signal": "TERM", "grace_s": 2, "then": "KILL"}},
        ],
        "caps": {"min_launch_gap_s": 900},
        "ban_signals": ["Account is banned"],
    }

    profiles = P.profiles_dir()
    profiles.mkdir(parents=True, exist_ok=True)
    profile_file = profiles / "fake.yaml"
    profile_file.write_text(yaml.safe_dump(doc))
    profile_file.chmod(0o600)

    store = RunStore()
    mgr = LiveRunManager(
        store=store, ledger=LaunchLedger(), tunnels=TunnelManager(),
        remote=RemoteToolRunner(ssh_bin=str(ssh)),
        load_profile=lambda p: P.load_profile(p, known_hosts_fn=lambda _h: True))

    yield _Env(mgr=mgr, store=store, ledger=LaunchLedger(), ssh=ssh, work=work_dir,
               ctrl=ctrl, brain_log=brain_log, brain_pidfile=brain_pidfile,
               client_pidfile=client_pidfile, state_url=state_url)

    mgr.teardown_all()
    for pidfile in (brain_pidfile, client_pidfile):
        try:
            os.kill(int(pidfile.read_text().strip()), 9)
        except (OSError, ValueError):
            pass


def _wait_terminal(env: _Env, timeout: float = 40.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = env.mgr.status(project_id="proj")
        if st["status"] == "empty" and st.get("last"):
            return st["last"]
        time.sleep(0.2)
    raise AssertionError(f"run never reached a terminal phase: {env.mgr.status(project_id='proj')}")


def _wait_phase(env: _Env, phase: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = env.mgr.status(project_id="proj")
        if st.get("phase") == phase:
            return
        if st["status"] == "empty" and st.get("last"):
            raise AssertionError(f"run ended before reaching {phase!r}: {st['last']}")
        time.sleep(0.1)
    raise AssertionError(f"never reached {phase!r}: {env.mgr.status(project_id='proj')}")


def test_stall_is_detected_and_torn_down_with_logoff(env: _Env) -> None:
    """§6.1: a stall is detected and torn down with `logoff_verified` inside the
    declared window, with nobody watching."""
    t0 = time.monotonic()
    started = env.mgr.start("fake", project_id="proj")
    assert started["status"] == "started", started
    final = _wait_terminal(env)
    assert final["run_id"] == started["run_id"]

    assert final["phase"] == "stopped", final
    assert final["reason"] == "stall:brain-log", final
    assert final["literals"] == {"logoff_verified": True}
    assert final["owned_pgids"] == []
    assert time.monotonic() - t0 < 45

    events = env.store.events(final["run_id"])
    kinds = [e["kind"] for e in events]
    assert kinds.count("stall") == 1, kinds
    # Evidence is collected while the world is still standing; teardown is what
    # takes it down. The order is the whole point of `_do_stopping`.
    assert kinds.index("evidence") < kinds.index("teardown_step"), kinds
    literals = [e for e in events if e["kind"] == "literals"]
    assert literals[-1]["detail"] == {"logoff_verified": "PRESENT"}

    # ... and it was detected promptly, not eventually. The brain's log stopped
    # advancing at its final mtime; that is the instant the stall began.
    quiet_at = env.brain_log.stat().st_mtime
    stall_at = _epoch(next(e for e in events if e["kind"] == "stall")["at"])
    assert stall_at - quiet_at <= STALL_BUDGET_S, (stall_at - quiet_at, STALL_BUDGET_S)

    # No orphans on either "host".
    brain_pid = int(env.brain_pidfile.read_text().strip())
    client_pid = int(env.client_pidfile.read_text().strip())
    assert _wait_gone(brain_pid), "brain survived teardown"
    assert _wait_gone(client_pid), "client survived teardown"


def test_second_start_inside_the_gap_is_refused(env: _Env) -> None:
    """§6.4: the caps ledger, not the model, decides whether a relaunch may
    happen — and it says which cap refused."""
    assert env.mgr.start("fake", project_id="proj")["status"] == "started"
    _wait_phase(env, "watching")
    assert env.mgr.stop(project_id="proj")["status"] == "stopping"
    final = _wait_terminal(env)
    assert final["phase"] == "stopped"

    assert env.mgr.start("fake", project_id="proj") == {"status": "refused", "reason": "cap_gap"}


def test_sidecar_restart_mid_run_is_lost_on_restart(env: _Env) -> None:
    """§6.2: a restart mid-run leaves no orphan child on either host, marks the
    run `lost_on_restart`, and does not relaunch it."""
    assert env.mgr.start("fake", project_id="proj")["status"] == "started"
    _wait_phase(env, "watching")

    sup = env.mgr._runs["fake"]
    brain_pid = int(env.brain_pidfile.read_text().strip())
    client_pid = int(env.client_pidfile.read_text().strip())
    assert _alive(brain_pid) and _alive(client_pid)

    # Simulate the sidecar dying: freeze the supervisor's own loop so it can
    # neither observe nor tear anything down, exactly as a killed process
    # can't. (Killing this pytest process is not an option, so the thread is
    # parked instead — the persisted state on disk is identical either way,
    # and that state is all `recover_on_boot` gets to see.)
    frozen = threading.Event()
    entered = threading.Event()

    def _frozen_tick() -> None:
        entered.set()
        frozen.wait(60)

    sup._tick = _frozen_tick  # type: ignore[method-assign]
    assert entered.wait(10), "supervisor thread never reached the frozen tick"

    lost = recover_on_boot(
        store=RunStore(), tunnels=TunnelManager(),
        remote=RemoteToolRunner(ssh_bin=str(env.ssh)),
        load_profile=lambda p: P.load_profile(p, known_hosts_fn=lambda _h: True))

    assert lost == [sup.state.run_id]
    after = RunStore().load(sup.state.run_id)
    assert after is not None
    assert after.phase == "lost_on_restart"
    assert after.reason == "sidecar_restart"
    # Teardown ran for real on the recovery path: the brain logged off.
    assert after.literals.get("logoff_verified") is True
    assert _wait_gone(brain_pid), "brain survived boot recovery"
    assert _wait_gone(client_pid), "client survived boot recovery"

    # No relaunch, and no laundering of the caps ledger: recovery records
    # exactly nothing — one launch row from the original start, no outcome.
    rows = env.ledger._rows()
    assert [r["kind"] for r in rows] == ["launch"], rows
    assert rows[0]["run_id"] == sup.state.run_id

    # Let the parked thread finish so the module leaks no threads. It will run
    # its own (now no-op) teardown and land terminal; the fixture joins it.
    sup.stop("test_cleanup")
    frozen.set()
    del sup._tick
