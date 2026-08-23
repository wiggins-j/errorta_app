"""Unsandboxed trusted-tier execution mechanics (spec 2026-08-23-trusted-gate).

This exercises errorta_tools.runner.trusted_exec directly — the actual
subprocess launch, kill-group, and bounded-drain machinery. errorta_council
may not import subprocess itself (see test_tool_runner_local.py and
test_toolgateway_slice1.py), so the council-facing wrapper
(errorta_council.coding.trusted_gate.run_trusted_command) only delegates
here and converts the result; that delegation is smoke-tested in
tests/council/test_trusted_gate.py, and is not re-tested below.

Two macOS realities shape a few of these tests: there is no standalone
`setsid` binary, so "detach into a new session" is done with a small
python3 fork() + os.setsid() spawner instead of shelling out to `setsid`;
and killpg can raise PermissionError (EPERM), not ProcessLookupError, when
the group's only member is a just-exited zombie.
"""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

import pytest

from errorta_tools.runner import trusted_exec as te


def _write_script(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(body)
    return p


def _busy_grandchild_script(tmp_path: Path) -> Path:
    """A process that forks, detaches into its own session, and then writes
    continuously — so the pipe is always readable, never idle."""
    return _write_script(tmp_path, "busy_grandchild.py", (
        "import os, sys, time\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    os.setsid()\n"
        "    while True:\n"
        "        sys.stdout.write('x' * 1000)\n"
        "        sys.stdout.flush()\n"
        "        time.sleep(0.01)\n"
        "else:\n"
        "    sys.exit(0)\n"
    ))


def _sleepy_grandchild_script(tmp_path: Path, seconds: int) -> Path:
    """A process that forks, detaches into its own session, sleeps (holding
    the inherited stdout/stderr fds open), then exits."""
    return _write_script(tmp_path, "sleepy_grandchild.py", (
        "import os, sys, time\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    os.setsid()\n"
        f"    time.sleep({seconds})\n"
        "    sys.exit(0)\n"
        "else:\n"
        "    sys.exit(0)\n"
    ))


def test_passthrough_env_copies_only_listed_non_secret_names(monkeypatch) -> None:
    monkeypatch.setenv("JAVA_HOME", "/jdk")
    monkeypatch.setenv("MY_API_KEY", "s3cret")
    env = te.passthrough_env(("JAVA_HOME", "MY_API_KEY", "NOT_SET_ANYWHERE_X"))
    assert env == {"JAVA_HOME": "/jdk"}


def test_run_trusted_command_passes_and_records_trusted(tmp_path: Path) -> None:
    res = te.run_trusted_command(
        {"argv": ["/usr/bin/true"], "cwd": ".", "timeout_seconds": 5},
        command_id="ok", workspace_root=tmp_path, env_passthrough=("PATH",))
    assert res.passed and res.status == "completed" and res.exit_code == 0
    assert res.command_id == "ok" and len(res.argv_sha256) == 64


def test_run_trusted_command_failure_is_a_real_red(tmp_path: Path) -> None:
    res = te.run_trusted_command(
        {"argv": ["/usr/bin/false"], "cwd": ".", "timeout_seconds": 5},
        command_id="no", workspace_root=tmp_path, env_passthrough=())
    assert not res.passed and res.status == "failed"
    assert res.exit_code == 1 and res.reason == "exit 1"


def test_run_trusted_command_times_out_and_kills_the_group(tmp_path: Path) -> None:
    t0 = time.monotonic()
    res = te.run_trusted_command(
        {"argv": ["/bin/sleep", "30"], "cwd": ".", "timeout_seconds": 1},
        command_id="slow", workspace_root=tmp_path, env_passthrough=())
    assert res.status == "timed_out" and not res.passed and "timed out" in res.reason
    assert time.monotonic() - t0 < 10


def test_run_trusted_command_cwd_is_inside_the_workspace(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    res = te.run_trusted_command(
        {"argv": ["/bin/pwd"], "cwd": "sub", "timeout_seconds": 5},
        command_id="pwd", workspace_root=tmp_path, env_passthrough=())
    assert res.passed and str((tmp_path / "sub").resolve()) in res.stdout_preview


def test_run_trusted_command_invalid_marker_is_blocked(tmp_path: Path) -> None:
    spec = {"tier": "trusted", "invalid": "gate_mode_insecure", "argv": [], "cwd": ".",
            "timeout_seconds": 1, "scope": "unit"}
    res = te.run_trusted_command(
        spec, command_id="trusted-gate", workspace_root=tmp_path, env_passthrough=())
    assert res.status == "blocked" and not res.passed
    assert res.reason == "trusted_gate_invalid:gate_mode_insecure"


def test_run_trusted_command_cancel_before_launch(tmp_path: Path) -> None:
    res = te.run_trusted_command(
        {"argv": ["/usr/bin/true"], "cwd": ".", "timeout_seconds": 5},
        command_id="c", workspace_root=tmp_path, env_passthrough=(),
        should_cancel=lambda: True)
    assert res.status == "blocked" and res.reason == "cancelled before launch"


def test_run_trusted_command_empty_argv_is_blocked(tmp_path: Path) -> None:
    res = te.run_trusted_command(
        {"argv": [], "cwd": ".", "timeout_seconds": 5},
        command_id="empty", workspace_root=tmp_path, env_passthrough=())
    assert res.status == "blocked" and not res.passed and res.reason == "empty_argv"


def test_run_trusted_command_caps_output_like_the_sandboxed_tier(tmp_path: Path) -> None:
    script = "head -c 3000000 /dev/zero | tr '\\0' a"
    res = te.run_trusted_command(
        {"argv": ["/bin/sh", "-c", script], "cwd": ".", "timeout_seconds": 10},
        command_id="big", workspace_root=tmp_path, env_passthrough=())
    assert res.passed and res.status == "completed"
    expected = hashlib.sha256(b"a" * te.MAX_OUTPUT_BYTES).hexdigest()
    assert res.stdout_sha256 == expected


def test_run_trusted_command_cwd_symlink_escaping_workspace_is_blocked(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-workspace"
    outside.mkdir(exist_ok=True)
    (tmp_path / "escape").symlink_to(outside)
    res = te.run_trusted_command(
        {"argv": ["/bin/pwd"], "cwd": "escape", "timeout_seconds": 5},
        command_id="escape", workspace_root=tmp_path, env_passthrough=())
    assert res.status == "blocked" and res.reason == "cwd_outside_workspace"


def test_run_trusted_command_missing_binary_is_a_labeled_failure(tmp_path: Path) -> None:
    res = te.run_trusted_command(
        {"argv": ["/nonexistent/bin"], "cwd": ".", "timeout_seconds": 5},
        command_id="nf", workspace_root=tmp_path, env_passthrough=())
    assert res.status == "failed" and not res.passed and res.exit_code is None
    assert res.reason == "launch failed: FileNotFoundError"


def test_run_trusted_command_passthrough_value_reaches_the_child(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_VAR", "reaches-the-child")
    res = te.run_trusted_command(
        {"argv": ["/usr/bin/env"], "cwd": ".", "timeout_seconds": 5},
        command_id="envtest", workspace_root=tmp_path, env_passthrough=("MY_VAR",))
    assert res.passed and "MY_VAR=reaches-the-child" in res.stdout_preview


def test_run_trusted_command_killpg_eperm_returns_within_bound(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # B1: macOS raises PermissionError (EPERM), not ProcessLookupError, from
    # os.killpg when the group's only member is a just-exited zombie. Both
    # killpg calls (TERM and KILL) must tolerate this without an exception
    # escaping, and the final proc.wait() must still be bounded (falling
    # back to a direct proc.kill()) rather than hanging for the command's
    # full duration.
    monkeypatch.setattr(os, "killpg", lambda pid, sig: (_ for _ in ()).throw(PermissionError))
    t0 = time.monotonic()
    res = te.run_trusted_command(
        {"argv": ["/bin/sleep", "25"], "cwd": ".", "timeout_seconds": 1},
        command_id="eperm", workspace_root=tmp_path, env_passthrough=())
    assert res.status == "timed_out" and not res.passed
    assert time.monotonic() - t0 < 12


def test_drain_deadline_is_checked_even_against_a_busy_writer(tmp_path: Path) -> None:
    # B2: the deadline check must run at the TOP of _drain's loop, not only
    # in the "select was empty" branch — otherwise a writer that keeps the
    # pipe continuously readable (a detached grandchild spinning out output)
    # starves the check and the reader thread never stops.
    script_path = _busy_grandchild_script(tmp_path)
    cmd = f"python3 {script_path} & sleep 30"
    threads: dict = {}
    res = te.run_trusted_command(
        {"argv": ["/bin/sh", "-c", cmd], "cwd": ".", "timeout_seconds": 1},
        command_id="busy", workspace_root=tmp_path, env_passthrough=("PATH",),
        _expose_threads=threads)
    assert res.status == "timed_out" and "output abandoned" in res.reason
    time.sleep(3)
    assert not threads["out_thread"].is_alive()
    assert not threads["err_thread"].is_alive()


def test_run_trusted_command_passing_with_abandoned_output_notes_it(tmp_path: Path) -> None:
    # B3: a command that exits cleanly (passed=True) must not report an
    # empty, silently-clean reason when its output was actually abandoned —
    # evidence must never read as a clean pass over output we never saw.
    script_path = _sleepy_grandchild_script(tmp_path, seconds=8)
    cmd = f"echo hi; python3 {script_path} &"
    res = te.run_trusted_command(
        {"argv": ["/bin/sh", "-c", cmd], "cwd": ".", "timeout_seconds": 5},
        command_id="passabandon", workspace_root=tmp_path, env_passthrough=("PATH",))
    assert res.passed is True
    assert "abandoned" in res.reason
    # The pre-abandonment output (written by `echo hi` before the grandchild
    # ever backgrounds itself) must be KEPT, not discarded: the abandonment
    # note in `reason` is honest evidence that the capture may be
    # incomplete, but real captured bytes are strictly more useful than none.
    assert "hi" in res.stdout_preview


def test_drain_treats_a_none_read_as_no_data_not_eof() -> None:
    # B4: a non-blocking read() returning None means "nothing available
    # right now", not end-of-file. Conflating the two would truncate a
    # capture the moment a poll raced ahead of the writer.
    class _FakeStream:
        def __init__(self, chunks):
            self._chunks = list(chunks)

        def fileno(self):
            return -1

        def read(self, n):
            return self._chunks.pop(0) if self._chunks else b""

        def close(self):
            pass

    stream = _FakeStream([None, b"hello", b""])
    buf = bytearray()
    state = te._ReaderState()

    def _fake_set_blocking(fd, blocking):
        return None

    def _fake_select(rlist, wlist, xlist, timeout):
        return (rlist, [], [])

    import select as select_module
    orig_set_blocking, orig_select = os.set_blocking, select_module.select
    os.set_blocking = _fake_set_blocking
    select_module.select = _fake_select
    try:
        te._drain(stream, buf, te.MAX_OUTPUT_BYTES, state)
    finally:
        os.set_blocking = orig_set_blocking
        select_module.select = orig_select

    assert bytes(buf) == b"hello"
    assert state.eof is True
    assert state.error is None


def test_run_trusted_command_reader_error_is_labeled_distinctly(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Minor: a genuine failure in OUR OWN read machinery (select/read) must
    # be recorded as a distinct reader_error, not silently folded into the
    # generic "detached child held the pipe" wording, which would misdiagnose
    # a bug in this module as an unrelated grandchild.
    import select as select_module
    monkeypatch.setattr(select_module, "select", lambda *a, **k: (_ for _ in ()).throw(OSError))
    res = te.run_trusted_command(
        {"argv": ["/usr/bin/true"], "cwd": ".", "timeout_seconds": 5},
        command_id="rerr", workspace_root=tmp_path, env_passthrough=())
    assert "reader_error:OSError" in res.reason
