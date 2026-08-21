"""Remote ToolRunner: one bounded command over a fixed ``ssh`` argv.

Supervisor egress primitive (live-run), NOT a member tool: it inherits the
real user environment (HOME, SSH_AUTH_SOCK) so ``~/.ssh/config`` and the
agent resolve normally. Never registered in the member tool gateway;
``errorta_council`` must not import it.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shlex
import signal
import subprocess
import time
from typing import Any, Mapping, Sequence

from .types import ToolRunnerRequest, ToolRunnerResult, now_iso

_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _token(value: Any, *, what: str) -> str:
    v = str(value or "").strip()
    if not _TOKEN_RE.match(v):
        raise ValueError(f"invalid {what}: {value!r}")
    return v


def build_remote_ssh_argv(
    host: str, remote_argv: Sequence[str], *, ssh_port: int | None = None,
    ssh_username: str | None = None, ssh_key_path: str | None = None,
    ssh_bin: str = "ssh",
) -> list[str]:
    host = _token(host, what="ssh_host")
    argv = [
        ssh_bin,
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "ConnectTimeout=10",
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=3",
    ]
    if ssh_port is not None:
        port = int(ssh_port)
        if not 1 <= port <= 65535:
            raise ValueError("invalid ssh_port")
        argv += ["-p", str(port)]
    if ssh_key_path:
        argv += ["-i", os.path.expanduser(str(ssh_key_path))]
    target = f"{_token(ssh_username, what='ssh_username')}@{host}" if ssh_username else host
    if not remote_argv:
        raise ValueError("empty remote argv")
    argv += [target, "--", shlex.join(str(a) for a in remote_argv)]
    return argv


class RemoteToolRunner:
    def __init__(self, *, ssh_bin: str = "ssh",
                 source_env: Mapping[str, str] | None = None) -> None:
        self._ssh_bin = ssh_bin
        self._env = dict(source_env) if source_env is not None else None

    async def run(self, request: ToolRunnerRequest) -> ToolRunnerResult:
        return await asyncio.to_thread(self.run_sync, request)

    def run_sync(self, request: ToolRunnerRequest, *,
                 stdin_path: str | None = None) -> ToolRunnerResult:
        if request.execution_location != "remote_ssh":
            return ToolRunnerResult.blocked(
                request=request, reason_code="runner_location_not_remote",
                metadata={"execution_location": request.execution_location})
        host = request.metadata.get("ssh_host")
        if not host:
            return ToolRunnerResult.blocked(request=request, reason_code="remote_host_missing")
        try:
            argv = build_remote_ssh_argv(
                str(host), request.argv,
                ssh_port=request.metadata.get("ssh_port"),
                ssh_username=request.metadata.get("ssh_username"),
                ssh_key_path=request.metadata.get("ssh_key_path"),
                ssh_bin=self._ssh_bin)
        except ValueError as exc:
            return ToolRunnerResult.blocked(
                request=request, reason_code="remote_argv_invalid",
                metadata={"detail": str(exc)})

        started_at = now_iso()
        started = time.monotonic()
        if stdin_path:
            try:
                stdin_fh: Any = open(stdin_path, "rb")
            except OSError:
                return _fail(request, "remote_stdin_unreadable", started_at, started)
        else:
            stdin_fh = subprocess.DEVNULL
        try:
            proc = subprocess.Popen(  # noqa: S603 — fixed, validated argv
                argv, stdin=stdin_fh, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=self._env, start_new_session=True)
        except FileNotFoundError:
            return _fail(request, "runner_executable_not_found", started_at, started)
        except OSError:
            return _fail(request, "runner_spawn_failed", started_at, started)
        finally:
            if stdin_path:
                stdin_fh.close()  # type: ignore[union-attr]

        timed_out = False
        try:
            out, err = proc.communicate(timeout=request.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            out, err = proc.communicate()

        cap = request.max_output_bytes
        out_c, err_c = out[:cap], err[:cap]
        status = "timed_out" if timed_out else ("completed" if proc.returncode == 0 else "failed")
        reason = "runner_timeout" if timed_out else (
            "runner_nonzero_exit" if proc.returncode != 0 else None)
        out_p = out_c.decode("utf-8", errors="replace")
        err_p = err_c.decode("utf-8", errors="replace")
        return ToolRunnerResult(
            request_id=request.request_id, run_id=request.run_id,
            tool_call_id=request.tool_call_id, status=status,
            exit_code=proc.returncode, duration_ms=int((time.monotonic() - started) * 1000),
            stdout_preview=out_p, stderr_preview=err_p,
            stdout_sha256=hashlib.sha256(out_c).hexdigest(),
            stderr_sha256=hashlib.sha256(err_c).hexdigest(),
            stdout_bytes=len(out_c), stderr_bytes=len(err_c),
            reason_code=reason, log_tail=(err_p or out_p)[-800:] if reason else None,
            started_at=started_at, finished_at=now_iso(),
            metadata={"stdout_truncated": len(out) > cap, "stderr_truncated": len(err) > cap,
                      "ssh_host": str(host)})


def _fail(request: ToolRunnerRequest, reason: str, started_at: str, started: float) -> ToolRunnerResult:
    empty = hashlib.sha256(b"").hexdigest()
    return ToolRunnerResult(
        request_id=request.request_id, run_id=request.run_id,
        tool_call_id=request.tool_call_id, status="failed", exit_code=None,
        duration_ms=int((time.monotonic() - started) * 1000), stdout_preview="",
        stderr_preview="", stdout_sha256=empty, stderr_sha256=empty, stdout_bytes=0,
        stderr_bytes=0, reason_code=reason, started_at=started_at, finished_at=now_iso())


__all__ = ["RemoteToolRunner", "build_remote_ssh_argv"]
