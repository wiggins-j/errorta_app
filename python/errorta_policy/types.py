"""Typed policy evaluation vocabulary for F041.

Policy decisions deliberately carry only safe projections of a request.
Raw prompts, tool arguments, provider payloads, or tool output bytes do not
belong in this package.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class PolicyAction(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


class PolicyPhase(str, Enum):
    MODEL_REQUEST = "model_request"
    CONTEXT_SOURCE = "context_source"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    REMOTE_EGRESS = "remote_egress"
    CODE_WRITE = "code_write"
    CODE_EXEC = "code_exec"
    CALLOUT_REQUEST = "callout_request"
    STEWARD_REQUEST = "steward_request"
    MCP_ELICITATION = "mcp_elicitation"
    CHILD_RUN = "child_run"
    CHILD_RUN_OUTPUT = "child_run_output"


@dataclass(frozen=True)
class PolicyStateWrite:
    """A state change to apply only after a pending ASK is approved."""

    key: str
    value: Any

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "value": self.value}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "PolicyStateWrite":
        return cls(key=str(raw["key"]), value=raw.get("value"))


@dataclass(frozen=True)
class PendingDecisionRequest:
    """A durable ASK request, before it has been assigned/resolved."""

    run_id: str
    phase: PolicyPhase
    reason_code: str
    requester: dict[str, Any]
    safe_request: dict[str, Any]
    state_writes_on_approve: tuple[PolicyStateWrite, ...] = ()
    decision_id: str | None = None
    risk_class: str | None = None
    created_by_policy_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "run_id": self.run_id,
            "phase": self.phase.value,
            "reason_code": self.reason_code,
            "requester": dict(self.requester),
            "safe_request": dict(self.safe_request),
            "state_writes_on_approve": [
                w.to_dict() for w in self.state_writes_on_approve
            ],
            "metadata": dict(self.metadata),
        }
        if self.decision_id is not None:
            d["decision_id"] = self.decision_id
        if self.risk_class is not None:
            d["risk_class"] = self.risk_class
        if self.created_by_policy_id is not None:
            d["created_by_policy_id"] = self.created_by_policy_id
        return d

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "PendingDecisionRequest":
        return cls(
            run_id=str(raw["run_id"]),
            phase=PolicyPhase(str(raw["phase"])),
            reason_code=str(raw["reason_code"]),
            requester=dict(raw.get("requester") or {}),
            safe_request=dict(raw.get("safe_request") or {}),
            state_writes_on_approve=tuple(
                PolicyStateWrite.from_dict(w)
                for w in raw.get("state_writes_on_approve") or []
            ),
            decision_id=raw.get("decision_id"),
            risk_class=raw.get("risk_class"),
            created_by_policy_id=raw.get("created_by_policy_id"),
            metadata=dict(raw.get("metadata") or {}),
        )


@dataclass(frozen=True)
class PolicyDecision:
    action: PolicyAction
    reason_code: str | None = None
    message_code: str | None = None
    pending_request: PendingDecisionRequest | None = None
    audit: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def allow(
        cls,
        *,
        reason_code: str | None = None,
        audit: dict[str, Any] | None = None,
    ) -> "PolicyDecision":
        return cls(
            action=PolicyAction.ALLOW,
            reason_code=reason_code,
            audit=dict(audit or {}),
        )

    @classmethod
    def deny(
        cls,
        *,
        reason_code: str,
        message_code: str | None = None,
        audit: dict[str, Any] | None = None,
    ) -> "PolicyDecision":
        return cls(
            action=PolicyAction.DENY,
            reason_code=reason_code,
            message_code=message_code,
            audit=dict(audit or {}),
        )

    @classmethod
    def ask(
        cls,
        *,
        reason_code: str,
        pending_request: PendingDecisionRequest,
        message_code: str | None = None,
        audit: dict[str, Any] | None = None,
    ) -> "PolicyDecision":
        return cls(
            action=PolicyAction.ASK,
            reason_code=reason_code,
            message_code=message_code,
            pending_request=pending_request,
            audit=dict(audit or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "action": self.action.value,
            "audit": dict(self.audit),
        }
        if self.reason_code is not None:
            d["reason_code"] = self.reason_code
        if self.message_code is not None:
            d["message_code"] = self.message_code
        if self.pending_request is not None:
            d["pending_request"] = self.pending_request.to_dict()
        return d


@dataclass(frozen=True)
class PolicyContext:
    phase: PolicyPhase
    run_id: str
    room_id: str | None = None
    member_id: str | None = None
    tool_id: str | None = None
    route_id: str | None = None
    egress_class: str | None = None
    request_sha256: str | None = None
    source_class: str | None = None
    risk_class: str | None = None
    estimated_cost_usd: float | None = None
    requester: dict[str, Any] = field(default_factory=dict)
    safe_request: dict[str, Any] = field(default_factory=dict)
    policy: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
