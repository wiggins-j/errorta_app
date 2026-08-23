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

import json
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
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

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

    def _write_profile() -> None:
        """Re-render the profile from `env.doc`. The fix-loop tests extend the
        Slice 1 profile (repos, fix_loop, a deploy step) and then rewrite it —
        the file on disk is what the REAL validator reads, so nothing about
        these tests bypasses it."""
        profile_file.write_text(yaml.safe_dump(doc))
        profile_file.chmod(0o600)

    _write_profile()

    # Timing-only overrides, applied AFTER the real validator has run. The
    # validator's floors (idle_timeout_s > 2100) are unit-tested where they
    # belong; an acceptance test cannot wait forty minutes to watch an idle dev
    # run get cancelled. The logic under test is untouched -- only the clock.
    timing: dict[str, float] = {}

    def _load(path):
        prof = P.load_profile(path, known_hosts_fn=lambda _h: True)
        if timing and prof.fix_loop is not None:
            prof = replace(prof, fix_loop=replace(prof.fix_loop, **timing))
        return prof

    store = RunStore()
    mgr = LiveRunManager(
        store=store, ledger=LaunchLedger(), tunnels=TunnelManager(),
        remote=RemoteToolRunner(ssh_bin=str(ssh)),
        load_profile=_load)

    yield _Env(mgr=mgr, store=store, ledger=LaunchLedger(), ssh=ssh, work=work_dir,
               ctrl=ctrl, brain_log=brain_log, brain_pidfile=brain_pidfile,
               client_pidfile=client_pidfile, state_url=state_url,
               doc=doc, write_profile=_write_profile, timing=timing,
               project_id="proj", profile_file=profile_file)

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


# ===========================================================================
# Slice 2 — the fix loop, end to end (spec 2026-08-22 §4).
#
# Everything below the fake dev is REAL: the profile validator reading a real
# repos:/fix_loop: block off disk, deterministic triage, the fenced brief, a
# real `LedgerStore` project with a real git worktree, a REAL registered
# acceptance gate executed as a subprocess, the REAL merge gate, the REAL
# `accept_live_fix` verb fired through the REAL bridge effect path, a real
# rsync deploy step through the argv-identity guard, and a real relaunch
# through the untouched launch caps.
#
# Exactly one seam is faked, at the edge of the process rather than inside any
# module under test: `FixDeps.start_run_fn`, the dev team's model run. It does
# what a dev member does -- edits one file in the project's worktree, runs the
# gate, records the review, and stops the run -- and spends no model call.
# ===========================================================================

BROKEN_APP = "def answer():\n    return 3\n"
FIXED_APP = "def answer():\n    return 4\n"
REPO_TEST = "from app import answer\n\n\ndef test_answer():\n    assert answer() == 4\n"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def fake_repo(work_dir: Path) -> Path:
    """A real git repository whose test suite FAILS until the fix lands.

    Generated rather than committed: a fixture tree with its own `.git` inside
    this repository would be a nested checkout, and the test needs real git
    history anyway (the worktree is cloned from it).
    """
    repo = work_dir / "brainrepo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "app.py").write_text(BROKEN_APP)
    (repo / "test_app.py").write_text(REPO_TEST)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


GATE_ARGV = [sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider",
             "test_app.py"]


def _seed_project(project_id: str, repo: Path):
    """The operator's one-time setup, done exactly as the runbook says: a
    project bound to the real repo, an argv-only acceptance gate registered on
    it, and `dev_repo_read` opted in."""
    from errorta_council.coding import autonomy
    from errorta_council.coding.ledger import LedgerStore
    from errorta_council.coding.workspace import CodingWorkspace

    store = LedgerStore(project_id)
    store.create_project(north_star="keep the brain playing",
                         definition_of_done="the brain runs unattended",
                         target="existing", repo_path=str(repo))
    store.set_test_commands(
        {"gate": {"argv": GATE_ARGV, "cwd": ".", "timeout_seconds": 120}})
    policy = autonomy.load_policy(store)
    autonomy.save_policy(store, replace(policy, dev_repo_read=True))
    ws = CodingWorkspace(project_id, store)
    ws.setup(target="existing", repo_path=str(repo))
    return store, ws


