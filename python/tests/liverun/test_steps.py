# python/tests/liverun/test_steps.py
from __future__ import annotations

import http.server
import os
import stat
import threading
import time
from pathlib import Path

import pytest

from errorta_liverun import profile as P
from errorta_liverun import steps as S
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
    assert ctx.owned_pgids  # recorded before/at spawn


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


def test_remote_action_via_fake_ssh(tmp_path: Path, fake_ssh: Path) -> None:
    prof = _profile(tmp_path, remote_argvs=[["echo", "remote $SESSION_ID"]])
    ctx = _ctx(tmp_path, prof, fake_ssh)
    res = S.run_action(prof.launch[0].action, ctx, timeout_s=5)
    assert res.ok and "remote SESS1" in res.stdout_tail


def test_remote_detach_records_pidfile(tmp_path: Path, fake_ssh: Path) -> None:
    pidfile = str(tmp_path / "b.pid"); log = str(tmp_path / "b.log")
    action = P.Action("remote", {"host": "box", "argv": ("sleep", "5"), "detach": True,
                                 "pidfile": pidfile, "stdin_file": None, "log": log})
    prof = P.Profile("p", {"box": P.Host("localhost")}, {}, (P.Step("b", action, None, 5),), (), (), (), P.DEFAULT_CAPS, ())
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


def test_http_checks_and_probe(tmp_path: Path, http_state) -> None:
    url, body = http_state
    ctx = _ctx(tmp_path, _profile(tmp_path))
    assert S.run_check(P.Check("http", {"url": url, "expect_status": 200}), ctx, step_start=0.0)
    assert not S.run_check(P.Check("http_json", {"url": url, "path": "gameState", "not_equals": "LOGGED_IN"}), ctx, step_start=0.0)
    body["value"] = b'{"gameState":"LOGIN_SCREEN"}'
    assert S.run_check(P.Check("http_json", {"url": url, "path": "gameState", "not_equals": "LOGGED_IN"}), ctx, step_start=0.0)
    assert S.run_probe(P.Probe("http", {"url": url}), ctx)
    assert not S.run_probe(P.Probe("http", {"url": "http://127.0.0.1:1/x"}), ctx)


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
