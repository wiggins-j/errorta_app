"""F043 local/remote ToolRunner adapter tests."""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

from errorta_tools.gateway import ToolCallRequest
from errorta_tools.runner import (
    EnvGrant,
    LocalToolRunner,
    RemoteToolRunner,
    RunnerArtifactStore,
    ToolRunnerRequest,
    ToolRunnerResult,
    evaluate_runner_launch,
    runner_result_to_tool_call_result,
)


def _request(tmp_path, *, argv, **overrides) -> ToolRunnerRequest:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    params = {
        "request_id": "runner-req-1",
        "run_id": "run-1",
        "tool_call_id": "tc-1",
        "argv": tuple(argv),
        "workspace_root": str(workspace),
    }
    params.update(overrides)
    return ToolRunnerRequest(**params)


def _runner(tmp_path, *, policy=None, source_env=None) -> LocalToolRunner:
    return LocalToolRunner(
        artifact_store=RunnerArtifactStore(root=tmp_path / "artifacts"),
        source_env=source_env or {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
        policy=policy or {"action": "allow", "reason_code": "test_allow"},
    )


@pytest.mark.asyncio
async def test_local_runner_executes_script_with_hashes_artifacts_and_preview(tmp_path) -> None:
    request = _request(
        tmp_path,
        argv=(sys.executable, "-c", "import sys; print('hello runner'); sys.stderr.write('warn')"),
    )

    result = await _runner(tmp_path).run(request)

    assert result.status == "completed"
    assert result.exit_code == 0
    assert result.stdout_preview == "hello runner\n"
    assert result.stderr_preview == "warn"
    assert result.stdout_sha256
    assert result.stderr_sha256
    assert {ref.kind for ref in result.artifact_refs} == {"stdout", "stderr"}
    assert "content" not in result.audit_projection()


@pytest.mark.asyncio
async def test_local_runner_output_cap_applies_before_result_projection(tmp_path) -> None:
    request = _request(
        tmp_path,
        argv=(sys.executable, "-c", "import sys; sys.stdout.write('x' * 200)"),
        max_output_bytes=25,
    )

    result = await _runner(tmp_path).run(request)

    assert result.status == "completed"
    assert result.stdout_bytes == 25
    assert result.stdout_preview == "x" * 25
    assert result.metadata["stdout_truncated"] is True


@pytest.mark.asyncio
async def test_local_runner_redacts_explicit_env_values_from_previews(tmp_path) -> None:
    request = _request(
        tmp_path,
        argv=(
            sys.executable,
            "-c",
            "import os, sys; sys.stdout.write(os.environ['SECRET_TOKEN'])",
        ),
        explicit_env=(EnvGrant(name="SECRET_TOKEN", value="runner-secret-value"),),
    )

    result = await _runner(tmp_path).run(request)

    assert result.status == "completed"
    assert "runner-secret-value" not in result.stdout_preview
    assert result.stdout_preview == "[redacted-env:SECRET_TOKEN]"
    assert "runner-secret-value" not in json.dumps(result.audit_projection())


@pytest.mark.asyncio
async def test_local_runner_blocks_without_policy_allow(tmp_path) -> None:
    request = _request(tmp_path, argv=(sys.executable, "-c", "print('should not run')"))

    result = await LocalToolRunner(
        artifact_store=RunnerArtifactStore(root=tmp_path / "artifacts")
    ).run(request)

    assert result.status == "blocked"
    assert result.reason_code == "runner_policy_missing"
    assert result.stdout_preview == ""


@pytest.mark.asyncio
async def test_local_runner_timeout_returns_sanitized_failure(tmp_path) -> None:
    request = _request(
        tmp_path,
        argv=(sys.executable, "-c", "import time; time.sleep(2)"),
        timeout_seconds=0.05,
    )

    result = await _runner(tmp_path).run(request)

    assert result.status == "timed_out"
    assert result.reason_code == "runner_timeout"
    assert result.log_tail is not None


@pytest.mark.asyncio
async def test_local_runner_nonzero_exit_reports_reason_and_log_tail(tmp_path) -> None:
    request = _request(
        tmp_path,
        argv=(sys.executable, "-c", "import sys; sys.stderr.write('bad input'); sys.exit(7)"),
    )

    result = await _runner(tmp_path).run(request)

    assert result.status == "failed"
    assert result.exit_code == 7
    assert result.reason_code == "runner_nonzero_exit"
    assert result.log_tail == "bad input"


@pytest.mark.asyncio
async def test_remote_runner_request_is_represented_but_fails_closed(tmp_path) -> None:
    request = _request(
        tmp_path,
        argv=("python", "-V"),
        execution_location="remote_ssh",
    )

    result = await RemoteToolRunner().run(request)

    assert result.status == "blocked"
    assert result.reason_code == "remote_runner_not_implemented"


def test_runner_policy_ask_projection_contains_env_names_not_values(tmp_path) -> None:
    request = _request(
        tmp_path,
        argv=("python", "-V"),
        explicit_env=(EnvGrant(name="OPENAI_API_KEY", value="secret-value"),),
    )

    decision = evaluate_runner_launch(request, policy={"action": "ask"})

    assert decision.pending_request is not None
    pending = decision.pending_request
    assert pending.safe_request["explicit_env_names"] == ["OPENAI_API_KEY"]
    assert "secret-value" not in json.dumps(pending.to_dict(), sort_keys=True)


def test_runner_result_can_bridge_to_tool_gateway_result(tmp_path) -> None:
    runner_request = _request(
        tmp_path,
        argv=(sys.executable, "-c", "print('hello')"),
    )
    tool_request = ToolCallRequest(
        call_id="tc-1",
        run_id="run-1",
        turn_id="turn-1",
        member_id="member-1",
        tool_id="code_exec",
        arguments={"cmd": "redacted elsewhere"},
    )
    runner_result = ToolRunnerResult(
        request_id=runner_request.request_id,
        run_id=runner_request.run_id,
        tool_call_id=runner_request.tool_call_id,
        status="completed",
        exit_code=0,
        duration_ms=1,
        stdout_preview="hello\n",
        stderr_preview="",
        stdout_sha256="stdout-hash",
        stderr_sha256="stderr-hash",
        stdout_bytes=6,
        stderr_bytes=0,
    )
    result = runner_result_to_tool_call_result(
        request=tool_request,
        result=runner_result,
    )

    assert result.tool_id == "code_exec"
    assert result.status == "completed"
    assert "hello" in result.content
    assert result.provenance["runner_stdout_sha256"] == "stdout-hash"


def test_errorta_council_runner_imports_no_process_egress_modules() -> None:
    council_dir = Path(__file__).parents[2] / "errorta_council"
    forbidden = {"subprocess", "requests", "urllib", "aiohttp", "httpx"}
    allowed = {council_dir / "gateway_local.py"}
    violations: list[str] = []
    for path in sorted(council_dir.rglob("*.py")):
        if path in allowed:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".", 1)[0]
                    if root in forbidden:
                        violations.append(f"{path.relative_to(council_dir)} imports {alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                root = node.module.split(".", 1)[0]
                if root in forbidden:
                    violations.append(f"{path.relative_to(council_dir)} imports from {node.module}")
    assert violations == []
