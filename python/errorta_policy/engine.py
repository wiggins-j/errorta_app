"""Default F041 policy evaluator.

This engine is intentionally small and deterministic. It does not perform
network calls, shell calls, or provider dispatch; it only converts a safe
policy context into ALLOW, DENY, or ASK.
"""
from __future__ import annotations

from .types import (
    PendingDecisionRequest,
    PolicyContext,
    PolicyDecision,
    PolicyPhase,
    PolicyStateWrite,
)


class PolicyEngine:
    policy_id = "errorta_policy_v1"

    def evaluate(self, context: PolicyContext) -> PolicyDecision:
        if context.phase == PolicyPhase.TOOL_CALL:
            return self._evaluate_tool_call(context)
        configured_action = str(context.policy.get("action") or "").lower()
        if configured_action == "deny":
            return PolicyDecision.deny(
                reason_code=str(context.policy.get("reason_code") or "policy_denied"),
                message_code="policy_denied",
                audit={"policy_id": self.policy_id, "phase": context.phase.value},
            )
        if configured_action == "ask":
            return self._ask_for_generic_approval(context)
        if configured_action == "allow":
            # A configured "allow" IS the authorization decision — callers (e.g.
            # LocalToolRunner running an already-approved tool with granted
            # explicit_env) rely on it to run without re-prompting. The coding
            # test path that must never carry an approval-needing grant under
            # allow is guarded at its own layer (F087-15 testing.py assert),
            # which is the correct place — not a blanket engine reorder (that
            # broke the runner's granted-env path, F087-17).
            return PolicyDecision.allow(
                reason_code=str(context.policy.get("reason_code") or "policy_allow"),
                audit={
                    "policy_id": self.policy_id,
                    "phase": context.phase.value,
                    "configured_action": "allow",
                },
            )
        if bool(context.metadata.get("requires_approval")):
            return self._ask_for_generic_approval(context)
        return PolicyDecision.allow(
            reason_code="policy_default_allow",
            audit={"policy_id": self.policy_id, "phase": context.phase.value},
        )

    def _evaluate_tool_call(self, context: PolicyContext) -> PolicyDecision:
        tool_id = context.tool_id or ""
        enabled = {
            str(t) for t in context.policy.get("enabled_tool_ids") or []
        }
        if not tool_id:
            return PolicyDecision.deny(
                reason_code="tool_missing",
                message_code="policy_tool_missing",
                audit={"policy_id": self.policy_id},
            )
        if tool_id not in enabled:
            return PolicyDecision.deny(
                reason_code="tool_not_granted",
                message_code="policy_tool_not_granted",
                audit={"policy_id": self.policy_id, "tool_id": tool_id},
            )
        if bool(context.policy.get("require_first_use_consent")):
            request = PendingDecisionRequest(
                run_id=context.run_id,
                phase=context.phase,
                reason_code="tool_consent_required",
                requester={
                    **dict(context.requester),
                    "member_id": context.member_id,
                    "tool_id": tool_id,
                },
                safe_request={
                    **dict(context.safe_request),
                    "tool_id": tool_id,
                    "args_sha256": context.request_sha256,
                },
                risk_class=context.risk_class or "remote_eligible_tool",
                created_by_policy_id=self.policy_id,
                state_writes_on_approve=(
                    PolicyStateWrite(key=f"tool_consent:{tool_id}", value=True),
                ),
                metadata={"phase": context.phase.value},
            )
            return PolicyDecision.ask(
                reason_code="tool_consent_required",
                message_code="policy_tool_consent_required",
                pending_request=request,
                audit={"policy_id": self.policy_id, "tool_id": tool_id},
            )
        return PolicyDecision.allow(
            reason_code="tool_policy_allow",
            audit={"policy_id": self.policy_id, "tool_id": tool_id},
        )

    def _ask_for_generic_approval(self, context: PolicyContext) -> PolicyDecision:
        request = PendingDecisionRequest(
            run_id=context.run_id,
            phase=context.phase,
            reason_code=str(context.metadata.get("reason_code") or "approval_required"),
            requester=dict(context.requester),
            safe_request=dict(context.safe_request),
            risk_class=context.risk_class,
            created_by_policy_id=self.policy_id,
            metadata={"phase": context.phase.value},
        )
        return PolicyDecision.ask(
            reason_code=request.reason_code,
            message_code="policy_approval_required",
            pending_request=request,
            audit={"policy_id": self.policy_id, "phase": context.phase.value},
        )
