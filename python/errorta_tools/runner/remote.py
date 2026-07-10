"""Remote ToolRunner request adapter.

SSH execution will land in a later slice. Until then, remote runner requests
are represented and fail closed through the same result contract.
"""
from __future__ import annotations

from .types import ToolRunnerRequest, ToolRunnerResult


class RemoteToolRunner:
    async def run(self, request: ToolRunnerRequest) -> ToolRunnerResult:
        if request.execution_location != "remote_ssh":
            return ToolRunnerResult.blocked(
                request=request,
                reason_code="runner_location_not_remote",
                metadata={"execution_location": request.execution_location},
            )
        return ToolRunnerResult.blocked(
            request=request,
            reason_code="remote_runner_not_implemented",
            metadata={"execution_location": request.execution_location},
        )


__all__ = ["RemoteToolRunner"]
