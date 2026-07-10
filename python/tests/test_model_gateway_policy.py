from __future__ import annotations

import pytest

from errorta_model_gateway import budget
from errorta_model_gateway.policy import GatewayPolicy
from errorta_model_gateway.providers.ollama import (
    OllamaProvider,
    is_loopback_ollama_host,
)
from errorta_model_gateway.router import GatewayRequest, plan_request
from errorta_model_gateway.runtime import GatewayOwner

OPEN_REMOTE_BUDGET = {
    "max_remote_calls_per_day": None,
    "max_remote_calls_per_session": None,
    "max_remote_tokens_per_day": None,
    "max_usd_per_month": None,
}


@pytest.fixture(autouse=True)
def local_gateway_owner(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    monkeypatch.delenv("AIAR_LOCAL_ONLY", raising=False)
    monkeypatch.delenv("ERRORTA_MODEL_GATEWAY_LOCAL_ONLY", raising=False)
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    monkeypatch.setattr(
        "errorta_model_gateway.router.gateway_owner",
        lambda: GatewayOwner(
            residency_mode="local",
            gateway_owner="local-sidecar",
            secret_location="local-secret-store",
            audit_location="local-errorta-home",
            local_proxy_may_call_remote=True,
        ),
    )


def _policy(data: dict) -> GatewayPolicy:
    return GatewayPolicy.from_dict({"budget": OPEN_REMOTE_BUDGET, **data})


def test_local_only_blocks_remote_provider() -> None:
    plan = plan_request(
        GatewayRequest(role="judge", corpus="welcome", provider="anthropic"),
        policy=_policy(
            {
                "global_mode": "local_only",
                "corpus_policies": {"welcome": "redacted_support"},
            }
        ),
    )

    assert plan.allowed is False
    assert plan.provider == "anthropic"
    assert plan.blocked_reason == "local-only mode blocks remote providers"


def test_unknown_provider_fails_closed_even_when_policy_would_allow_remote() -> None:
    plan = plan_request(
        GatewayRequest(
            role="judge",
            corpus="welcome",
            provider="mystery-vendor",
            payload_fields=["prompt", "answer", "redacted_snippets"],
        ),
        policy=_policy(
            {
                "global_mode": "you_pick",
                "corpus_policies": {"welcome": "redacted_support"},
            }
        ),
    )

    assert plan.allowed is False
    assert plan.remote is True
    assert plan.blocked_reason == "unknown provider 'mystery-vendor'"


def test_local_only_blocks_non_loopback_ollama_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OLLAMA_HOST", "http://198.51.100.25:11434")

    plan = plan_request(
        GatewayRequest(role="answerer", provider="local"),
        policy=_policy({"global_mode": "local_only"}),
    )

    assert plan.allowed is False
    assert plan.provider == "local"
    assert plan.remote is False
    assert plan.blocked_reason == "local-only mode requires OLLAMA_HOST to be loopback"


def test_ollama_provider_rejects_non_loopback_host_when_local_only_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIAR_LOCAL_ONLY", "1")

    with pytest.raises(RuntimeError, match="loopback"):
        OllamaProvider(host="http://ollama.internal:11434")


def test_ollama_loopback_host_detection() -> None:
    assert is_loopback_ollama_host(None) is True
    assert is_loopback_ollama_host("http://localhost:11434") is True
    assert is_loopback_ollama_host("http://127.0.0.1:11434") is True
    assert is_loopback_ollama_host("http://[::1]:11434") is True
    assert is_loopback_ollama_host("http://198.51.100.25:11434") is False
    assert is_loopback_ollama_host("http://ollama.internal:11434") is False


def test_redacted_support_allows_remote_judge() -> None:
    plan = plan_request(
        GatewayRequest(
            role="judge",
            corpus="welcome",
            provider="anthropic",
            payload_fields=["answer", "prompt", "redacted_snippets"],
        ),
        policy=_policy(
            {
                "global_mode": "you_pick",
                "corpus_policies": {"welcome": "redacted_support"},
            }
        ),
    )

    assert plan.allowed is True
    assert plan.egress_policy == "redacted_support"


@pytest.mark.parametrize("provider", ["claude_cli", "codex_cli", "cursor_cli"])
def test_subscription_cli_providers_are_known_remote(provider: str) -> None:
    plan = plan_request(
        GatewayRequest(
            role="judge",
            corpus="welcome",
            provider=provider,
            payload_fields=["answer", "prompt", "redacted_snippets"],
        ),
        policy=_policy(
            {
                "global_mode": "you_pick",
                "corpus_policies": {"welcome": "redacted_support"},
            }
        ),
    )

    assert plan.allowed is True
    assert plan.remote is True


def test_hyde_stays_local_until_later_slice_explicitly_enables_it() -> None:
    plan = plan_request(
        GatewayRequest(
            role="hyde",
            corpus="welcome",
            provider="anthropic",
            payload_fields=["prompt"],
        ),
        policy=_policy(
            {
                "global_mode": "you_pick",
                "corpus_policies": {"welcome": "prompt_only"},
            }
        ),
    )

    assert plan.allowed is False
    assert "does not allow role 'hyde'" in (plan.blocked_reason or "")


def test_cloud_primary_answerer_requires_answer_context() -> None:
    plan = plan_request(
        GatewayRequest(
            role="answerer",
            corpus="welcome",
            provider="anthropic",
            payload_fields=["prompt", "retrieved_snippets"],
        ),
        policy=_policy(
            {
                "global_mode": "you_pick",
                "corpus_policies": {"welcome": "retrieved_snippets"},
            }
        ),
    )

    assert plan.allowed is False
    assert "does not allow role 'answerer'" in (plan.blocked_reason or "")


def test_answer_context_allows_remote_answerer_with_bounded_context() -> None:
    plan = plan_request(
        GatewayRequest(
            role="answerer",
            corpus="welcome",
            provider="anthropic",
            payload_fields=["prompt", "retrieved_chunks", "answer_context"],
        ),
        policy=_policy(
            {
                "global_mode": "you_pick",
                "corpus_policies": {"welcome": "answer_context"},
            }
        ),
    )

    assert plan.allowed is True
    assert plan.egress_policy == "answer_context"


def test_payload_fields_cannot_exceed_corpus_policy() -> None:
    plan = plan_request(
        GatewayRequest(
            role="judge",
            corpus="welcome",
            provider="anthropic",
            payload_fields=["prompt", "retrieved_chunks"],
        ),
        policy=_policy(
            {
                "global_mode": "you_pick",
                "corpus_policies": {"welcome": "redacted_support"},
            }
        ),
    )

    assert plan.allowed is False
    assert plan.blocked_reason == "payload fields exceed corpus policy 'redacted_support'"


def test_desktop_proxy_cannot_call_remote_when_residency_owner_is_cloud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "errorta_model_gateway.router.gateway_owner",
        lambda: GatewayOwner(
            residency_mode="cloud",
            gateway_owner="cloud-sidecar",
            secret_location="cloud-sidecar",
            audit_location="cloud-errorta-home",
            local_proxy_may_call_remote=False,
        ),
    )

    plan = plan_request(
        GatewayRequest(role="judge", corpus="welcome", provider="anthropic"),
        policy=_policy(
            {
                "global_mode": "you_pick",
                "corpus_policies": {"welcome": "redacted_support"},
            }
        ),
    )

    assert plan.allowed is False
    assert plan.blocked_reason == "gateway owner is remote sidecar for active residency mode"


