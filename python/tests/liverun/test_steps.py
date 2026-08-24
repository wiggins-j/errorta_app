# python/tests/liverun/test_steps.py
from __future__ import annotations

import http.server
import os
import stat
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from errorta_liverun import profile as P
from errorta_liverun import steps as S
from errorta_policy import PolicyAction, PolicyDecision
from errorta_tools.runner.remote import RemoteToolRunner
from errorta_tunnels.manager import TunnelManager


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def fake_ssh(tmp_path: Path) -> Path:
    script = tmp_path / "ssh"
    script.write_text("#!/bin/sh\nwhile [ \"$1\" != \"--\" ]; do shift; done; shift\neval \"$1\"\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


@pytest.fixture
def http_state():
    body = {"value": b'{"gameState":"LOGGED_IN","seq":1}'}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.end_headers(); self.wfile.write(body["value"])
        def log_message(self, *a): pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
    yield f"http://127.0.0.1:{srv.server_port}/state", body
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def redirect_server():
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", "http://example.invalid/should-not-be-fetched")
            self.end_headers()
        def log_message(self, *a): pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
    yield f"http://127.0.0.1:{srv.server_port}/x"
    srv.shutdown()
    srv.server_close()


def _profile(tmp_path: Path, local_argvs=(), remote_argvs=()) -> P.Profile:
    hosts = {"box": P.Host("localhost")}
    launch = [P.Step("l", P.Action("local", {"argv": tuple(a), "cwd": None}), None, 5) for a in local_argvs]
    launch += [P.Step("r", P.Action("remote", {"host": "box", "argv": tuple(a), "detach": False,
                                                "pidfile": None, "stdin_file": None, "log": None}), None, 5)
               for a in remote_argvs]
    return P.Profile("p", hosts, {}, tuple(launch), (), (), (), P.DEFAULT_CAPS, ())


def _ctx(tmp_path: Path, prof: P.Profile, ssh: Path | None = None) -> S.Ctx:
    return S.Ctx(profile=prof, run_id="RUN1", session_id="SESS1", evidence_dir=tmp_path / "ev",
                 tunnels=TunnelManager(), remote=RemoteToolRunner(ssh_bin=str(ssh) if ssh else "ssh"),
                 owned_pgids=[], owned_remote_pidfiles=[], owned_tunnels=[], last_values={},
                 launched_monotonic=None)


def _profile_with_step(tmp_path: Path, action: P.Action) -> P.Profile:
    return P.Profile("p", {"box": P.Host("localhost")}, {}, (P.Step("s", action, None, 5),),
                     (), (), (), P.DEFAULT_CAPS, ())


def test_substitute_only_known_tokens() -> None:
    prof = _profile(Path("/"), local_argvs=[["/bin/echo", "$SESSION_ID", "$RUN_ID", "$HOME"]])
    ctx = _ctx(Path("/tmp"), prof)
    assert S.substitute(("/bin/echo", "$SESSION_ID", "$RUN_ID", "$HOME"), ctx) == \
        ("/bin/echo", "SESS1", "RUN1", "$HOME")


def test_local_action_runs_and_records_pgid(tmp_path: Path) -> None:
    prof = _profile(tmp_path, local_argvs=[["/bin/echo", "hello $RUN_ID"]])
    ctx = _ctx(tmp_path, prof)
    res = S.run_action(prof.launch[0].action, ctx, timeout_s=5)
    assert res.ok and "hello RUN1" in res.stdout_tail
    assert res.pgid is not None
    # owned_pgids must only ever hold LIVE pgids: a stale entry left behind
    # after the child exits is a future SIGKILL aimed at a stranger (pid
    # reuse), and killpg does not raise on a reused pgid — so it's pruned on
    # reap, not left dangling after run_action returns.
    assert ctx.owned_pgids == []


def test_local_action_owns_pgid_during_lifetime(tmp_path: Path) -> None:
    prof = _profile(tmp_path, local_argvs=[["/bin/sleep", "2"]])
    ctx = _ctx(tmp_path, prof)
    result_box: dict[str, S.StepResult] = {}

    def _run() -> None:
        result_box["res"] = S.run_action(prof.launch[0].action, ctx, timeout_s=5)

    t = threading.Thread(target=_run)
    t.start()
    deadline = time.monotonic() + 2
    seen_alive = False
    while time.monotonic() < deadline and not seen_alive:
        if ctx.owned_pgids:
            seen_alive = True
        time.sleep(0.02)
    assert seen_alive, "pgid was never observed in ctx.owned_pgids while the child was alive"
    t.join(timeout=5)
    assert result_box["res"].ok
    assert ctx.owned_pgids == []


def test_local_action_rejects_foreign_argv(tmp_path: Path) -> None:
    prof = _profile(tmp_path, local_argvs=[["/bin/echo", "ok"]])
    ctx = _ctx(tmp_path, prof)
    with pytest.raises(S.ArgvIdentityError):
        S.run_action(P.Action("local", {"argv": ("/bin/echo", "pwned"), "cwd": None}), ctx, timeout_s=5)


def test_local_timeout_kills_group(tmp_path: Path) -> None:
    marker = tmp_path / "gc"
    argv = ["/bin/sh", "-c", f"sleep 30 & echo $! > {marker}; wait"]
    prof = _profile(tmp_path, local_argvs=[argv])
    ctx = _ctx(tmp_path, prof)
    res = S.run_action(prof.launch[0].action, ctx, timeout_s=0.5)
    assert res.timed_out and not res.ok
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(int(marker.read_text()), 0)
    assert ctx.owned_pgids == []


def test_remote_action_via_fake_ssh(tmp_path: Path, fake_ssh: Path) -> None:
    prof = _profile(tmp_path, remote_argvs=[["echo", "remote $SESSION_ID"]])
    ctx = _ctx(tmp_path, prof, fake_ssh)
    res = S.run_action(prof.launch[0].action, ctx, timeout_s=5)
    assert res.ok and "remote SESS1" in res.stdout_tail


def test_remote_detach_records_pidfile(tmp_path: Path, fake_ssh: Path) -> None:
    pidfile = str(tmp_path / "b.pid"); log = str(tmp_path / "b.log")
    action = P.Action("remote", {"host": "box", "argv": ("sleep", "5"), "detach": True,
                                 "pidfile": pidfile, "stdin_file": None, "log": log})
    prof = _profile_with_step(tmp_path, action)
    ctx = _ctx(tmp_path, prof, fake_ssh)
    res = S.run_action(action, ctx, timeout_s=5)
    assert res.ok
    assert ctx.owned_remote_pidfiles == [{"host": "box", "pidfile": pidfile}]
    time.sleep(0.3)
    pid = int(Path(pidfile).read_text().strip())
    os.kill(pid, 0)  # alive
    assert S.run_check(P.Check("remote_pid_alive", {"host": "box", "pidfile": pidfile}), ctx, step_start=0.0)
    sig = P.Action("remote_signal", {"host": "box", "pidfile": pidfile, "signal": "TERM", "grace_s": 1, "then": "KILL"})
    assert S.run_action(sig, ctx, timeout_s=5).ok
    time.sleep(0.3)
    assert not S.run_check(P.Check("remote_pid_alive", {"host": "box", "pidfile": pidfile}), ctx, step_start=0.0)


def test_remote_detach_bad_pidfile_dir_fails(tmp_path: Path, fake_ssh: Path) -> None:
    pidfile = str(tmp_path / "no-such-dir" / "b.pid")
    log = str(tmp_path / "b.log")
    # A distinctive, essentially-unique sleep duration doubles as the argv
    # `pgrep -f` needs to find (or fail to find) the orphaned child below.
    duration = str(210000 + (os.getpid() % 9000))
    action = P.Action("remote", {"host": "box", "argv": ("sleep", duration), "detach": True,
                                 "pidfile": pidfile, "stdin_file": None, "log": log})
    prof = _profile_with_step(tmp_path, action)
    ctx = _ctx(tmp_path, prof, fake_ssh)
    try:
        res = S.run_action(action, ctx, timeout_s=5)
        assert not res.ok
        assert ctx.owned_remote_pidfiles == []
        assert not Path(pidfile).exists()
        # The pidfile write failed, but the child had already spawned by
        # then — the wrapper must kill it rather than leave an orphan that
        # nothing will ever signal again.
        time.sleep(0.3)
        survivors = subprocess.run(
            ["pgrep", "-f", f"sleep {duration}"], capture_output=True, text=True
        ).stdout.split()
        assert survivors == [], f"orphaned process(es) still running: {survivors}"
    finally:
        # Best-effort cleanup even on assertion failure, so a regression in
        # this test doesn't leave a real `sleep` process running for hours.
        subprocess.run(["pkill", "-f", f"sleep {duration}"], capture_output=True)


def test_remote_detach_nonexistent_command_fails(tmp_path: Path, fake_ssh: Path) -> None:
    pidfile = str(tmp_path / "c.pid"); log = str(tmp_path / "c.log")
    action = P.Action("remote", {"host": "box", "argv": ("definitely-not-a-real-binary-xyz",), "detach": True,
                                 "pidfile": pidfile, "stdin_file": None, "log": log})
    prof = _profile_with_step(tmp_path, action)
    ctx = _ctx(tmp_path, prof, fake_ssh)
    res = S.run_action(action, ctx, timeout_s=5)
    assert not res.ok
    assert ctx.owned_remote_pidfiles == []


def test_remote_detach_tilde_pidfile(tmp_path: Path, fake_ssh: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    action = P.Action("remote", {"host": "box", "argv": ("sleep", "2"), "detach": True,
                                 "pidfile": "~/x.pid", "stdin_file": None, "log": "~/x.log"})
    prof = _profile_with_step(tmp_path, action)
    ctx = _ctx(tmp_path, prof, fake_ssh)
    res = S.run_action(action, ctx, timeout_s=5)
    assert res.ok
    assert ctx.owned_remote_pidfiles == [{"host": "box", "pidfile": "~/x.pid"}]
    assert (tmp_path / "x.pid").exists()
    pid = int((tmp_path / "x.pid").read_text().strip())
    os.kill(pid, 0)


def test_remote_detach_uses_setsid_when_available(
    tmp_path: Path, fake_ssh: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "setsid.used"
    setsid = tmp_path / "setsid"
    setsid.write_text(f"#!/bin/sh\necho used >> {marker}\nexec \"$@\"\n")
    setsid.chmod(setsid.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")

    pidfile = str(tmp_path / "e.pid"); log = str(tmp_path / "e.log")
    action = P.Action("remote", {"host": "box", "argv": ("sleep", "2"), "detach": True,
                                 "pidfile": pidfile, "stdin_file": None, "log": log})
    prof = _profile_with_step(tmp_path, action)
    ctx = _ctx(tmp_path, prof, fake_ssh)
    res = S.run_action(action, ctx, timeout_s=5)
    assert res.ok
    assert marker.exists()  # the setsid shim actually ran
    pid = int(Path(pidfile).read_text().strip())
    os.kill(pid, 0)


def test_remote_stdin_file_non_detached(tmp_path: Path, fake_ssh: Path) -> None:
    stdin_file = tmp_path / "in.txt"; stdin_file.write_text("hello-stdin")
    action = P.Action("remote", {"host": "box", "argv": ("cat",), "detach": False,
                                 "pidfile": None, "stdin_file": str(stdin_file), "log": None})
    prof = _profile_with_step(tmp_path, action)
    ctx = _ctx(tmp_path, prof, fake_ssh)
    res = S.run_action(action, ctx, timeout_s=5)
    assert res.ok
    assert "hello-stdin" in res.stdout_tail


def test_remote_detach_stdin_file_and_tempfile_cleanup(
    tmp_path: Path, fake_ssh: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `cat` alone would consume the tiny payload and exit almost instantly —
    # too fast for the wrapper's post-spawn `kill -0` liveness check, which
    # this test also wants to exercise. `cat; sleep 2` reads+echoes stdin
    # into the log (proving the token arrived) and then stays alive.
    stdin_file = tmp_path / "in2.txt"; stdin_file.write_text("payload-xyz")
    pidfile = str(tmp_path / "d.pid"); log = str(tmp_path / "d.log")
    action = P.Action("remote", {"host": "box", "argv": ("sh", "-c", "cat; sleep 2"), "detach": True,
                                 "pidfile": pidfile, "stdin_file": str(stdin_file), "log": log})
    prof = _profile_with_step(tmp_path, action)
    ctx = _ctx(tmp_path, prof, fake_ssh)

    # Point the (fake) remote's mktemp at a private, empty per-test directory
    # instead of counting entries in the shared system tmp dir — that count
    # is flake-prone under a concurrently-running test suite (or anything
    # else on the machine) creating/removing its own unrelated tmp files in
    # the same window.
    remote_tmp = tmp_path / "remote_tmp"
    remote_tmp.mkdir()
    monkeypatch.setenv("TMPDIR", str(remote_tmp))

    res = S.run_action(action, ctx, timeout_s=5)

    assert res.ok
    assert list(remote_tmp.iterdir()) == []  # the mktemp token file left nothing behind
    pid = int(Path(pidfile).read_text().strip())
    os.kill(pid, 0)  # still alive (sleep 2)
    assert Path(log).read_text().strip() == "payload-xyz"


def test_http_checks_and_probe(tmp_path: Path, http_state) -> None:
    url, body = http_state
    ctx = _ctx(tmp_path, _profile(tmp_path))
    assert S.run_check(P.Check("http", {"url": url, "expect_status": 200}), ctx, step_start=0.0)
    assert not S.run_check(P.Check("http_json", {"url": url, "path": "gameState", "not_equals": "LOGGED_IN"}), ctx, step_start=0.0)
    body["value"] = b'{"gameState":"LOGIN_SCREEN"}'
    assert S.run_check(P.Check("http_json", {"url": url, "path": "gameState", "not_equals": "LOGGED_IN"}), ctx, step_start=0.0)
    assert S.run_probe(P.Probe("http", {"url": url}), ctx)
    assert not S.run_probe(P.Probe("http", {"url": "http://127.0.0.1:1/x"}), ctx)


def test_http_refuses_redirect(tmp_path: Path, redirect_server: str) -> None:
    ctx = _ctx(tmp_path, _profile(tmp_path))
    assert not S.run_check(P.Check("http", {"url": redirect_server, "expect_status": 200}), ctx, step_start=0.0)
    assert not S.run_probe(P.Probe("http", {"url": redirect_server}), ctx)


def test_file_checks(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, _profile(tmp_path))
    f = tmp_path / "jar"; f.write_text("x")
    assert S.run_check(P.Check("file_exists", str(f)), ctx, step_start=0.0)
    assert not S.run_check(P.Check("file_exists", str(tmp_path / "nope")), ctx, step_start=0.0)
    assert S.run_check(P.Check("file_mtime_newer", {"path": str(f), "than": "step_start"}), ctx, step_start=f.stat().st_mtime - 1)
    assert not S.run_check(P.Check("file_mtime_newer", {"path": str(f), "than": "step_start"}), ctx, step_start=f.stat().st_mtime + 1)


def test_advancing_probes(tmp_path: Path, fake_ssh: Path) -> None:
    ctx = _ctx(tmp_path, _profile(tmp_path, remote_argvs=[["cat", str(tmp_path / "seq")]]), fake_ssh)
    seqf = tmp_path / "seq"; seqf.write_text("1")
    probe = P.Probe("remote_stdout_advancing", {"host": "box", "argv": ("cat", str(seqf))})
    assert S.run_probe(probe, ctx) is True      # first observation counts as ok
    assert S.run_probe(probe, ctx) is False     # unchanged
    seqf.write_text("2")
    assert S.run_probe(probe, ctx) is True
    m = P.Probe("remote_stdout_matches", {"host": "box", "argv": ("cat", str(seqf)), "regex": r"^2$"})
    assert S.run_probe(m, ctx) is True


def test_elapsed_probe(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, _profile(tmp_path))
    ctx.launched_monotonic = ctx.clock() - 100
    assert S.run_probe(P.Probe("elapsed_lt_s", 200), ctx)
    assert not S.run_probe(P.Probe("elapsed_lt_s", 50), ctx)


def test_all_check_and_pgrep(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, _profile(tmp_path))
    assert S.run_check(P.Check("pgrep_absent", "definitely-not-a-real-process-name-xyz"), ctx, step_start=0.0)
    assert S.run_check(P.Check("all", (P.Check("pgrep_absent", "nope-xyz"), P.Check("file_exists", "/"))), ctx, step_start=0.0)
    assert not S.run_check(P.Check("all", (P.Check("file_exists", "/"), P.Check("file_exists", "/nope"))), ctx, step_start=0.0)


def test_evidence_redacted(tmp_path: Path) -> None:
    prof = _profile(tmp_path, local_argvs=[["/bin/echo", "token=sk-abcdefghijklmnopqrstuvwxyz0123456789"]])
    ctx = _ctx(tmp_path, prof)
    res = S.run_action(prof.launch[0].action, ctx, timeout_s=5)
    assert "sk-abcdefghijklmnopqrstuvwxyz0123456789" not in res.stdout_tail


def test_exit0_check_rejects_foreign_argv(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, _profile(tmp_path))
    with pytest.raises(S.ArgvIdentityError):
        S.run_check(P.Check("exit0", {"argv": ("/bin/echo", "nope")}), ctx, step_start=0.0)


def test_exit0_check_tracks_and_prunes_pgid(tmp_path: Path) -> None:
    prof = _profile(tmp_path)
    check = P.Check("exit0", {"argv": ("/bin/echo", "ok")})
    # Route the check's argv through the profile's declared set the same way
    # _profile does for actions, so _guard accepts it.
    launch = (P.Step("c", None, check, 5),)
    prof = P.Profile("p", {"box": P.Host("localhost")}, {}, launch, (), (), (), P.DEFAULT_CAPS, ())
    ctx = _ctx(tmp_path, prof)
    assert S.run_check(check, ctx, step_start=0.0)
    assert ctx.owned_pgids == []  # tracked-spawn helper pruned it on reap


def test_remote_stdout_advancing_rejects_foreign_argv(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, _profile(tmp_path))
    probe = P.Probe("remote_stdout_advancing", {"host": "box", "argv": ("cat", "/etc/hosts")})
    with pytest.raises(S.ArgvIdentityError):
        S.run_probe(probe, ctx)


def test_remote_action_blocked_by_policy(
    tmp_path: Path, fake_ssh: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _deny(request, *, phase=None, policy=None):
        return PolicyDecision(action=PolicyAction.DENY, reason_code="test_denied")

    monkeypatch.setattr(S, "evaluate_runner_launch", _deny)
    prof = _profile(tmp_path, remote_argvs=[["echo", "hi"]])
    ctx = _ctx(tmp_path, prof, fake_ssh)
    res = S.run_action(prof.launch[0].action, ctx, timeout_s=5)
    assert not res.ok
    assert res.detail == "test_denied"


class _FakeTunnels:
    """Double for TunnelManager: ensure/close/status_for only, no real ssh."""

    def __init__(self) -> None:
        self.ensured: list = []
        self.closed: list = []
        self._up: set = set()

    def ensure(self, spec, *, wait: bool = True) -> int:
        self.ensured.append(spec)
        self._up.add(spec)
        return 40000

    def close(self, spec) -> bool:
        self.closed.append(spec)
        if spec in self._up:
            self._up.discard(spec)
            return True
        return False

    def status_for(self, spec):
        return {"state": "up"} if spec in self._up else None


def test_tunnel_action_and_check_and_close(tmp_path: Path) -> None:
    tdef = P.TunnelDef("t1", "box", ((9000, 9001),))
    prof = P.Profile("p", {"box": P.Host("localhost")}, {"t1": tdef}, (), (), (), (), P.DEFAULT_CAPS, ())
    fake = _FakeTunnels()
    ctx = S.Ctx(profile=prof, run_id="RUN1", session_id="SESS1", evidence_dir=tmp_path / "ev",
               tunnels=fake, remote=RemoteToolRunner(ssh_bin="ssh"),
               owned_pgids=[], owned_remote_pidfiles=[], owned_tunnels=[], last_values={},
               launched_monotonic=None)

    res = S.run_action(P.Action("tunnel", {"id": "t1"}), ctx, timeout_s=5)
    assert res.ok
    assert ctx.owned_tunnels == ["t1"]
    assert len(fake.ensured) == 1
    assert S.run_check(P.Check("tunnel_up", "t1"), ctx, step_start=0.0)

    res2 = S.run_action(P.Action("tunnel_close", {"id": "t1"}), ctx, timeout_s=5)
    assert res2.ok
    assert ctx.owned_tunnels == []
    assert len(fake.closed) == 1
    assert not S.run_check(P.Check("tunnel_up", "t1"), ctx, step_start=0.0)


@pytest.mark.parametrize("bad_pid", ["-1", "0"])
def test_remote_pid_alive_and_signal_reject_non_numeric_pidfile(
    tmp_path: Path, fake_ssh: Path, bad_pid: str
) -> None:
    # "-1" is the classic "signal every process this user can reach"
    # broadcast target; "0" means "every process in the caller's own
    # process group" — both are dangerous to ever pass through unguarded,
    # neither is a valid single-process pid.
    pidfile = tmp_path / "bad.pid"; pidfile.write_text(bad_pid)
    ctx = _ctx(tmp_path, _profile(tmp_path), fake_ssh)
    assert not S.run_check(
        P.Check("remote_pid_alive", {"host": "box", "pidfile": str(pidfile)}), ctx, step_start=0.0)
    sig = P.Action("remote_signal", {"host": "box", "pidfile": str(pidfile), "signal": "TERM",
                                     "grace_s": 0.1, "then": "KILL"})
    res = S.run_action(sig, ctx, timeout_s=5)
    assert res.ok  # the guard makes this a clean no-op, not an actual broadcast kill
    # If the guard had NOT fired, this would have signalled every process
    # this user can reach (or the whole process group). Confirm this very
    # test process is still here.
    os.kill(os.getpid(), 0)


# --- tri-state remote probes: transport failure is unknown, not dead ------ #
#
# Live 2026-08-23 (run aaadac): a ~64s tailscale flake made ssh itself fail.
# `_remote_pid_alive` returned False on that, indistinguishable from a real
# `kill -0` failure -- the supervisor stopped a HEALTHY run, and triage
# classified it as a dead brain. These probe-level tests fix that at the
# source: a transport failure (ssh exit 255, a runner timeout) must not look
# like a definite "no".

def _fake_remote(status: str, exit_code: int | None, stdout: str = ""):
    def _fn(ctx, host, argv, timeout_s=20, **kw):
        return SimpleNamespace(status=status, exit_code=exit_code, stdout_preview=stdout)
    return _fn


def _watch_ctx(tmp_path: Path, probe: P.Probe) -> S.Ctx:
    """A profile whose sole watch entry IS this probe -- so an argv-based
    probe's argv is declared and `_guard` does not reject it."""
    prof = P.Profile("p", {"box": P.Host("localhost")}, {},
                     (), (P.WatchProbe("w", 10, 30, "stop", probe),), (), (),
                     P.DEFAULT_CAPS, ())
    return _ctx(tmp_path, prof)


def test_remote_pid_alive_is_none_on_ssh_transport_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ssh exiting 255 means the box could not be reached at all -- not that
    the pid is dead."""
    monkeypatch.setattr(S, "_remote", _fake_remote("failed", 255))
    ctx = _ctx(tmp_path, _profile(tmp_path))
    assert S._remote_pid_alive(ctx, "box", "/p.pid") is None


def test_remote_pid_alive_is_none_on_a_runner_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(S, "_remote", _fake_remote("timed_out", None))
    ctx = _ctx(tmp_path, _profile(tmp_path))
    assert S._remote_pid_alive(ctx, "box", "/p.pid") is None


def test_remote_pid_alive_is_none_when_the_runner_blocks_the_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(S, "_remote", _fake_remote("blocked", None))
    ctx = _ctx(tmp_path, _profile(tmp_path))
    assert S._remote_pid_alive(ctx, "box", "/p.pid") is None


def test_remote_pid_alive_is_false_on_a_clean_kill_dash_zero_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A command that actually RAN and reported the pid gone (exit 1, not
    255) is a real, definite failure -- ssh worked fine."""
    monkeypatch.setattr(S, "_remote", _fake_remote("failed", 1))
    ctx = _ctx(tmp_path, _profile(tmp_path))
    assert S._remote_pid_alive(ctx, "box", "/p.pid") is False


def test_remote_pid_alive_is_true_on_completed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(S, "_remote", _fake_remote("completed", 0))
    ctx = _ctx(tmp_path, _profile(tmp_path))
    assert S._remote_pid_alive(ctx, "box", "/p.pid") is True


@pytest.mark.parametrize("probe_kind,params", [
    ("remote_file_mtime_advancing", {"host": "box", "path": "/l.log"}),
    ("remote_stdout_advancing", {"host": "box", "argv": ("cat", "/j")}),
    ("remote_stdout_matches", {"host": "box", "argv": ("cat", "/j"), "regex": "x"}),
])
def test_run_probe_is_none_on_ssh_transport_failure_for_every_remote_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, probe_kind: str, params: dict
) -> None:
    monkeypatch.setattr(S, "_remote", _fake_remote("failed", 255))
    probe = P.Probe(probe_kind, params)
    assert S.run_probe(probe, _watch_ctx(tmp_path, probe)) is None


def test_run_probe_remote_stdout_matches_is_false_on_a_clean_non_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(S, "_remote", _fake_remote("completed", 0, stdout="no match here"))
    probe = P.Probe("remote_stdout_matches", {"host": "box", "argv": ("cat", "/j"), "regex": "x"})
    assert S.run_probe(probe, _watch_ctx(tmp_path, probe)) is False


def _profile_with_deploy(action: P.Action, check: P.Check | None = None) -> P.Profile:
    repo = P.RepoDef("brain", "/r/brain", "brain-proj", True, (),
                     (P.Step("rsync", action, check, 30),))
    return P.Profile("p", {"box": P.Host("localhost")}, {}, (), (), (), (), P.DEFAULT_CAPS,
                     (), (repo,), P.FixLoop(enabled=True))


def test_a_declared_deploy_argv_is_accepted_by_the_identity_guard(tmp_path: Path) -> None:
    """A repo's `deploy:` steps are validated at load time exactly like launch
    steps, so their argvs are declared argvs — without this the fix loop's
    deploy phase could not run its own profile's step at all."""
    action = P.Action("local", {"argv": ("/bin/echo", "deployed"), "cwd": None})
    ctx = _ctx(tmp_path, _profile_with_deploy(action))
    res = S.run_action(action, ctx, timeout_s=5)
    assert res.ok and "deployed" in res.stdout_tail


def test_a_deploy_steps_check_argv_is_declared_too(tmp_path: Path) -> None:
    action = P.Action("local", {"argv": ("/bin/echo", "deployed"), "cwd": None})
    check = P.Check("exit0", {"argv": ("/bin/echo", "checked")})
    ctx = _ctx(tmp_path, _profile_with_deploy(action, check))
    assert S.run_check(check, ctx, step_start=0.0)


def test_an_undeclared_argv_is_still_refused_when_deploy_steps_exist(tmp_path: Path) -> None:
    action = P.Action("local", {"argv": ("/bin/echo", "deployed"), "cwd": None})
    ctx = _ctx(tmp_path, _profile_with_deploy(action))
    with pytest.raises(S.ArgvIdentityError):
        S.run_action(P.Action("local", {"argv": ("/bin/echo", "pwned"), "cwd": None}),
                     ctx, timeout_s=5)


def test_window_shot_names_the_missing_quartz(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(S, "_pgrep", lambda pattern: [4242])
    monkeypatch.setattr(S, "quartz_available", lambda: False)
    monkeypatch.setattr(S.sys, "platform", "darwin")
    ctx = _ctx(tmp_path, _profile(tmp_path))
    res = S._run_window_shot({"pgrep": "x"}, ctx, 5)
    assert res.ok is False and res.detail == "quartz_unavailable"


def test_window_shot_names_a_missing_process(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(S, "_pgrep", lambda pattern: [])
    ctx = _ctx(tmp_path, _profile(tmp_path))
    res = S._run_window_shot({"pgrep": "x"}, ctx, 5)
    assert res.ok is False and res.detail == "no process matched"


def test_window_shot_reports_an_uncapturable_window(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(S, "_pgrep", lambda pattern: [4242])
    monkeypatch.setattr(S, "quartz_available", lambda: True)
    monkeypatch.setattr(S, "capture_app_window", lambda *, pids, out_path: False)
    ctx = _ctx(tmp_path, _profile(tmp_path))
    res = S._run_window_shot({"pgrep": "x"}, ctx, 5)
    assert res.ok is False and res.detail == "no window captured"
