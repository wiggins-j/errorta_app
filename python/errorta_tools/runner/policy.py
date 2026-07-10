"""F041 policy helpers for ToolRunner launches."""
from __future__ import annotations

from typing import Any

from errorta_policy import PolicyContext, PolicyDecision, PolicyEngine, PolicyPhase

from .types import ToolRunnerRequest


def build_runner_policy_context(
    request: ToolRunnerRequest,
    *,
    phase: PolicyPhase = PolicyPhase.CODE_EXEC,
    policy: dict[str, Any] | None = None,
) -> PolicyContext:
    requires_approval = bool(
        request.network_allowed
        or request.explicit_env
        or (policy or {}).get("requires_approval")
    )
    return PolicyContext(
        phase=phase,
        run_id=request.run_id,
        tool_id="code_exec" if phase == PolicyPhase.CODE_EXEC else "code_write",
        egress_class=request.execution_location,
        request_sha256=request.argv_sha256,
        risk_class="code_execution",
        requester={"type": "tool_runner", "tool_call_id": request.tool_call_id},
        safe_request=request.safe_projection(),
        policy=dict(policy or {}),
        metadata={
            "requires_approval": requires_approval,
            "reason_code": "runner_approval_required",
        },
    )


def evaluate_runner_launch(
    request: ToolRunnerRequest,
    *,
    phase: PolicyPhase = PolicyPhase.CODE_EXEC,
    policy: dict[str, Any] | None = None,
    engine: PolicyEngine | None = None,
) -> PolicyDecision:
    evaluator = engine or PolicyEngine()
    context = build_runner_policy_context(request, phase=phase, policy=policy)
    return evaluator.evaluate(context)


__all__ = ["build_runner_policy_context", "evaluate_runner_launch"]
