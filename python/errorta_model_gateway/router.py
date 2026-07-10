"""Gateway request planning.

This slice does not call Anthropic/OpenAI. It determines the effective route,
enforces fail-closed policy/budget checks, and emits audit/ledger entries when
asked to record the decision.
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from typing import Any

from . import audit, budget
from .policy import (
    GatewayPolicy,
    egress_class,
    fields_allowed,
    provider_is_known,
    provider_is_remote,
    role_allows_policy,
)
from .providers.ollama import is_loopback_ollama_host
from .runtime import gateway_owner
from .settings import load_policy


@dataclass(frozen=True)
class GatewayRequest:
    role: str
    prompt: str | None = None
    corpus: str | None = None
    provider: str | None = None
    model: str | None = None
    payload_fields: list[str] = field(default_factory=list)
    input_tokens: int = 0
    estimated_cost_usd: float = 0.0
    session_id: str | None = None


@dataclass(frozen=True)
class GatewayPlan:
    allowed: bool
    provider: str
    model: str | None
    role: str
    corpus: str | None
    remote: bool
    egress_policy: str
    egress_class: str
    payload_fields: list[str]
    blocked_reason: str | None = None
    audit_id: str | None = None
    budget: dict[str, Any] | None = None
    runtime: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def local_only_forced(policy: GatewayPolicy) -> bool:
    """Return True when any master local-only switch is active."""
    env_forced = os.environ.get("AIAR_LOCAL_ONLY") == "1"
    errorta_env_forced = os.environ.get("ERRORTA_MODEL_GATEWAY_LOCAL_ONLY") == "1"
    return env_forced or errorta_env_forced or policy.global_mode == "local_only"


def plan_request(
    request: GatewayRequest,
    *,
    policy: GatewayPolicy | None = None,
    record: bool = False,
    settle_usage: bool = False,
) -> GatewayPlan:
    active_policy = policy or load_policy()
    role = (request.role or "answerer").strip().lower()
    route = active_policy.route_for(role)
    provider = (request.provider or route.provider).strip().lower()
    model = request.model or route.model
    provider_known = provider_is_known(provider)
    remote = not provider_known or provider_is_remote(provider)
    payload_fields = sorted(
        {str(field).strip() for field in request.payload_fields if str(field).strip()}
    )
    payload_field_set = set(payload_fields)
    corpus = (request.corpus or "").strip() or None
    egress_policy = active_policy.egress_policy_for(corpus)
    runtime = gateway_owner().to_dict()
    blocked_reason: str | None = None

    if not provider_known:
        blocked_reason = f"unknown provider {provider!r}"
    elif not route.enabled or provider == "off":
        blocked_reason = "role route disabled"
    elif (
        provider == "local"
        and local_only_forced(active_policy)
        and not is_loopback_ollama_host(os.environ.get("OLLAMA_HOST"))
    ):
        blocked_reason = "local-only mode requires OLLAMA_HOST to be loopback"
    elif runtime.get("local_proxy_may_call_remote") is False and remote:
        blocked_reason = "gateway owner is remote sidecar for active residency mode"
    elif remote and local_only_forced(active_policy):
        blocked_reason = "local-only mode blocks remote providers"
    elif remote and not role_allows_policy(role, egress_policy):
        blocked_reason = f"corpus policy {egress_policy!r} does not allow role {role!r}"
    elif remote and not fields_allowed(egress_policy, payload_field_set):
        blocked_reason = f"payload fields exceed corpus policy {egress_policy!r}"

    budget_status = budget.status(
        active_policy.budget,
        requested_input_tokens=request.input_tokens,
        requested_estimated_cost_usd=request.estimated_cost_usd,
        session_id=request.session_id,
    )
    if blocked_reason is None and remote and not budget_status.allowed:
        blocked_reason = budget_status.reason or "budget exhausted"

    allowed = blocked_reason is None
    plan = GatewayPlan(
        allowed=allowed,
        provider=provider,
        model=model,
        role=role,
        corpus=corpus,
        remote=remote,
        egress_policy=egress_policy,
        egress_class=egress_class(egress_policy, payload_field_set),
        payload_fields=payload_fields,
        blocked_reason=blocked_reason,
        budget=budget_status.to_dict(),
        runtime=runtime,
    )

    if record and (remote or blocked_reason is not None):
        status: audit.AuditStatus
        if blocked_reason is None:
            status = "planned"
        elif blocked_reason.startswith("max_") or "budget" in blocked_reason:
            status = "blocked_by_budget"
        else:
            status = "blocked_by_policy"
        event = audit.append(
            audit.build_event(
                status=status,
                role=role,
                provider=provider,
                model=model,
                corpus=corpus,
                egress_policy=egress_policy,
                egress_class=plan.egress_class,
                payload_fields=payload_fields,
                payload_hash=audit.payload_sha256(request.prompt),
                preview_redacted=audit.preview_text(request.prompt or ""),
                input_tokens=request.input_tokens,
                estimated_cost_usd=request.estimated_cost_usd,
                blocked_reason=blocked_reason,
                session_id=request.session_id,
            )
        )
        if settle_usage and remote and allowed:
            budget.record_usage(
                audit_id=event.id,
                provider=provider,
                role=role,
                remote=True,
                input_tokens=request.input_tokens,
                estimated_cost_usd=request.estimated_cost_usd,
                session_id=request.session_id,
            )
        plan = GatewayPlan(**{**plan.to_dict(), "audit_id": event.id})

    return plan
