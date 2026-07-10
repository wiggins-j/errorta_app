"""Tests for the judge FastAPI router (errorta_app.routes.judge).

Per F001-SEAM, the router resolves ``errorta_query.pipeline.default_pipeline``
at request time unless tests install a module-level ``_pipeline`` override.
Tests inject a mock pipeline via the ``mock_aiar_pipeline`` fixture, and a
mock grounding sink via ``mock_grounding_store``.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from errorta_app.routes import judge as judge_routes


@pytest.fixture
def client() -> TestClient:
    """Mount only the judge router on a minimal app.

    Importing ``errorta_app.server`` would also load sibling routers
    (e.g. hardware -> psutil) that pollute session-wide module state and
    break other tests' fixture-based mocks of those modules.
    """
    app = FastAPI()
    app.include_router(judge_routes.router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_judge_model_override(monkeypatch: pytest.MonkeyPatch):
    """Reset the module-level override and env var between tests."""
    monkeypatch.setattr(judge_routes, "_judge_model_override", None, raising=True)
    monkeypatch.setattr(judge_routes, "_pipeline", None, raising=True)
    monkeypatch.delenv("EVAL_JUDGE_MODEL", raising=False)
    yield


def test_verdict_happy_path(
    client: TestClient,
    tmp_errorta_home: Path,
    mock_aiar_pipeline: MagicMock,
) -> None:
    resp = client.post("/judge/verdict", json={"prompt": "what orbits earth?"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"] == "stub answer"
    assert body["verdict"]["rating"] == "pass"
    assert body["verdict"]["latency_ms"] is not None
    assert body["id"]
    mock_aiar_pipeline.answer.assert_called_once()


def test_verdict_resolves_default_pipeline_per_request(
    client: TestClient,
    tmp_errorta_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runtime routing changes must affect the next judge call."""

    def _result(answer: str) -> MagicMock:
        fake = MagicMock()
        fake.answer = answer
        fake.raw_verdict = {
            "rating": "pass",
            "reason": "ok",
            "failure_tags": [],
            "confidence": 0.9,
        }
        fake.verdict = None
        return fake

    first = MagicMock()
    first.answer = MagicMock(return_value=_result("first route"))
    second = MagicMock()
    second.answer = MagicMock(return_value=_result("second route"))
    factory = MagicMock(side_effect=[first, second])
    monkeypatch.setattr(judge_routes, "default_pipeline", factory)

    r1 = client.post("/judge/verdict", json={"prompt": "first prompt"})
    r2 = client.post("/judge/verdict", json={"prompt": "second prompt"})

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["answer"] == "first route"
    assert r2.json()["answer"] == "second route"
    assert factory.call_count == 2
    first.answer.assert_called_once()
    second.answer.assert_called_once()


def test_verdict_empty_prompt_returns_400(
    client: TestClient,
    tmp_errorta_home: Path,
    mock_aiar_pipeline: MagicMock,
) -> None:
    resp = client.post("/judge/verdict", json={"prompt": "   "})
    assert resp.status_code == 400


def test_correction_draft_endpoint(client: TestClient) -> None:
    resp = client.post(
        "/judge/correction-draft",
        json={
            "answer": "first draft",
            "verdict": {"rating": "fail", "reason": "wrong", "failure_tags": ["bad"]},
        },
    )
    assert resp.status_code == 200
    draft = resp.json()["draft"]
    assert "Judge said: wrong" in draft
    assert "Tags: bad" in draft
    assert "first draft" in draft


def test_accept_404_on_missing_id(client: TestClient, tmp_errorta_home: Path) -> None:
    resp = client.post("/judge/accept", json={"id": "nonexistent", "correction": "x"})
    assert resp.status_code == 404


def test_accept_happy_path_records_grounding(
    client: TestClient,
    tmp_errorta_home: Path,
    mock_aiar_pipeline: MagicMock,
    mock_grounding_store: MagicMock,
) -> None:
    v = client.post("/judge/verdict", json={"prompt": "p"}).json()
    accept = client.post(
        "/judge/accept", json={"id": v["id"], "correction": "the right answer"}
    )
    assert accept.status_code == 200
    body = accept.json()
    assert body["correction"] == "the right answer"
    assert body["grounding_recorded"] is True
    mock_grounding_store.assert_called_once()


