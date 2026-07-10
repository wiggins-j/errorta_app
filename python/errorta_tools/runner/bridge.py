"""Bridge ToolRunner results into ToolGateway results."""
from __future__ import annotations

import json

from errorta_tools.gateway import ToolCallRequest, ToolCallResult

from .types import ToolRunnerResult


def runner_result_to_tool_call_result(
    *,
    request: ToolCallRequest,
    result: ToolRunnerResult,
) -> ToolCallResult:
    content = json.dumps(result.audit_projection(), sort_keys=True)
    return ToolCallResult.from_content(
        request=request,
        content=content,
        duration_ms=result.duration_ms,
        egress_class="local" if result.status != "blocked" else "none",
        status=result.status,
        provenance={
            "runner_request_id": result.request_id,
            "runner_status": result.status,
            "runner_stdout_sha256": result.stdout_sha256,
            "runner_stderr_sha256": result.stderr_sha256,
        },
        metadata={
            "runner_artifact_refs": [ref.to_dict() for ref in result.artifact_refs],
            "runner_reason_code": result.reason_code,
        },
    )


__all__ = ["runner_result_to_tool_call_result"]
