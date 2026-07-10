"""F030 model gateway routes.

These endpoints expose the backend-only skeleton: policy read/write, budget
summary, audit readout, and route planning. Provider SDKs are not initialized
in this slice.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from errorta_model_gateway import audit, budget
from errorta_model_gateway.policy import GatewayPolicy, provider_is_remote
from errorta_model_gateway.router import GatewayRequest, plan_request
from errorta_model_gateway.runtime import gateway_owner
from errorta_model_gateway.settings import load_policy, save_policy

router = APIRouter(prefix="/model-gateway", tags=["model-gateway"])


class PlanRequest(BaseModel):
    role: str
    prompt: str | None = None
    corpus: str | None = None
    provider: str | None = None
    model: str | None = None
    payload_fields: list[str] = Field(default_factory=list)
    input_tokens: int = 0
    estimated_cost_usd: float = 0.0
    session_id: str | None = None
    record: bool = False


class OllamaChatRequest(BaseModel):
    model: str
    messages: list[dict[str, Any]] = Field(default_factory=list)
    stream: bool = False


LOGICAL_MODEL_ROLES = {
    "answerer": "answerer",
    "answer": "answerer",
    "judge": "judge",
    "verifier": "verifier",
    "hyde": "hyde",
    "fallback": "fallback",
    "benchmark": "benchmark",
    "brief_planner": "brief_planner",
    "brief-planner": "brief_planner",
}


def _safe_policy_from_body(body: dict[str, Any]) -> GatewayPolicy:
    policy = GatewayPolicy.from_dict(body)
    # Fail closed on ambiguous policy writes: unsupported strings are normalized
    # by GatewayPolicy.from_dict, so surface the normalized shape to callers.
    return policy


def _logical_role(model: str) -> str:
    for token, role in LOGICAL_MODEL_ROLES.items():
        if f".{token}." in model or model.endswith(f".{token}.remote"):
            return role
    return "answerer"


def _is_logical_model_alias(model: str) -> bool:
    return model.startswith("errorta.") and model.rsplit(".", 1)[-1] in {
        "local",
        "remote",
    }


def _remote_provider_for_role(role: str) -> str:
    configured = load_policy().route_for(role).provider
    if provider_is_remote(configured):
        return configured
    return "anthropic"


@router.get("/status")
def status() -> dict[str, Any]:
    policy = load_policy()
    return {
        "service": "errorta-model-gateway",
        "enabled": policy.global_mode == "you_pick",
        "policy": policy.to_dict(),
        "runtime": gateway_owner().to_dict(),
        "budget": budget.summary(policy.budget),
    }


@router.get("/policy")
def get_policy() -> dict[str, Any]:
    return load_policy().to_dict()


@router.put("/policy")
def put_policy(body: dict[str, Any]) -> dict[str, Any]:
    policy = _safe_policy_from_body(body)
    return save_policy(policy).to_dict()


@router.get("/settings")
def get_settings() -> dict[str, Any]:
    return load_policy().to_dict()


@router.put("/settings")
def put_settings(body: dict[str, Any]) -> dict[str, Any]:
    return save_policy(_safe_policy_from_body(body)).to_dict()


@router.post("/plan")
def plan(body: PlanRequest) -> dict[str, Any]:
    """Return an advisory policy preview, not dispatch authority.

    Caller-declared ``payload_fields`` are useful for Settings previews, but
    dispatch paths must derive payload fields from the request they build.
    """
    request = GatewayRequest(
        role=body.role,
        prompt=body.prompt,
        corpus=body.corpus,
        provider=body.provider,
        model=body.model,
        payload_fields=body.payload_fields,
        input_tokens=body.input_tokens,
        estimated_cost_usd=body.estimated_cost_usd,
        session_id=body.session_id,
    )
    plan_result = plan_request(request, record=body.record).to_dict()
    return {
        **plan_result,
        "advisory": True,
        "dispatch_authority": False,
        "payload_fields_source": "caller_declared",
    }


@router.get("/audit")
def get_audit(limit: int = 50) -> dict[str, Any]:
    return {"events": audit.list_events(limit=limit)}


@router.get("/budget")
def get_budget() -> dict[str, Any]:
    policy = load_policy()
    return budget.summary(policy.budget)


@router.post("/ollama/api/chat")
async def ollama_chat(body: OllamaChatRequest) -> dict[str, Any]:
    """F030-01 — Ollama-compatible chat: PLAN (fail-closed policy/budget) then
    DISPATCH the resolved route to its provider handler.

    Local and remote both dispatch through the F034 async handler registry (the
    same machinery the Council uses). Nothing reaches a provider without
    ``plan_request`` allowing it first; remote usage is settled to the budget
    ledger with the provider's REAL token counts (not the pre-call estimate).
    """
    role = _logical_role(body.model)
    is_alias = _is_logical_model_alias(body.model)
    provider = (
        _remote_provider_for_role(role)
        if is_alias and body.model.endswith(".remote")
        else None
    )
    prompt = "\n".join(
        str(m.get("content", "")) for m in body.messages if isinstance(m, dict)
    )
    payload_fields = ["prompt"] if prompt else []
    plan = plan_request(
        GatewayRequest(
            role=role,
            prompt=prompt,
            provider=provider,
            model=None if is_alias else body.model,
            payload_fields=payload_fields,
            input_tokens=max(0, len(prompt) // 4),
            estimated_cost_usd=0.0,
        ),
        record=True,
    )
    if not plan.allowed:
        raise HTTPException(status_code=403, detail=plan.to_dict())

    try:
        result = await _dispatch_resolved(plan, body)
    except Exception as exc:  # provider failure -> clean HTTP, never a raw 500
        retryable = _is_retryable(exc)
        _record_outcome(plan, prompt=prompt, status="provider_error", error=str(exc))
        raise HTTPException(
            status_code=503 if retryable else 502,
            detail={
                "code": "provider_error",
                "retryable": retryable,
                "message": str(exc),
                "provider": plan.provider,
                "audit_id": plan.audit_id,
            },
        )

    in_tokens = int(result.input_tokens or 0)
    out_tokens = int(result.output_tokens or 0)
    # Settle the budget ledger with the provider's REAL counts (remote only —
    # local Ollama is unmetered). Linked to the pre-call "planned" audit so the
    # ledger row and the audit event share an id.
    if plan.remote and plan.audit_id:
        budget.record_usage(
            audit_id=plan.audit_id,
            provider=plan.provider,
            role=plan.role,
            remote=True,
            input_tokens=in_tokens,
            output_tokens=out_tokens,
        )
        _record_outcome(
            plan, prompt=prompt, status="ok",
            input_tokens=in_tokens, output_tokens=out_tokens,
        )

    return {
        "model": body.model,
        "created_at": _now_iso(),
        "message": {"role": "assistant", "content": result.content},
        "done": True,
        "done_reason": "stop",
        "prompt_eval_count": in_tokens,
        "eval_count": out_tokens,
        # Gateway provenance: the resolved concrete route + its audit id.
        "gateway": {
            "provider": plan.provider,
            "model": plan.model,
            "remote": plan.remote,
            "audit_id": plan.audit_id,
        },
    }


async def _dispatch_resolved(plan: Any, body: OllamaChatRequest) -> Any:
    """Dispatch an ALLOWED plan to its provider handler via the F034 async
    registry (local + remote uniform). Raises
    ``errorta_council.gateway_local.{FatalError,RetryableError}`` on provider
    failure. Never called unless ``plan.allowed`` — the single choke point holds.
    """
    from errorta_council.gateway_local import FatalError
    from errorta_model_gateway.providers import async_registry
    from errorta_model_gateway.providers.async_base import AsyncProviderRequest

    async_registry.ensure_bootstrapped()
    handler = async_registry.get_handler(plan.provider)
    if handler is None:
        raise FatalError(f"provider_not_registered: {plan.provider!r}")

    api_key: str | None = None
    if plan.provider not in ("custom", "local", "fake"):
        from errorta_app import provider_keys as _provider_keys

        api_key = _provider_keys.get_fixed_key(plan.provider)

    model = _provider_model_for_plan(plan, body)
    if not model:
        raise FatalError(
            f"model_route_not_configured: provider={plan.provider!r} role={plan.role!r}"
        )

    areq = AsyncProviderRequest(
        model=model,
        messages=[
            {"role": str(m.get("role", "user")), "content": str(m.get("content", ""))}
            for m in body.messages
            if isinstance(m, dict)
        ],
        timeout_seconds=60,
    )
    return await handler.call(areq, api_key=api_key)


def _provider_model_for_plan(plan: Any, body: OllamaChatRequest) -> str:
    raw = str(plan.model or "").strip()
    if not raw and not _is_logical_model_alias(body.model):
        raw = body.model.strip()
    if not raw:
        return ""

    if plan.provider == "local" and raw.startswith("local.ollama."):
        return raw[len("local.ollama.") :]

    prefix = f"{plan.provider}."
    if raw.startswith(prefix):
        return raw[len(prefix) :]
    return raw


def _is_retryable(exc: Exception) -> bool:
    try:
        from errorta_council.gateway_local import RetryableError

        return isinstance(exc, RetryableError)
    except Exception:  # pragma: no cover - defensive
        return False


def _record_outcome(
    plan: Any,
    *,
    prompt: str,
    status: str,
    error: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> None:
    """Append a post-call audit outcome (ok / provider_error). Best-effort —
    an audit-write failure must never break a served response."""
    try:
        audit.append(
            audit.build_event(
                status=status,  # type: ignore[arg-type]
                role=plan.role,
                provider=plan.provider,
                model=plan.model,
                corpus=plan.corpus,
                egress_policy=plan.egress_policy,
                egress_class=plan.egress_class,
                payload_fields=list(plan.payload_fields),
                payload_hash=audit.payload_sha256(prompt),
                preview_redacted=audit.preview_text(prompt or ""),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                blocked_reason=error,
            )
        )
    except Exception:  # pragma: no cover - defensive
        pass


def _now_iso() -> str:
    import datetime as _dt

    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
