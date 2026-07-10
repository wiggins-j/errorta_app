"""F116 — raw AIAR service pipeline adapter."""
from __future__ import annotations

import httpx

from errorta_query.aiar_service_pipeline import AiarServicePipeline, AiarServicePipelineError


class _FakeClient:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict | None]] = []

    def __call__(self, *args, **kwargs):  # noqa: ANN002, ANN003 - httpx shim
        return self

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        return None

    def request(self, method: str, url: str, *, json=None) -> httpx.Response:  # noqa: ANN001
        self.requests.append((method, url, json))
        if url.endswith("/services/prompt"):
            return httpx.Response(
                200,
                json={
                    "answer": "remote answer",
                    "model": "qwen3.5:9b",
                    "verdict": {
                        "rating": "good",
                        "reason": "grounded",
                        "failure_tags": [],
                        "confidence": 0.91,
                    },
                    "sources": [{"text": "chunk"}],
                    "grounded": True,
                    "instance": "alpha",
                    "call_id": "call-1",
                },
            )
        if "/instances/alpha/retrieve" in url:
            return httpx.Response(
                200,
                json={
                    "instance": "alpha",
                    "hits": [
                        {
                            "text": "retrieved chunk",
                            "chunk_id": "c1",
                            "score": 0.87,
                            "source": "doc.pdf",
                            "page_span": [2, 3],
                        }
                    ],
                },
            )
        return httpx.Response(404, json={"detail": "missing"})


def test_aiar_service_pipeline_maps_answer_and_retrieval(monkeypatch) -> None:
    fake = _FakeClient()
    monkeypatch.setattr(httpx, "Client", fake)
    pipe = AiarServicePipeline(
        "http://example-host.local:8766",
        token="secret-token",
        timeout_s=30,
    )

    answer = pipe.answer(
        prompt="What changed?",
        corpus="alpha",
        judge=True,
        reground=True,
        model=None,
        top_k=7,
    )
    hits = pipe.query(prompt="What changed?", corpus_ids=["alpha"], top_k=2)

    assert answer.aiar is True
    assert answer.answer == "remote answer"
    assert answer.model == "qwen3.5:9b"
    assert answer.verdict is not None
    assert answer.verdict.rating == "pass"
    assert answer.grounded is True
    assert answer.instance == "alpha"
    assert hits[0].content == "retrieved chunk"
    assert hits[0].page_span == (2, 3)
    assert fake.requests[0][2]["service_name"] == "errorta-judge"
    assert fake.requests[0][2]["instance"] == "alpha"
    assert fake.requests[0][2]["top_k"] == 7


def test_aiar_service_query_strict_raises_on_retrieval_failure(monkeypatch) -> None:
    fake = _FakeClient()
    monkeypatch.setattr(httpx, "Client", fake)
    pipe = AiarServicePipeline(
        "http://example-host.local:8766",
        token="secret-token",
        timeout_s=30,
    )

    assert pipe.query(prompt="What changed?", corpus_ids=["missing"], top_k=2) == []
    try:
        pipe.query_strict(prompt="What changed?", corpus_ids=["missing"], top_k=2)
    except AiarServicePipelineError as exc:
        assert "404" in str(exc)
    else:
        raise AssertionError("query_strict should raise on retrieval failure")
