"""F030-01 — model-gateway provider dispatch.

Tests the route's dispatch/settle/error contract in isolation: ``plan_request``
(the policy/budget engine) is tested elsewhere, so here we stub it with a fake
ALLOWED plan and stub the provider handler, then assert the route (a) dispatches,
(b) settles the budget ledger with the provider's REAL token counts for remote,
(c) never dispatches a blocked plan, (d) maps provider failures to clean HTTP.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from errorta_app.routes import model_gateway as route_mod


def _client(tmp_path: Path, monkeypatch) -> TestClient:
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    app = FastAPI()
    app.include_router(route_mod.router)
    return TestClient(app)


def _allowed_plan(*, remote: bool):
    plan = SimpleNamespace(
        allowed=True, remote=remote, audit_id="mg_test" if remote else None,
        provider="anthropic" if remote else "local", model="claude-x",
        role="judge", corpus=None, egress_policy="local_only",
        egress_class="local", payload_fields=["prompt"],
    )
    plan.to_dict = lambda: {"allowed": True, "provider": plan.provider}
    return plan


class _Result:
    def __init__(self, content, input_tokens, output_tokens):
        self.content = content
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


def _post(client):
    return client.post(
        "/model-gateway/ollama/api/chat",
        json={"model": "errorta.judge.remote",
              "messages": [{"role": "user", "content": "hi"}]},
    )


def test_remote_dispatch_settles_real_token_counts(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(route_mod, "plan_request", lambda *a, **k: _allowed_plan(remote=True))

    async def _fake_dispatch(plan, body):
        return _Result("remote answer", input_tokens=11, output_tokens=22)

    monkeypatch.setattr(route_mod, "_dispatch_resolved", _fake_dispatch)
    settled: list = []
    monkeypatch.setattr(route_mod.budget, "record_usage", lambda **kw: settled.append(kw))

    resp = _post(_client(tmp_path, monkeypatch))
    assert resp.status_code == 200
    body = resp.json()
    assert body["message"]["content"] == "remote answer"
    assert body["prompt_eval_count"] == 11 and body["eval_count"] == 22
    assert body["gateway"] == {
        "provider": "anthropic", "model": "claude-x", "remote": True, "audit_id": "mg_test",
    }
    # The settle used the provider's REAL counts (not the len//4 estimate), linked
    # to the pre-call audit, marked remote.
    assert len(settled) == 1
    assert settled[0]["input_tokens"] == 11 and settled[0]["output_tokens"] == 22
    assert settled[0]["remote"] is True and settled[0]["audit_id"] == "mg_test"


def test_blocked_plan_never_dispatches(tmp_path, monkeypatch) -> None:
    blocked = SimpleNamespace(allowed=False)
    blocked.to_dict = lambda: {"allowed": False, "blocked_reason": "policy"}
    monkeypatch.setattr(route_mod, "plan_request", lambda *a, **k: blocked)

    async def _boom(plan, body):  # the choke point: never reached for a blocked plan
        raise AssertionError("dispatch must not run for a blocked plan")

    monkeypatch.setattr(route_mod, "_dispatch_resolved", _boom)
    resp = _post(_client(tmp_path, monkeypatch))
    assert resp.status_code == 403
    assert resp.json()["detail"]["allowed"] is False


def test_provider_error_maps_to_clean_http(tmp_path, monkeypatch) -> None:
    from errorta_council.gateway_local import FatalError, RetryableError

    monkeypatch.setattr(route_mod, "plan_request", lambda *a, **k: _allowed_plan(remote=True))
    monkeypatch.setattr(route_mod, "budget", route_mod.budget)  # no-op, keep ledger real

    async def _retryable(plan, body):
        raise RetryableError("429 rate limited")

    monkeypatch.setattr(route_mod, "_dispatch_resolved", _retryable)
    resp = _post(_client(tmp_path, monkeypatch))
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "provider_error"
    assert resp.json()["detail"]["retryable"] is True

    async def _fatal(plan, body):
        raise FatalError("401 bad key")

    monkeypatch.setattr(route_mod, "_dispatch_resolved", _fatal)
    resp = _post(_client(tmp_path, monkeypatch))
    assert resp.status_code == 502
    assert resp.json()["detail"]["retryable"] is False
