"""F009-01 `/services/*` route contract tests."""

from __future__ import annotations

from fastapi import HTTPException
from fastapi.testclient import TestClient

from errorta_app.auth import audit, store
from errorta_app.auth.ratelimit import auth_failure_limiter
from errorta_app.routes import services
from errorta_app.routes.services_runtime import ServicePipelineCapabilities
from errorta_app.server import app
from errorta_query.models import AnswerResult, QueryResult, Retrieval, Verdict
from errorta_query.pipeline import StubPipeline, UnavailablePipeline


class FakePipeline:
    def __init__(self) -> None:
        self.answer_calls: list[dict] = []
        self.query_calls: list[dict] = []

    def answer(self, **kwargs) -> AnswerResult:
        self.answer_calls.append(kwargs)
        return AnswerResult(
            answer="Retainers are governed by Section 4.",
            model=kwargs.get("model"),
            verdict=Verdict(
                rating="good",
                reason="Answer cites the relevant clause.",
                failure_tags=[],
                confidence=0.92,
            ),
            retrieval=Retrieval(grounded=True, reground_applied=False, top_k=4, chunks_used=1),
            prompt_signature="sig_test",
            aiar=True,
        )

    def query(self, **kwargs) -> list[QueryResult]:
        self.query_calls.append(kwargs)
        return [
            QueryResult(
                content="4. Retainers. Client shall deposit...",
                corpus_id="legal-cases",
                chunk_id="chunk-1",
                citation_id="cite-1",
                score=0.98,
                source="/corpora/legal-cases/standard.pdf",
                page_span=(3, 3),
            )
        ]

    # F009-02: the Service API route requires strict retrieval; route each fake's
    # `query` behavior (incl. subclass overrides) through the strict entrypoint.
    def query_strict(self, **kwargs) -> list[QueryResult]:
        return self.query(**kwargs)


class RaisingQueryPipeline(FakePipeline):
    def answer(self, **kwargs) -> AnswerResult:
        raise AssertionError("answer should not run when retrieval fails")

    def query(self, **kwargs) -> list[QueryResult]:
        self.query_calls.append(kwargs)
        raise RuntimeError("retrieve failed")


class EmptyQueryPipeline(FakePipeline):
    def query(self, **kwargs) -> list[QueryResult]:
        self.query_calls.append(kwargs)
        return []


class RuntimeFailurePipeline(FakePipeline):
    def answer(self, **kwargs) -> AnswerResult:
        self.answer_calls.append(kwargs)
        return AnswerResult(
            answer="",
            model=kwargs.get("model"),
            verdict=Verdict(
                rating="fail",
                reason="aiar pipeline error: boom",
                failure_tags=["aiar_pipeline_error"],
                confidence=None,
            ),
            retrieval=Retrieval(grounded=False, reground_applied=False, top_k=0, chunks_used=0),
            prompt_signature="sig_test",
            aiar=True,
        )


def _client(tmp_errorta_home, monkeypatch) -> tuple[TestClient, FakePipeline]:
    store.reset_state_for_tests()
    auth_failure_limiter.reset()
    (tmp_errorta_home / ".errorta" / "corpora" / "legal-cases").mkdir(parents=True)
    (tmp_errorta_home / ".errorta" / "corpora" / "other-corpus").mkdir(parents=True)
    pipeline = FakePipeline()
    monkeypatch.setattr(services, "default_pipeline", lambda: pipeline)
    return TestClient(app), pipeline


def _issue_token(*, corpora: list[str], scopes: list[str] | None = None) -> str:
    raw = store.mint_token()
    store.create_token(
        raw_token=raw,
        app_slug="sdk-demo",
        app_name="SDK Demo",
        corpora=corpora,
        scopes=scopes or ["prompt", "meta"],
    )
    return raw


def _prompt_body(**overrides) -> dict:
    body = {
        "prompt": "What's our retainer policy?",
        "corpus": "legal-cases",
        "model": "mistral-small3.1",
        "judge": True,
        "top_k": 8,
    }
    body.update(overrides)
    return body