def _fix_profile(env: _Env, fake_repo: Path, *, deploy: bool = True,
                 max_cycles: int = 3) -> Path:
    """Extend the Slice 1 profile with the §3.2 fix-loop block and rewrite it.

    The brain goes quiet almost immediately here: the fix loop's own timings
    are what this test is about, and the stall detection they follow is already
    covered by `test_stall_is_detected_and_torn_down_with_logoff`.
    """
    deploy_dest = env.work / "deployed"
    rsync = shutil.which("rsync")
    if rsync is None:                       # pragma: no cover - macOS/Linux both ship it
        pytest.skip("rsync is not installed")
    # The "brain-log" watch probe tails the brain's log via `remote_stdout_advancing`
    # (see the profile's `watch` list above), which triage now classifies by
    # *kind* as `journal_stall`, not `brain_log_stall` (that class is for a
    # `remote_file_mtime_advancing` probe). Declare both so this stall still
    # attributes deterministically to the one repo.
    repo: dict = {"id": "brain", "path": str(fake_repo), "errorta_project": env.project_id,
                  "classify": ["brain_log_stall", "journal_stall", "python_traceback"]}
    if deploy:
        repo["deploy"] = [{"name": "rsync", "timeout_s": 60,
                           "local": {"argv": [rsync, "-a", "--delete", "--exclude", ".git",
                                              f"{fake_repo}/", f"{deploy_dest}/"]}}]
    env.doc["repos"] = [repo]
    env.doc["fix_loop"] = {"enabled": True, "max_fix_cycles_per_day": max_cycles,
                           "idle_timeout_s": 2101, "accept_timeout_s": 1800}
    env.doc["launch"][1]["remote"]["argv"][3] = "0.5"        # the brain's active window
    env.write_profile()
    env.deploy_dest = deploy_dest
    return deploy_dest


def _fake_dev(env: _Env, *, extra: dict[str, str] | None = None,
              go_idle: bool = False):
    """The dev member, minus the model. Edits ONE file in the project's real
    worktree, runs the REAL registered gate against it, records the review the
    merge gate demands, and stops the run — the observable shape of a dev run
    that finished its task."""
    from errorta_council.coding import testing
    from errorta_council.coding.ledger import LedgerStore
    from errorta_council.coding.workspace import CodingWorkspace

    def _start(project_id: str, *, resume: bool, continue_: bool) -> dict:
        store = LedgerStore(project_id)
        store.set_run_state(status="running", cancel_requested=False, ended_at=None)
        env.dev_calls.append({"project_id": project_id, "resume": resume,
                              "continue": continue_})
        if go_idle:
            # A dev run that starts and then says nothing at all -- what the
            # idle watchdog exists for. It stops only when asked to, the way
            # the real run loop honours `cancel_requested`.
            env.idle_watcher = threading.Thread(
                target=_await_cancel, args=(store,), daemon=True,
                name="acceptance-idle-dev")
            env.idle_watcher.start()
            return {"started": True}
        ws = CodingWorkspace(project_id, store)
        ws.set_target("existing")
        task = store.list_tasks()[-1]
        # The fix itself always lands: a diff the gate refuses is a DIFFERENT
        # pause (`fix_gate_blocked`), and a test about guarded paths must not
        # accidentally be a test about a red gate.
        for rel, text in {"app.py": FIXED_APP, **(extra or {})}.items():
            ws.write_file(rel, text, task_id=task.task_id, summary="live-run fix")
        head = ws.head()
        session = testing.run_test_commands(
            Path(ws.root()), store.get_test_commands(), ["gate"])
        env.gate_sessions.append(bool(session.passed))
        store.record_test_run(session, task_id=task.task_id, head=head)
        store.update_task(task.task_id, state="done")
        store.record_decision(title="reviewed the live-run fix", context="fix loop",
                              choice="review_approved", rationale="the gate is green",
                              extra={"reviewed_head": head})
        store.set_project_status("done")
        store.set_run_state(status="stopped", stop_reason="task complete")
        return {"started": True}

    return _start