def test_monthly_cost_cap_includes_requested_estimated_cost() -> None:
    policy = _policy(
        {
            "global_mode": "you_pick",
            "corpus_policies": {"welcome": "redacted_support"},
            "budget": {
                **OPEN_REMOTE_BUDGET,
                "max_usd_per_month": 0.10,
            },
        }
    )
    budget.record_usage(
        audit_id="settled-1",
        provider="anthropic",
        role="judge",
        remote=True,
        input_tokens=10,
        estimated_cost_usd=0.08,
        session_id="session-1",
    )

    plan = plan_request(
        GatewayRequest(
            role="judge",
            corpus="welcome",
            provider="anthropic",
            payload_fields=["prompt"],
            input_tokens=12,
            estimated_cost_usd=0.03,
            session_id="session-1",
        ),
        policy=policy,
    )

    assert plan.allowed is False
    assert plan.blocked_reason == "max_usd_per_month exhausted"
    assert plan.budget
    assert plan.budget["estimated_usd_this_month"] == 0.08
    assert plan.budget["requested_estimated_cost_usd"] == 0.03


def test_session_cap_counts_missing_session_ids_in_anonymous_bucket() -> None:
    policy = _policy(
        {
            "global_mode": "you_pick",
            "corpus_policies": {"welcome": "redacted_support"},
            "budget": {
                **OPEN_REMOTE_BUDGET,
                "max_remote_calls_per_session": 1,
            },
        }
    )
    budget.record_usage(
        audit_id="settled-anonymous",
        provider="anthropic",
        role="judge",
        remote=True,
        input_tokens=10,
        session_id=None,
    )

    plan = plan_request(
        GatewayRequest(
            role="judge",
            corpus="welcome",
            provider="anthropic",
            payload_fields=["prompt"],
            input_tokens=8,
            session_id=None,
        ),
        policy=policy,
    )

    assert plan.allowed is False
    assert plan.blocked_reason == "max_remote_calls_per_session exhausted"
    assert plan.budget
    assert plan.budget["remote_calls_this_session"] == 1
    assert plan.budget["session_id_bucket"] == "anonymous"


def test_recorded_plan_does_not_settle_budget_until_provider_success() -> None:
    policy = _policy(
        {
            "global_mode": "you_pick",
            "corpus_policies": {"welcome": "redacted_support"},
            "budget": {
                **OPEN_REMOTE_BUDGET,
                "max_remote_calls_per_day": 1,
                "max_usd_per_month": 0.05,
            },
        }
    )

    plan = plan_request(
        GatewayRequest(
            role="judge",
            corpus="welcome",
            provider="anthropic",
            payload_fields=["prompt"],
            input_tokens=8,
            estimated_cost_usd=0.04,
            session_id="session-1",
        ),
        policy=policy,
        record=True,
    )

    assert plan.allowed is True
    assert plan.audit_id
    summary = budget.summary(policy.budget)
    assert summary["remote_calls_today"] == 0
    assert summary["estimated_usd_this_month"] == 0.0


def test_settled_plan_records_budget_usage_after_success() -> None:
    policy = _policy(
        {
            "global_mode": "you_pick",
            "corpus_policies": {"welcome": "redacted_support"},
            "budget": {
                **OPEN_REMOTE_BUDGET,
                "max_remote_calls_per_day": 1,
                "max_usd_per_month": 0.05,
            },
        }
    )

    plan = plan_request(
        GatewayRequest(
            role="judge",
            corpus="welcome",
            provider="anthropic",
            payload_fields=["prompt"],
            input_tokens=8,
            estimated_cost_usd=0.04,
            session_id="session-1",
        ),
        policy=policy,
        record=True,
        settle_usage=True,
    )

    assert plan.allowed is True
    assert plan.audit_id
    summary = budget.summary(policy.budget)
    assert summary["remote_calls_today"] == 1
    assert summary["estimated_usd_this_month"] == 0.04