def test_services_prompt_requires_token(tmp_errorta_home, monkeypatch):
    client, _pipeline = _client(tmp_errorta_home, monkeypatch)
    resp = client.post("/services/prompt", json=_prompt_body())
    assert resp.status_code == 401
    assert resp.json()["detail"] == "token_missing"


def test_services_prompt_denies_unapproved_corpus(tmp_errorta_home, monkeypatch):
    client, _pipeline = _client(tmp_errorta_home, monkeypatch)
    token = _issue_token(corpora=["other-corpus"])
    resp = client.post(
        "/services/prompt",
        json=_prompt_body(),
        headers={"X-Errorta-Token": token},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "token_corpus_denied"


def test_services_prompt_returns_corpus_not_found_after_auth(tmp_errorta_home, monkeypatch):
    client, _pipeline = _client(tmp_errorta_home, monkeypatch)
    token = _issue_token(corpora=["missing-corpus"])
    resp = client.post(
        "/services/prompt",
        json=_prompt_body(corpus="missing-corpus"),
        headers={"X-Errorta-Token": token},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "corpus_not_found"


def test_services_prompt_uses_pipeline_and_returns_contract_shape(
    tmp_errorta_home,
    monkeypatch,
):
    client, pipeline = _client(tmp_errorta_home, monkeypatch)
    token = _issue_token(corpora=["legal-cases"])

    resp = client.post(
        "/services/prompt",
        json=_prompt_body(),
        headers={"X-Errorta-Token": token},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["id"].startswith("prompt-")
    assert data["answer"] == "Retainers are governed by Section 4."
    assert data["verdict"]["rating"] == "pass"
    assert data["verdict"]["score"] == 4
    assert data["verdict"]["reasoning"] == "Answer cites the relevant clause."
    assert data["citations"] == [
        {
            "source_path": "/corpora/legal-cases/standard.pdf",
            "chunk_text": "4. Retainers. Client shall deposit...",
            "page_num": 3,
        }
    ]
    assert data["judge_model"] == "mistral-small3.1"
    assert data["latency_ms"] >= 0
    assert pipeline.query_calls[0]["top_k"] == 8
    assert pipeline.answer_calls[0]["corpus"] == "legal-cases"
    assert pipeline.answer_calls[0]["model"] == "mistral-small3.1"
    assert pipeline.answer_calls[0]["top_k"] == 8
    assert "System instructions:" not in pipeline.answer_calls[0]["prompt"]
    assert store.load_tokens()[0]["last_used_at"] is not None
    audit_text = "\n".join(str(item) for item in audit.read_events())
    assert "prompt" in audit_text
    assert "legal-cases" in audit_text
    assert "retrieval_status" in audit_text
    assert "citation_count" in audit_text
    assert token not in audit_text
    assert "What's our retainer policy?" not in audit_text


def test_services_prompt_rejects_system_without_runtime_support(tmp_errorta_home, monkeypatch):
    client, _pipeline = _client(tmp_errorta_home, monkeypatch)
    token = _issue_token(corpora=["legal-cases"])

    resp = client.post(
        "/services/prompt",
        json=_prompt_body(system="You are concise."),
        headers={"X-Errorta-Token": token},
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "system_not_supported"


def test_services_prompt_rejects_stub_pipeline(tmp_errorta_home, monkeypatch):
    client, _pipeline = _client(tmp_errorta_home, monkeypatch)
    monkeypatch.setattr(services, "default_pipeline", lambda: StubPipeline())
    token = _issue_token(corpora=["legal-cases"])

    resp = client.post(
        "/services/prompt",
        json=_prompt_body(),
        headers={"X-Errorta-Token": token},
    )

    assert resp.status_code == 503
    assert resp.json()["detail"] == "aiar_unavailable"
    assert "development answer" not in resp.text


def test_services_prompt_rejects_unavailable_pipeline(tmp_errorta_home, monkeypatch):
    client, _pipeline = _client(tmp_errorta_home, monkeypatch)
    monkeypatch.setattr(
        services,
        "default_pipeline",
        lambda: UnavailablePipeline("AIAR is disconnected", tag="aiar_disconnected"),
    )
    token = _issue_token(corpora=["legal-cases"])

    resp = client.post(
        "/services/prompt",
        json=_prompt_body(),
        headers={"X-Errorta-Token": token},
    )

    assert resp.status_code == 503
    assert resp.json()["detail"] == "aiar_unavailable"


def test_services_prompt_rejects_answer_result_runtime_failure(tmp_errorta_home, monkeypatch):
    client, _pipeline = _client(tmp_errorta_home, monkeypatch)
    failing = RuntimeFailurePipeline()
    monkeypatch.setattr(services, "default_pipeline", lambda: failing)
    token = _issue_token(corpora=["legal-cases"])

    resp = client.post(
        "/services/prompt",
        json=_prompt_body(),
        headers={"X-Errorta-Token": token},
    )

    assert resp.status_code == 503
    assert resp.json()["detail"] == "answer_unavailable"
    assert failing.answer_calls


def test_services_prompt_fails_when_retrieval_raises(tmp_errorta_home, monkeypatch):
    client, _pipeline = _client(tmp_errorta_home, monkeypatch)
    failing = RaisingQueryPipeline()
    monkeypatch.setattr(services, "default_pipeline", lambda: failing)
    token = _issue_token(corpora=["legal-cases"])

    resp = client.post(
        "/services/prompt",
        json=_prompt_body(),
        headers={"X-Errorta-Token": token},
    )

    assert resp.status_code == 503
    assert resp.json()["detail"] == "retrieval_unavailable"
    assert failing.answer_calls == []


def test_services_prompt_allows_zero_hit_retrieval(tmp_errorta_home, monkeypatch):
    client, _pipeline = _client(tmp_errorta_home, monkeypatch)
    empty = EmptyQueryPipeline()
    monkeypatch.setattr(services, "default_pipeline", lambda: empty)
    token = _issue_token(corpora=["legal-cases"])

    resp = client.post(
        "/services/prompt",
        json=_prompt_body(),
        headers={"X-Errorta-Token": token},
    )

    assert resp.status_code == 200
    assert resp.json()["citations"] == []
    events = audit.read_events()
    assert events[-1]["retrieval_status"] == "no_hits"
    assert events[-1]["citation_count"] == 0


def test_services_prompt_preserves_residency_catalog_http_exception(
    tmp_errorta_home,
    monkeypatch,
):
    client, _pipeline = _client(tmp_errorta_home, monkeypatch)
    token = _issue_token(corpora=["legal-cases"])

    def _raise_http():
        raise HTTPException(status_code=409, detail={"code": "residency_unsupported_path"})

    monkeypatch.setattr(services, "list_all_corpora", _raise_http)
    resp = client.post(
        "/services/prompt",
        json=_prompt_body(),
        headers={"X-Errorta-Token": token},
    )

    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "residency_unsupported_path"


def test_services_prompt_fails_on_local_catalog_exception(tmp_errorta_home, monkeypatch):
    client, _pipeline = _client(tmp_errorta_home, monkeypatch)
    token = _issue_token(corpora=["legal-cases"])

    def _raise_runtime():
        raise RuntimeError("catalog failed")

    monkeypatch.setattr(services, "list_all_corpora", _raise_runtime)
    resp = client.post(
        "/services/prompt",
        json=_prompt_body(),
        headers={"X-Errorta-Token": token},
    )

    assert resp.status_code == 503
    assert resp.json()["detail"] == "corpus_catalog_unavailable"


def test_services_prompt_rejects_oversized_metadata(tmp_errorta_home, monkeypatch):
    client, _pipeline = _client(tmp_errorta_home, monkeypatch)
    token = _issue_token(corpora=["legal-cases"])

    resp = client.post(
        "/services/prompt",
        json=_prompt_body(metadata={"request_id": "x" * 3000}),
        headers={"X-Errorta-Token": token},
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "metadata_too_large"


def test_services_prompt_rejects_nested_metadata(tmp_errorta_home, monkeypatch):
    client, _pipeline = _client(tmp_errorta_home, monkeypatch)
    token = _issue_token(corpora=["legal-cases"])

    resp = client.post(
        "/services/prompt",
        json=_prompt_body(metadata={"nested": {"nope": True}}),
        headers={"X-Errorta-Token": token},
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "metadata_unsupported"


def test_services_prompt_audits_metadata_safely(tmp_errorta_home, monkeypatch):
    client, _pipeline = _client(tmp_errorta_home, monkeypatch)
    token = _issue_token(corpora=["legal-cases"])

    resp = client.post(
        "/services/prompt",
        json=_prompt_body(
            metadata={
                "request_source": "vscode",
                "request_id": "abc123",
                "secretish": "do-not-log",
            },
        ),
        headers={"X-Errorta-Token": token},
    )

    assert resp.status_code == 200
    event = audit.read_events()[-1]
    assert event["metadata_keys"] == ["request_id", "request_source", "secretish"]
    assert event["metadata_request_source"] == "vscode"
    assert event["metadata_request_id"] == "abc123"
    assert "do-not-log" not in str(event)


def test_services_meta_requires_token(tmp_errorta_home, monkeypatch):
    client, _pipeline = _client(tmp_errorta_home, monkeypatch)
    resp = client.get("/services/meta")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "token_missing"


def test_services_meta_filters_corpora_to_token_grant(tmp_errorta_home, monkeypatch):
    client, _pipeline = _client(tmp_errorta_home, monkeypatch)
    token = _issue_token(corpora=["legal-cases"], scopes=["meta"])

    resp = client.get("/services/meta", headers={"X-Errorta-Token": token})

    assert resp.status_code == 200
    data = resp.json()
    assert data["sdk_contract_version"] == "1.0"
    assert [item["name"] for item in data["corpora"]] == ["legal-cases"]
    assert data["corpus_source"] == "local"
    assert data["catalog_verified"] is True


def test_services_meta_fails_on_catalog_unavailable(tmp_errorta_home, monkeypatch):
    client, _pipeline = _client(tmp_errorta_home, monkeypatch)
    token = _issue_token(corpora=["legal-cases"], scopes=["meta"])

    def _raise_runtime():
        raise RuntimeError("catalog failed")

    monkeypatch.setattr(services, "list_all_corpora", _raise_runtime)
    resp = client.get("/services/meta", headers={"X-Errorta-Token": token})

    assert resp.status_code == 503
    assert resp.json()["detail"] == "corpus_catalog_unavailable"


def test_services_meta_remote_unverified_uses_token_grants_only(
    tmp_errorta_home,
    monkeypatch,
):
    client, _pipeline = _client(tmp_errorta_home, monkeypatch)
    token = _issue_token(corpora=["legal-cases"], scopes=["meta"])
    monkeypatch.setattr(
        services,
        "classify_service_pipeline",
        lambda _pipeline: ServicePipelineCapabilities(
            runtime_kind="aiar-service",
            answer_available=True,
            retrieval_available=True,
            supports_top_k_answer=True,
        ),
    )

    monkeypatch.setattr(
        services,
        "list_all_corpora",
        lambda: {
            "corpora": [],
            "source": "remote_unverified",
            "verified": False,
            "backend_id": None,
        },
    )
    resp = client.get("/services/meta", headers={"X-Errorta-Token": token})

    assert resp.status_code == 200
    data = resp.json()
    assert data["catalog_verified"] is False
    assert data["corpus_source"] == "remote_unverified"
    assert data["corpora"] == [
        {
            "name": "legal-cases",
            "status": "unknown",
            "source": "remote_unverified",
            "unit": "unknown",
        }
    ]


def test_retrieve_citations_requires_strict_retrieval():
    """F009-02 regression: a pipeline offering only best-effort query() (no
    query_strict) must be refused (fail closed), never silently degraded."""
    import pytest

    from errorta_app.routes.services import ServiceApiError, _retrieve_citations

    class _NoStrictPipeline:
        def query(self, **kwargs):  # best-effort only — must be rejected
            return []

    with pytest.raises(ServiceApiError):
        _retrieve_citations(
            pipeline=_NoStrictPipeline(), prompt="p", corpus="c", top_k=4)