def _await_cancel(store) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if (store.get_run_state() or {}).get("cancel_requested"):
            store.set_run_state(status="stopped", stop_reason="cancelled")
            return
        time.sleep(0.1)


@pytest.fixture
def fix_env(env: _Env, fake_repo: Path, monkeypatch: pytest.MonkeyPatch):
    """`env`, plus a seeded project and the fix loop's cadence turned down.

    `POLL_S`/`RUN_START_GRACE_S` are the driver's polling cadence, not its
    logic: at the shipped 30 s/120 s a single cycle would spend two and a half
    minutes asleep. Every transition they gate is still taken for real.
    """
    from errorta_liverun import fixloop

    monkeypatch.setattr(fixloop, "POLL_S", 0.5)
    monkeypatch.setattr(fixloop, "RUN_START_GRACE_S", 2.0)
    env.dev_calls = []
    env.gate_sessions = []
    env.idle_watcher = None
    env.ledger_store, env.ws = _seed_project(env.project_id, fake_repo)
    yield env
    if env.idle_watcher is not None:
        env.idle_watcher.join(timeout=10)
        assert not env.idle_watcher.is_alive(), "the fake dev's watcher outlived the test"


def _start_with(env: _Env, fake_dev) -> dict:
    """Rebuild the manager with the fake dev wired into `FixDeps`, then start.

    Only `start_run_fn` and `baseline_gate_fn` are injected: the ledger, the
    workspace, the gate reads, the confirmation store and the bound channel
    all resolve to their real production defaults. `baseline_gate_fn` is
    stubbed to a passing baseline because `BROKEN_APP` -- committed as the
    fake repo's ONLY commit, precisely so the dev's own fix has something real
    to turn green -- is exactly what a genuinely red clean-tree gate looks
    like, and the real seam would now pause every one of these runs before
    the dev ever got a task (`fix_run_failed`/`baseline_gate_red`). That
    precondition has its own dedicated coverage in `test_fixloop.py`; this
    suite is about the run/accept/deploy machinery downstream of it.
    """
    from errorta_liverun.fixloop import FixDeps

    env.mgr._fix_deps = FixDeps(
        start_run_fn=fake_dev,
        baseline_gate_fn=lambda store, ws: SimpleNamespace(passed=True, sandbox="seatbelt"))
    started = env.mgr.start("fake", project_id=env.project_id)
    assert started["status"] == "started", started
    return started


def _events(env: _Env, run_id: str) -> list[dict]:
    return env.store.events(run_id)


