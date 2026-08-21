from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from errorta_tools.runner.remote import RemoteToolRunner, build_remote_ssh_argv
from errorta_tools.runner.types import ToolRunnerRequest


def _req(**kw) -> ToolRunnerRequest:
    base = dict(request_id="r1", run_id="run1", tool_call_id="t1",
                argv=("echo", "hi there"), workspace_root="/tmp",
                execution_location="remote_ssh", timeout_seconds=5,
                metadata={"ssh_host": "box"})
    base.update(kw)
    return ToolRunnerRequest(**base)


@pytest.fixture
def fake_ssh(tmp_path: Path) -> Path:
    """A stand-in `ssh` that records argv + stdin and runs the remote command locally."""
    script = tmp_path / "ssh"
    script.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$@\" > {tmp_path}/argv\n"
        f"cat > {tmp_path}/stdin\n"
        "while [ \"$1\" != \"--\" ]; do shift; done; shift\n"
        "eval \"$1\"\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def test_argv_is_hardened_and_uses_double_dash() -> None:
    argv = build_remote_ssh_argv("box", ["tail", "-n", "5", "a b.log"], ssh_port=2222,
                                 ssh_username="u", ssh_key_path=None)
    assert argv[0] == "ssh"
    for flag in ("BatchMode=yes", "StrictHostKeyChecking=yes", "ConnectTimeout=10",
                 "ServerAliveInterval=15", "ServerAliveCountMax=3"):
        assert flag in argv
    assert "-N" not in argv
    assert argv[argv.index("-p") + 1] == "2222"
    assert argv[-3:] == ["u@box", "--", "tail -n 5 'a b.log'"]


def test_rejects_flag_injection_in_host() -> None:
    with pytest.raises(ValueError):
        build_remote_ssh_argv("-oProxyCommand=evil", ["true"])


def test_run_sync_executes_and_caps_output(fake_ssh: Path, tmp_path: Path) -> None:
    runner = RemoteToolRunner(ssh_bin=str(fake_ssh))
    res = runner.run_sync(_req(argv=("echo", "hi there")))
    assert res.status == "completed" and res.exit_code == 0
    assert res.stdout_preview.strip() == "hi there"
    recorded = (tmp_path / "argv").read_text().splitlines()
    assert recorded[-2:] == ["--", "echo 'hi there'"]


def test_stdin_path_reaches_stdin_not_argv(fake_ssh: Path, tmp_path: Path) -> None:
    secret = tmp_path / "tok"; secret.write_text("s3cr3t")
    runner = RemoteToolRunner(ssh_bin=str(fake_ssh))
    runner.run_sync(_req(argv=("cat",)), stdin_path=str(secret))
    assert (tmp_path / "stdin").read_text() == "s3cr3t"
    assert "s3cr3t" not in (tmp_path / "argv").read_text()


def test_timeout_kills_group_and_reports(fake_ssh: Path) -> None:
    runner = RemoteToolRunner(ssh_bin=str(fake_ssh))
    res = runner.run_sync(_req(argv=("sh", "-c", "sleep 30 & wait"), timeout_seconds=0.5))
    assert res.status == "timed_out" and res.reason_code == "runner_timeout"


def test_wrong_location_is_blocked() -> None:
    res = RemoteToolRunner().run_sync(_req(execution_location="local"))
    assert res.status == "blocked" and res.reason_code == "runner_location_not_remote"


def test_missing_host_is_blocked() -> None:
    res = RemoteToolRunner().run_sync(_req(metadata={}))
    assert res.status == "blocked" and res.reason_code == "remote_host_missing"


async def test_async_run_delegates(fake_ssh: Path) -> None:
    res = await RemoteToolRunner(ssh_bin=str(fake_ssh)).run(_req())
    assert res.status == "completed"


def test_nonzero_remote_exit_is_reported(fake_ssh: Path) -> None:
    runner = RemoteToolRunner(ssh_bin=str(fake_ssh))
    res = runner.run_sync(_req(argv=("sh", "-c", "echo boom 1>&2; exit 3")))
    assert res.status == "failed" and res.exit_code == 3
    assert res.reason_code == "runner_nonzero_exit"
    assert res.log_tail and "boom" in res.log_tail


def test_missing_ssh_binary_is_reported(tmp_path: Path) -> None:
    runner = RemoteToolRunner(ssh_bin=str(tmp_path / "no-such-ssh-binary"))
    res = runner.run_sync(_req())
    assert res.status == "failed" and res.exit_code is None
    assert res.reason_code == "runner_executable_not_found"


def test_invalid_ssh_port_is_blocked() -> None:
    res = RemoteToolRunner().run_sync(_req(metadata={"ssh_host": "box", "ssh_port": 70000}))
    assert res.status == "blocked" and res.reason_code == "remote_argv_invalid"


def test_flag_injecting_username_is_blocked() -> None:
    res = RemoteToolRunner().run_sync(
        _req(metadata={"ssh_host": "box", "ssh_username": "-oProxyCommand=x"}))
    assert res.status == "blocked" and res.reason_code == "remote_argv_invalid"


def test_unreadable_stdin_path_returns_typed_result(tmp_path: Path) -> None:
    runner = RemoteToolRunner()
    res = runner.run_sync(_req(), stdin_path=str(tmp_path / "does-not-exist"))
    assert res.status == "failed" and res.exit_code is None
    assert res.reason_code == "remote_stdin_unreadable"
