from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from errorta_app.routes import model_gateway as route_mod


def _client(tmp_path: Path, monkeypatch) -> TestClient:
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    monkeypatch.delenv("AIAR_LOCAL_ONLY", raising=False)
    monkeypatch.delenv("ERRORTA_MODEL_GATEWAY_LOCAL_ONLY", raising=False)
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    app = FastAPI()
    app.include_router(route_mod.router)
    return TestClient(app)


def test_model_gateway_settings_route_round_trips(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)

    resp = client.put(
        "/model-gateway/settings",
        json={
            "global_mode": "user_selected",
            "role_routes": {"judge": {"provider": "anthropic", "model": "claude"}},
            "corpus_policies": {"welcome": "redacted_support"},
            "budget": {
                "max_tokens_per_call": 400,
                "max_remote_calls_per_day": None,
                "max_remote_calls_per_session": None,
                "max_usd_per_month": None,
            },
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["global_mode"] == "you_pick"
    assert body["role_routes"]["judge"]["provider"] == "anthropic"
    assert body["role_routes"]["judge"]["model"] == "claude"
    assert body["budget"]["max_tokens_per_call"] == 400

    saved = json.loads(
        (tmp_path / "model-gateway" / "policy.json").read_text(encoding="utf-8")
    )
    assert saved["corpus_policies"] == {"welcome": "redacted_support"}


def test_plan_route_records_allowed_remote_support_call(
    tmp_path: Path,
    monkeypatch,
) -> None:
    client = _client(tmp_path, monkeypatch)
    client.put(
        "/model-gateway/policy",
        json={
            "global_mode": "you_pick",
            "role_routes": {"judge": {"provider": "anthropic"}},
            "corpus_policies": {"welcome": "redacted_support"},
            "budget": {
                "max_remote_calls_per_day": None,
                "max_remote_calls_per_session": None,
                "max_usd_per_month": None,
            },
        },
    )

    resp = client.post(
        "/model-gateway/plan",
        json={
            "role": "judge",
            "corpus": "welcome",
            "prompt": "review help@errorta.app using sk-ant-secretsecretsecret",
            "payload_fields": ["prompt", "answer", "redacted_snippets"],
            "input_tokens": 12,
            "estimated_cost_usd": 0.01,
            "session_id": "session-1",
            "record": True,
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["advisory"] is True
    assert body["dispatch_authority"] is False
    assert body["payload_fields_source"] == "caller_declared"
    assert body["allowed"] is True
    assert body["provider"] == "anthropic"
    assert body["audit_id"]
    assert body["budget"]["requested_estimated_cost_usd"] == 0.01

    audit = client.get("/model-gateway/audit").json()["events"][0]
    assert audit["request_id"] == body["audit_id"]
    assert audit["estimated_cost_usd"] == 0.01
    assert audit["payload_sha256"]
    assert "sk-ant-secret" not in audit["preview_redacted"]
    assert "<token-redacted>" in audit["preview_redacted"]
    assert "help@errorta.app" not in audit["preview_redacted"]
    assert "<email-redacted>" in audit["preview_redacted"]

    budget = client.get("/model-gateway/budget").json()
    assert budget["remote_calls_today"] == 0
    assert budget["estimated_usd_this_month"] == 0.0


def test_plan_route_blocks_when_budget_exceeded(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    client.put(
        "/model-gateway/policy",
        json={
            "global_mode": "you_pick",
            "role_routes": {"judge": {"provider": "anthropic"}},
            "corpus_policies": {"welcome": "redacted_support"},
            "budget": {
                "max_tokens_per_call": 10,
                "max_remote_calls_per_day": None,
                "max_remote_calls_per_session": None,
                "max_usd_per_month": None,
            },
        },
    )

    resp = client.post(
        "/model-gateway/plan",
        json={
            "role": "judge",
            "corpus": "welcome",
            "payload_fields": ["prompt"],
            "input_tokens": 11,
            "record": True,
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["advisory"] is True
    assert body["dispatch_authority"] is False
    assert body["allowed"] is False
    assert body["blocked_reason"] == "max_tokens_per_call exceeded"
    assert body["audit_id"]


def test_ollama_compat_remote_alias_blocks_before_dispatch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    client = _client(tmp_path, monkeypatch)

    resp = client.post(
        "/model-gateway/ollama/api/chat",
        json={
            "model": "errorta.judge.remote",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": False,
        },
    )

    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert detail["allowed"] is False
    assert detail["provider"] == "anthropic"
    assert detail["audit_id"]


def test_ollama_compat_remote_alias_derives_payload_fields_server_side(
    tmp_path: Path,
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    class FakePlan:
        allowed = False

        def to_dict(self) -> dict[str, object]:
            request = captured["request"]
            return {
                "allowed": False,
                "provider": request.provider,
                "payload_fields": request.payload_fields,
                "blocked_reason": "blocked by fake planner",
            }

    def fake_plan_request(request, *, record=False, settle_usage=False):
        captured["request"] = request
        captured["record"] = record
        captured["settle_usage"] = settle_usage
        return FakePlan()

    monkeypatch.setattr(route_mod, "plan_request", fake_plan_request)
    client = _client(tmp_path, monkeypatch)

    resp = client.post(
        "/model-gateway/ollama/api/chat",
        json={
            "model": "errorta.judge.remote",
            "messages": [{"role": "user", "content": "review this"}],
            "payload_fields": ["prompt", "retrieved_chunks", "answer_context"],
        },
    )

    assert resp.status_code == 403
    request = captured["request"]
    assert request.provider == "anthropic"
    assert request.model is None
    assert request.payload_fields == ["prompt"]
    assert captured["record"] is True
    assert captured["settle_usage"] is False
    assert resp.json()["detail"]["payload_fields"] == ["prompt"]


def test_provider_model_for_plan_strips_route_prefixes() -> None:
    class Plan:
        provider = "anthropic"
        model = "anthropic.claude-sonnet-4-6"

    body = route_mod.OllamaChatRequest(model="errorta.judge.remote")

    assert route_mod._provider_model_for_plan(Plan(), body) == "claude-sonnet-4-6"


def test_provider_model_for_plan_strips_local_ollama_prefix() -> None:
    class Plan:
        provider = "local"
        model = "local.ollama.llama3.2:3b"

    body = route_mod.OllamaChatRequest(model="errorta.judge.local")

    assert route_mod._provider_model_for_plan(Plan(), body) == "llama3.2:3b"


class _FakeResult:
    def __init__(self, content="hi", input_tokens=None, output_tokens=None):
        self.content = content
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


def test_ollama_compat_local_alias_dispatches(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """F030-01: a local alias now DISPATCHES (was 501). Local is unmetered, so no
    budget settle. We stub the provider handler so the test needs no live Ollama."""
    async def _fake_dispatch(plan, body):
        assert plan.allowed and plan.remote is False  # local route
        return _FakeResult(content="local answer")

    monkeypatch.setattr(route_mod, "_dispatch_resolved", _fake_dispatch)
    settled: list = []
    monkeypatch.setattr(
        route_mod.budget, "record_usage",
        lambda **kw: settled.append(kw),
    )
    client = _client(tmp_path, monkeypatch)

    resp = client.post(
        "/model-gateway/ollama/api/chat",
        json={
            "model": "errorta.judge.local",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": False,
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["message"]["content"] == "local answer"
    assert body["done"] is True
    assert body["gateway"]["remote"] is False
    assert settled == []  # local is not settled to the remote budget ledger