def _wait_for_kind(env: _Env, run_id: str, kind: str, timeout: float = 40.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for event in _events(env, run_id):
            if event["kind"] == kind:
                return event
        state = env.store.load(run_id)
        if state is not None and state.phase in ("paused_awaiting_human", "failed"):
            # Terminal without the event we are waiting for: report what the
            # run actually did rather than timing out with no explanation.
            raise AssertionError(
                f"run ended {state.phase}/{state.reason} before {kind!r}: "
                f"{[e['kind'] for e in _events(env, run_id)]}")
        time.sleep(0.1)
    raise AssertionError(f"never saw {kind!r}: {[e['kind'] for e in _events(env, run_id)]}")


def _detail(env: _Env, run_id: str, kind: str) -> dict:
    return dict(_wait_for_kind(env, run_id, kind)["detail"])


def _backdate_launches(env: _Env, seconds: float = 3700.0) -> None:
    """Move every recorded launch back in time. The relaunch is then evaluated
    by the REAL, untouched cap arithmetic -- against a ledger that says the
    last launch was over an hour ago, which after a real multi-hour session is
    exactly what it would say. The alternative is a test that waits 15 minutes
    to prove `min_launch_gap_s` is 15 minutes."""
    path = env.ledger._path
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    for row in rows:
        if isinstance(row.get("at"), (int, float)):
            row["at"] = float(row["at"]) - seconds
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


class _RecordingPoster:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    def post_message(self, channel_id, thread_ts, text, blocks=None) -> dict:
        self.messages.append({"channel_id": channel_id, "text": text, "blocks": blocks})
        return {"ts": "ts-1"}


def _autopilot(env: _Env) -> list[str]:
    """One outbound tick's autopilot sweep, through the REAL bridge effect path
    (`SlackBridge._fire_confirmed_effect` -> `tools.dispatch(...,
    confirmed_via="block_actions")` -> the real `accept_live_fix`)."""
    from errorta_slack import connection as slack_connection
    from errorta_slack import outbound as slack_outbound
    from errorta_slack import tools as slack_tools

    # The ONE seam this env has to wire: `accept_live_fix` refuses any
    # confirmation a live supervisor is not currently waiting on, and its
    # production default asks the `errorta_liverun.supervisor` module
    # singleton. This test owns its own `LiveRunManager` (`env.mgr`), so it
    # answers the identical question against the manager that actually holds
    # the run. Everything else is the real default.
    deps_ = slack_tools.ToolDeps(liverun_accept_binding_fn=env.mgr.accept_is_staged)
    bridge = slack_connection.SlackBridge(
        object(), env.poster, deps_, lambda member, prompt: "{}",
        config={"allowed_team_ids": ["T1"], "allowed_user_ids": ["U1"]})
    deps = slack_outbound.OutboundDeps(fire_effect_fn=bridge._fire_confirmed_effect)
    return slack_outbound.sweep_autopilot(
        env.channel_id, env.project_id, deps=deps, poster=env.poster,
        config={"autopilot": True})


@pytest.fixture
def slack_channel(fix_env: _Env):
    """A bound channel, so the staged acceptance carries one and the autopilot
    sweep can find it."""
    from errorta_slack import store as slack_store

    slack_store.bind_channel("C-acc", fix_env.project_id)
    fix_env.channel_id = "C-acc"
    fix_env.poster = _RecordingPoster()
    return fix_env


def test_a_stall_is_fixed_accepted_deployed_and_relaunched(slack_channel: _Env) -> None:
    """The whole Slice 2 loop, no model calls and no human: stall -> teardown
    -> deterministic triage -> a fenced brief filed as a dev task -> the dev
    run -> the real merge gate -> autopilot's accept -> the deploy step -> a
    NEW run, linked by `fix_of`."""
    env = slack_channel
    deploy_dest = _fix_profile(env, Path(env.ledger_store.get_project().repo_path))
    fake_repo = Path(env.ledger_store.get_project().repo_path)

    first = _start_with(env, _fake_dev(env))
    run_id = first["run_id"]

    # 1. triage named the repo from the stall reason alone -- no model.
    triage = _detail(env, run_id, "fix_triage")
    assert triage["repo_id"] == "brain"
    assert triage["confidence"] == "deterministic"

    # 2. the task carries the fenced, untrusted-evidence brief.
    task_event = _detail(env, run_id, "fix_task")
    task = env.ledger_store.list_tasks()[-1]
    assert task.task_id == task_event["task_id"] and task.role == "dev"
    assert "UNTRUSTED LIVE-RUN EVIDENCE" in task.detail
    assert task.detail.count("UNTRUSTED LIVE-RUN EVIDENCE") >= 2   # begin AND end fence

    # 3. the acceptance is STAGED, never taken by the driver itself.
    staged = _detail(env, run_id, "fix_accept_staged")
    assert staged["human_only"] is False and staged["repo_id"] == "brain"
    assert env.gate_sessions == [True]        # the real gate ran, and passed
    assert (fake_repo / "app.py").read_text() == BROKEN_APP   # nothing merged yet

    # 4. autopilot fires it through the same path a button tap takes.
    _backdate_launches(env)
    assert _autopilot(env) == [staged["cid"]]

    accepted = _detail(env, run_id, "fix_accepted")
    assert (fake_repo / "app.py").read_text() == FIXED_APP

    # 4b. and the merge is believed because the effect RECORDED it, not because
    # a branch head moved: `deliver` copies the merged tree out without moving
    # `ws.head()` at all, so the decision row is the only durable proof.
    assert accepted["verified_by"] == "accepted"
    rows = [d for d in env.ledger_store.list_decisions()
            if d.get("choice") == "accept_live_fix"]
    assert [r.get("status") for r in rows] == ["accepted"]
    assert rows[0].get("run_id") == run_id and rows[0].get("repo_id") == "brain"

    # 5. the deploy step ran, as the exact argv the profile declared.
    deploy = _detail(env, run_id, "deploy_step")
    assert deploy["name"] == "rsync" and deploy["ok"] is True
    assert (deploy_dest / "app.py").read_text() == FIXED_APP
    assert not (deploy_dest / ".git").exists()          # --exclude .git honoured

    # 6. a NEW run, linked, inside the untouched caps.
    relaunched = _wait_for_new_run(env, after=run_id)
    assert relaunched.fix_of == run_id and relaunched.fix_cycle == 1
    assert relaunched.run_id != run_id
    assert [e["kind"] for e in _events(env, run_id) if e["kind"] == "relaunch_refused"] == []
    assert env.ledger.fix_cycles_today("fake", time.time()) == 1


def _wait_for_new_run(env: _Env, *, after: str, timeout: float = 30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for state in env.store.list_non_terminal():
            if state.run_id != after and state.project_id == env.project_id:
                return state
        time.sleep(0.1)
    raise AssertionError("no relaunched run appeared")


def test_a_guarded_path_stops_the_cycle_at_the_button(slack_channel: _Env) -> None:
    """Same cycle, but the fix touches the supervisor's own package: the accept
    is staged human-only, autopilot does NOT fire it, nothing merges, and the
    client is never relaunched."""
    env = slack_channel
    _fix_profile(env, Path(env.ledger_store.get_project().repo_path))
    env.timing["accept_timeout_s"] = 4.0        # the wait, not the rule
    fake_repo = Path(env.ledger_store.get_project().repo_path)

    first = _start_with(env, _fake_dev(
        env, extra={"errorta_liverun/x.py": "# a fix to the supervisor itself\n"}))
    run_id = first["run_id"]

    staged = _detail(env, run_id, "fix_accept_staged")
    assert staged["human_only"] is True
    assert _autopilot(env) == []                     # autopilot declined to act

    final = _wait_terminal(env)
    assert final["phase"] == "paused_awaiting_human"
    assert final["reason"] == "fix_declined"
    assert not (fake_repo / "errorta_liverun").exists()      # nothing merged
    assert env.store.list_non_terminal() == []               # nothing relaunched


def test_an_idle_fix_run_is_cancelled_and_never_merges(slack_channel: _Env) -> None:
    """A dev run that goes quiet is cancelled with the run loop's own
    cooperative signal, and the cycle pauses for a human rather than merging
    whatever half-finished state the worktree is in."""
    env = slack_channel
    _fix_profile(env, Path(env.ledger_store.get_project().repo_path))
    env.timing["idle_timeout_s"] = 3.0          # the window, not the rule

    first = _start_with(env, _fake_dev(env, go_idle=True))
    run_id = first["run_id"]

    cancel = _detail(env, run_id, "fix_idle_cancel")
    assert cancel["idle_s"] >= 3

    final = _wait_terminal(env)
    assert final["phase"] == "paused_awaiting_human"
    assert final["reason"] == "fix_idle"
    assert env.ledger_store.get_run_state()["cancel_requested"] is True
    assert [e for e in _events(env, run_id) if e["kind"] == "fix_accept_staged"] == []
    assert env.ledger.fix_cycles_today("fake", time.time()) == 1   # a spent cycle


def test_the_day_cap_stops_the_cycle_before_it_starts(slack_channel: _Env) -> None:
    """The cap is arithmetic over the ledger, not a counter in memory: a
    profile that has already spent its day pauses for a human on the next stall
    and files nothing at all."""
    env = slack_channel
    _fix_profile(env, Path(env.ledger_store.get_project().repo_path), max_cycles=1)
    env.ledger.record_fix_cycle("fake", "r-earlier", "brain", failed=True,
                                at=time.time() - 60)

    first = _start_with(env, _fake_dev(env))
    run_id = first["run_id"]

    cap = _detail(env, run_id, "fix_cycle_cap")
    assert cap == {"cycles_today": 1, "cap": 1}
    final = _wait_terminal(env)
    assert final["phase"] == "paused_awaiting_human"
    assert final["reason"] == "fix_cycle_cap"
    assert env.dev_calls == []                      # no dev run was ever started
    assert env.ledger_store.list_tasks() == []      # and no task was filed


def test_a_stop_mid_cycle_takes_the_merge_button_with_it(slack_channel: _Env) -> None:
    """`stop_live_run` while an acceptance is pending. The button was posted to
    a real channel and lives in the real confirmation store; leaving it there
    would hand the next autopilot tick a merge nobody is waiting on any more.

    Asserted against the REAL store, not the driver's own event: the withdrawal
    is only worth anything if the record on disk actually stopped being
    pending."""
    from errorta_slack import store as slack_store

    env = slack_channel
    _fix_profile(env, Path(env.ledger_store.get_project().repo_path))
    env.timing["accept_timeout_s"] = 600.0      # nothing may time out under us
    fake_repo = Path(env.ledger_store.get_project().repo_path)

    first = _start_with(env, _fake_dev(env))
    run_id = first["run_id"]
    staged = _detail(env, run_id, "fix_accept_staged")
    assert slack_store.get_confirmation(staged["cid"])["state"] == "pending"

    assert env.mgr.stop(project_id="proj")["status"] == "stopping"

    aborted = _detail(env, run_id, "fix_aborted")
    assert aborted["reason"] == "operator_stop" and aborted["at"] == "await"
    assert aborted["accept_withdrawn"] is True
    assert slack_store.get_confirmation(staged["cid"])["state"] != "pending"
    assert _autopilot(env) == []                     # and the sweep finds nothing

    final = _wait_terminal(env)
    assert final["phase"] == "stopped"
    assert (fake_repo / "app.py").read_text() == BROKEN_APP   # nothing merged
    assert env.store.list_non_terminal() == []                # nothing relaunched


def test_a_stop_during_the_dev_run_cancels_it(slack_channel: _Env) -> None:
    """One level earlier: the dev run is still going. It outlives this
    supervisor unless it is told to stop, through the same cooperative
    `cancel_requested` flag Slack's own `stop_run` sets."""
    env = slack_channel
    _fix_profile(env, Path(env.ledger_store.get_project().repo_path))
    env.timing["idle_timeout_s"] = 600.0        # the stop must be what ends this

    first = _start_with(env, _fake_dev(env, go_idle=True))
    run_id = first["run_id"]
    _wait_for_kind(env, run_id, "fix_run")

    assert env.mgr.stop(project_id="proj")["status"] == "stopping"

    aborted = _detail(env, run_id, "fix_aborted")
    assert aborted["run_cancelled"] is True
    assert env.ledger_store.get_run_state()["cancel_requested"] is True
    assert [e for e in _events(env, run_id) if e["kind"] == "fix_accept_staged"] == []
    assert _wait_terminal(env)["phase"] == "stopped"


def test_dev_repo_read_is_on_for_the_fixture_project(fix_env: _Env) -> None:
    """G-17 is a precondition of the whole slice, not work inside it: a dev
    member that cannot read the repository cannot fix it. The fixture opts in
    exactly as the runbook tells an operator to, and this asserts it took."""
    from errorta_council.coding import autonomy

    assert autonomy.load_policy(fix_env.ledger_store).dev_repo_read is True