def test_record_grounding_mirrors_aiar_to_local_f024_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline = MagicMock()
    pipeline.record_grounding.return_value = True
    local = MagicMock()
    monkeypatch.setattr(judge_routes, "_pipeline", pipeline)
    monkeypatch.setattr(judge_routes._stub_grounding, "record_with_embedding", local)

    ok = judge_routes._record_grounding(
        prompt="prompt text",
        answer="answer text",
        correction="corrected answer",
        verdict={"rating": "partial"},
    )

    assert ok is True
    local.assert_called_once()
    pipeline.record_grounding.assert_called_once()
    assert pipeline.record_grounding.call_args.kwargs["instance"] is None


def test_record_grounding_passes_instance_to_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline = MagicMock()
    pipeline.record_grounding.return_value = True
    monkeypatch.setattr(judge_routes, "_pipeline", pipeline)
    monkeypatch.setattr(
        judge_routes._stub_grounding, "record_with_embedding", MagicMock()
    )

    ok = judge_routes._record_grounding(
        prompt="prompt text",
        answer="answer text",
        correction="corrected answer",
        verdict={"rating": "partial"},
        instance="welcome",
    )

    assert ok is True
    assert pipeline.record_grounding.call_args.kwargs["instance"] == "welcome"


def test_verdict_response_passes_through_aiar_telemetry(
    client: TestClient,
    tmp_errorta_home: Path,
    mock_aiar_pipeline: MagicMock,
) -> None:
    result = mock_aiar_pipeline.answer.return_value
    result.model = "llama3.1:8b"
    result.call_id = "call-abc"
    result.instance = "welcome"
    result.grounded = True
    result.reground_applied = False
    result.rag_enabled = True
    result.latency = 12.5

    resp = client.post(
        "/judge/verdict",
        json={"prompt": "what orbits earth?", "corpus": "welcome"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["model"] == "llama3.1:8b"
    assert body["call_id"] == "call-abc"
    assert body["instance"] == "welcome"
    assert body["grounded"] is True
    assert body["reground_applied"] is False
    assert body["rag_enabled"] is True
    assert body["latency"] == 12.5


def test_metrics_endpoint(client: TestClient, tmp_errorta_home: Path) -> None:
    resp = client.get("/judge/metrics")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 0
    assert len(body["trend_7d"]) == 7


def test_preflight_ollama_reachable(
    client: TestClient,
    tmp_errorta_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    resp_mock = MagicMock(status_code=200)
    resp_mock.json.return_value = {"models": [{"name": "llama3.1:8b"}]}
    monkeypatch.setattr(httpx, "get", lambda *a, **k: resp_mock)

    r = client.get("/judge/preflight")
    assert r.status_code == 200
    body = r.json()
    assert body["judge_model"] == "llama3.1:8b"
    assert body["ollama_reachable"] is True
    assert body["model_available"] is True


def test_preflight_ollama_unreachable(
    client: TestClient,
    tmp_errorta_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    def boom(*a, **k):
        raise httpx.ConnectError("nope")

    monkeypatch.setattr(httpx, "get", boom)
    r = client.get("/judge/preflight")
    assert r.status_code == 200
    body = r.json()
    assert body["ollama_reachable"] is False
    assert body["error"] is not None


def test_preflight_aiar_missing(
    client: TestClient,
    tmp_errorta_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Force the AIAR re-probe inside the route to raise.

    Force the active runtime resolver into the legacy local-probe path,
    then monkeypatch ``AiarPipeline`` to raise so ``aiar_available``
    resolves to False regardless of the host env.
    """
    import httpx

    from errorta_aiar_connection.models import disconnected
    from errorta_judge import aiar_adapter

    def _raise(*a, **k):
        raise RuntimeError("simulated aiar missing")

    monkeypatch.setattr(
        "errorta_aiar_connection.resolve_aiar_runtime",
        lambda: disconnected(display_name="This Mac", config_source="none"),
    )
    monkeypatch.setattr(aiar_adapter.AiarPipeline, "__init__", _raise)
    monkeypatch.setattr(
        httpx, "get", lambda *a, **k: MagicMock(status_code=200, json=lambda: {"models": []})
    )

    r = client.get("/judge/preflight")
    assert r.status_code == 200
    assert r.json()["aiar_available"] is False


def test_preflight_aiar_present(
    client: TestClient,
    tmp_errorta_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Force the AIAR re-probe to succeed."""
    import httpx

    from errorta_aiar_connection.models import disconnected
    from errorta_judge import aiar_adapter

    monkeypatch.setattr(
        "errorta_aiar_connection.resolve_aiar_runtime",
        lambda: disconnected(display_name="This Mac", config_source="none"),
    )
    monkeypatch.setattr(aiar_adapter.AiarPipeline, "__init__", lambda self: None)
    monkeypatch.setattr(
        httpx, "get", lambda *a, **k: MagicMock(status_code=200, json=lambda: {"models": []})
    )

    r = client.get("/judge/preflight")
    assert r.status_code == 200
    assert r.json()["aiar_available"] is True


def test_preflight_uses_remote_aiar_runtime_without_local_ollama_probe(
    client: TestClient,
    tmp_errorta_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from errorta_aiar_connection.models import AiarCapabilities, AiarRuntime

    runtime = AiarRuntime(
        kind="aiar-service",
        display_name="example-host",
        connected=True,
        base_url="http://example-host.local:8766",
        token="secret-token",
        backend_id="example-host",
        active_model="qwen3.5:9b",
        active_model_ready=True,
        available_models=["qwen3.5:9b"],
        corpus_count=12,
        capabilities=AiarCapabilities(
            answer=True,
            judge=True,
            model_catalog=True,
            model_active_status=True,
            pure_retrieve=True,
            remote_ingest=True,
        ),
        config_source="legacy_remote_aiar",
        status_source="healthz",
    )
    monkeypatch.setattr("errorta_aiar_connection.resolve_aiar_runtime", lambda: runtime)

    def _no_local_ollama(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("remote preflight must not probe local Ollama")

    monkeypatch.setattr("httpx.get", _no_local_ollama)

    r = client.get("/judge/preflight")
    assert r.status_code == 200
    body = r.json()
    assert body["runtime_kind"] == "aiar-service"
    assert body["display_name"] == "example-host"
    assert body["aiar_connected"] is True
    assert body["aiar_available"] is True
    assert body["ollama_reachable"] is True
    assert body["model_available"] is True
    assert body["active_model"] == "qwen3.5:9b"
    assert body["capabilities"]["ollama_pull"] is False
    assert "secret-token" not in str(body)


def test_get_model_default(client: TestClient) -> None:
    r = client.get("/judge/model")
    assert r.status_code == 200
    body = r.json()
    assert body["judge_model"] == judge_routes.DEFAULT_JUDGE_MODEL
    assert body["source"] == "default"


def test_get_model_env(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVAL_JUDGE_MODEL", "qwen2:7b")
    r = client.get("/judge/model")
    body = r.json()
    assert body["judge_model"] == "qwen2:7b"
    assert body["source"] == "env"


def test_put_model_override_then_clear(client: TestClient) -> None:
    r = client.put("/judge/model", json={"judge_model": "mistral:7b"})
    assert r.status_code == 200
    body = r.json()
    assert body["judge_model"] == "mistral:7b"
    assert body["source"] == "override"

    # Clearing the override falls back to default.
    r2 = client.put("/judge/model", json={"judge_model": ""})
    body2 = r2.json()
    assert body2["judge_model"] == judge_routes.DEFAULT_JUDGE_MODEL
    assert body2["source"] == "default"


def test_verdict_response_includes_prompt_signature(
    client: TestClient,
    tmp_errorta_home: Path,
    mock_aiar_pipeline: MagicMock,
) -> None:
    """F001-deepen-01: VerdictResponse carries 64-hex prompt_signature."""
    resp = client.post("/judge/verdict", json={"prompt": "what orbits earth?"})
    assert resp.status_code == 200
    body = resp.json()
    sig = body.get("prompt_signature")
    assert isinstance(sig, str)
    assert len(sig) == 64
    # SHA-256 hex digest — lowercase hex chars only.
    assert all(c in "0123456789abcdef" for c in sig)


def test_override_resets_between_tests(client: TestClient) -> None:
    """Sanity check that the autouse reset fixture cleans the override."""
    assert judge_routes._judge_model_override is None
    r = client.get("/judge/model")
    assert r.json()["source"] == "default"
