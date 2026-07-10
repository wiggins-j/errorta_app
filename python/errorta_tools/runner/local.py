"""Local ToolRunner adapter.

This module is the only F043 local process-launch point. Council code reaches
it through tool abstractions and policy, never by importing subprocess itself.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import time
from typing import Any

from errorta_policy import PolicyAction, PolicyEngine, PolicyPhase

from .artifacts import RunnerArtifactStore
from .env import build_runner_env, sanitize_text
from .paths import WorkspacePathError, resolve_workspace_path
from .policy import evaluate_runner_launch
from .sandbox import SandboxUnavailable, wrap_argv
from .types import RunnerArtifactRef, ToolRunnerRequest, ToolRunnerResult, now_iso


class LocalToolRunner:
    def __init__(
        self,
        *,
        artifact_store: RunnerArtifactStore,
        source_env: dict[str, object] | None = None,
        policy: dict[str, Any] | None = None,
        policy_engine: PolicyEngine | None = None,
    ) -> None:
        self._artifact_store = artifact_store
        self._source_env = source_env
        self._policy = dict(policy or {"action": "deny", "reason_code": "runner_policy_missing"})
        self._policy_engine = policy_engine or PolicyEngine()

    async def run(
        self,
        request: ToolRunnerRequest,
        *,
        phase: PolicyPhase = PolicyPhase.CODE_EXEC,
    ) -> ToolRunnerResult:
        started_at = now_iso()
        started = time.monotonic()
        if request.execution_location != "local":
            return ToolRunnerResult.blocked(
                request=request,
                reason_code="runner_location_not_local",
                metadata={"execution_location": request.execution_location},
            )

        decision = evaluate_runner_launch(
            request,
            phase=phase,
            policy=self._policy,
            engine=self._policy_engine,
        )
        if decision.action != PolicyAction.ALLOW:
            return ToolRunnerResult.blocked(
                request=request,
                reason_code=decision.reason_code or "runner_policy_blocked",
                metadata={"policy_decision": decision.to_dict()},
            )

        try:
            cwd = resolve_workspace_path(
                request.workspace_root,
                request.relative_cwd,
                must_exist=True,
            )
        except WorkspacePathError as exc:
            return _failure_result(
                request=request,
                status="blocked",
                reason_code=str(exc) or "workspace_path_invalid",
                started_at=started_at,
                started=started,
            )

        runner_env = build_runner_env(
            source_env=self._source_env if self._source_env is not None else os.environ,
            allowlist=request.env_allowlist,
            explicit_env=request.explicit_env,
        )

        # Hardened tier: wrap the child argv in the requested OS sandbox. Fail
        # closed — a requested-but-unavailable sandbox blocks the launch rather
        # than silently downgrading to the unsandboxed subprocess.
        try:
            launch_argv = wrap_argv(
                backend=request.sandbox,
                argv=request.argv,
                workspace_root=str(cwd),
                writable_paths=request.sandbox_writable_paths,
                network_allowed=request.network_allowed,
                docker_image=request.sandbox_image,
            )
        except SandboxUnavailable as exc:
            return _failure_result(
                request=request,
                status="blocked",
                reason_code=exc.reason_code,
                started_at=started_at,
                started=started,
            )

        try:
            proc = await asyncio.create_subprocess_exec(
                *launch_argv,
                cwd=str(cwd),
                env=runner_env.values,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            return _failure_result(
                request=request,
                status="failed",
                reason_code="runner_executable_not_found",
                started_at=started_at,
                started=started,
            )
        except OSError:
            return _failure_result(
                request=request,
                status="failed",
                reason_code="runner_spawn_failed",
                started_at=started_at,
                started=started,
            )

        timed_out = False
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=request.timeout_seconds,
            )
        except asyncio.TimeoutError:
            timed_out = True
            proc.kill()
            stdout, stderr = await proc.communicate()

        capped_stdout = stdout[: request.max_output_bytes]
        capped_stderr = stderr[: request.max_output_bytes]
        stdout_preview = _decode_preview(capped_stdout, redactions=runner_env.redaction_values)
        stderr_preview = _decode_preview(capped_stderr, redactions=runner_env.redaction_values)
        refs = _write_output_artifacts(
            store=self._artifact_store,
            request=request,
            stdout=capped_stdout,
            stderr=capped_stderr,
        )

        status = "timed_out" if timed_out else ("completed" if proc.returncode == 0 else "failed")
        reason_code = None
        if timed_out:
            reason_code = "runner_timeout"
        elif proc.returncode != 0:
            reason_code = "runner_nonzero_exit"

        metadata = {
            "stdout_truncated": len(stdout) > len(capped_stdout),
            "stderr_truncated": len(stderr) > len(capped_stderr),
            "env": runner_env.safe_projection(),
        }
        return ToolRunnerResult(
            request_id=request.request_id,
            run_id=request.run_id,
            tool_call_id=request.tool_call_id,
            status=status,
            exit_code=proc.returncode,
            duration_ms=_duration_ms(started),
            stdout_preview=stdout_preview,
            stderr_preview=stderr_preview,
            stdout_sha256=hashlib.sha256(capped_stdout).hexdigest(),
            stderr_sha256=hashlib.sha256(capped_stderr).hexdigest(),
            stdout_bytes=len(capped_stdout),
            stderr_bytes=len(capped_stderr),
            artifact_refs=refs,
            reason_code=reason_code,
            log_tail=_tail(stderr_preview or stdout_preview) if reason_code else None,
            started_at=started_at,
            finished_at=now_iso(),
            metadata=metadata,
        )


def _decode_preview(data: bytes, *, redactions: dict[str, str]) -> str:
    return sanitize_text(data.decode("utf-8", errors="replace"), redaction_values=redactions)


def _tail(text: str, *, chars: int = 800) -> str:
    return text[-chars:]


def _duration_ms(started: float) -> int:
    return int(max(0.0, time.monotonic() - started) * 1000)


def _write_output_artifacts(
    *,
    store: RunnerArtifactStore,
    request: ToolRunnerRequest,
    stdout: bytes,
    stderr: bytes,
) -> tuple[RunnerArtifactRef, ...]:
    refs: list[RunnerArtifactRef] = []
    if stdout:
        refs.append(
            store.write_bytes(
                run_id=request.run_id,
                request_id=request.request_id,
                name="stdout.txt",
                data=stdout,
                kind="stdout",
            )
        )
    if stderr:
        refs.append(
            store.write_bytes(
                run_id=request.run_id,
                request_id=request.request_id,
                name="stderr.txt",
                data=stderr,
                kind="stderr",
            )
        )
    return tuple(refs)


def _failure_result(
    *,
    request: ToolRunnerRequest,
    status: str,
    reason_code: str,
    started_at: str,
    started: float,
) -> ToolRunnerResult:
    empty_hash = hashlib.sha256(b"").hexdigest()
    return ToolRunnerResult(
        request_id=request.request_id,
        run_id=request.run_id,
        tool_call_id=request.tool_call_id,
        status=status,  # type: ignore[arg-type]
        exit_code=None,
        duration_ms=_duration_ms(started),
        stdout_preview="",
        stderr_preview="",
        stdout_sha256=empty_hash,
        stderr_sha256=empty_hash,
        stdout_bytes=0,
        stderr_bytes=0,
        reason_code=reason_code,
        log_tail=None,
        started_at=started_at,
        finished_at=now_iso(),
    )


__all__ = ["LocalToolRunner"]
