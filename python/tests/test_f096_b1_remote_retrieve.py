"""F096 B1 — real remote-AIAR pure retrieval wired into the query pipeline.

Hermetic: mocks the AIAR ``aiar.retrieve.v1`` HTTP shape so the mapper + routing
are tested offline. Live validation against example-host is
``python/scripts/validate_f096_retrieve_live.py`` (env-gated, not a unit test).
"""
from __future__ import annotations

import httpx
import pytest

from errorta_query import aiar_retrieve
from errorta_query.models import QueryResult


def _retrieve_response(instance: str, hits: list[dict]) -> dict:
    return {
        "schema_version": "aiar.retrieve.v1", "instance": instance,
        "query": "q", "k": len(hits), "count": len(hits),
        "score_kind": "cosine_similarity", "score_order": "higher_is_better",
        "hits": hits,
    }


_HIT = {
    "chunk_id": "c-1", "source": "doc.md", "title": "Doc", "text": "hello world",
    "score": 0.81, "chunk_index": 3, "category": "general",
    "page_span": [2, 4], "metadata": {"k": "v"},
}


def test_map_hit_carries_full_provenance() -> None:
    qr = aiar_retrieve.map_hit_to_query_result(_HIT, instance="welcome")
    assert isinstance(qr, QueryResult)
    assert qr.content == "hello world" and qr.corpus_id == "welcome"
    assert qr.chunk_id == "c-1" and qr.citation_id == "c-1"
    assert qr.score == 0.81 and qr.source == "doc.md" and qr.title == "Doc"
    assert qr.page_span == (2, 4) and qr.metadata == {"k": "v"}


def test_remote_retrieve_merges_and_trims_by_score(monkeypatch) -> None:
    monkeypatch.setattr(aiar_retrieve, "aiar_retrieval_target",
                        lambda: ("http://aiar.test:8766", "tok"), raising=False)
    # backend.aiar_retrieval_target is imported lazily inside the function:
    monkeypatch.setattr("errorta_query.backend.aiar_retrieval_target",
                        lambda: ("http://aiar.test:8766", "tok"))

    calls: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        inst = request.url.path.split("/instances/")[1].split("/retrieve")[0]
        # Bearer auth must be present (not the residency X-Errorta-Token).
        assert request.headers.get("Authorization") == "Bearer tok"
        calls.append((inst, {}))
        score = {"a": 0.9, "b": 0.5}[inst]
        return httpx.Response(200, json=_retrieve_response(
            inst, [{**_HIT, "chunk_id": f"{inst}-1", "score": score}]))

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(aiar_retrieve.httpx, "Client", client_factory)

    out = aiar_retrieve.remote_aiar_retrieve(
        prompt="hi", corpus_ids=["a", "b"], top_k=1)
    assert {c[0] for c in calls} == {"a", "b"}          # queried both corpora
    assert len(out) == 1 and out[0].chunk_id == "a-1"   # merged + trimmed by score


def test_remote_retrieve_no_target_returns_empty(monkeypatch) -> None:
    monkeypatch.setattr("errorta_query.backend.aiar_retrieval_target", lambda: None)
    assert aiar_retrieve.remote_aiar_retrieve(
        prompt="hi", corpus_ids=["a"], top_k=4) == []


def test_remote_retrieve_strict_no_target_raises(monkeypatch) -> None:
    monkeypatch.setattr("errorta_query.backend.aiar_retrieval_target", lambda: None)
    with pytest.raises(aiar_retrieve.AiarRetrieveError):
        aiar_retrieve.remote_aiar_retrieve(
            prompt="hi",
            corpus_ids=["a"],
            top_k=4,
            strict=True,
        )


def test_remote_retrieve_failsafe_on_transport_error(monkeypatch) -> None:
    monkeypatch.setattr("errorta_query.backend.aiar_retrieval_target",
                        lambda: ("http://aiar.test:8766", "tok"))

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    transport = httpx.MockTransport(boom)
    real_client = httpx.Client
    monkeypatch.setattr(aiar_retrieve.httpx, "Client",
                        lambda *a, **k: real_client(*a, transport=transport, **k))
    # a transport blip yields no hits, never raises
    assert aiar_retrieve.remote_aiar_retrieve(
        prompt="hi", corpus_ids=["a"], top_k=4) == []


def test_remote_retrieve_strict_transport_error_raises(monkeypatch) -> None:
    monkeypatch.setattr("errorta_query.backend.aiar_retrieval_target",
                        lambda: ("http://aiar.test:8766", "tok"))

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down with Bearer tok")

    transport = httpx.MockTransport(boom)
    real_client = httpx.Client
    monkeypatch.setattr(aiar_retrieve.httpx, "Client",
                        lambda *a, **k: real_client(*a, transport=transport, **k))

    with pytest.raises(aiar_retrieve.AiarRetrieveError) as exc:
        aiar_retrieve.remote_aiar_retrieve(
            prompt="hi",
            corpus_ids=["a"],
            top_k=4,
            strict=True,
        )
    assert "tok" not in str(exc.value)


def test_default_pipeline_wraps_for_remote_retrieval(monkeypatch) -> None:
    import errorta_aiar_connection
    from errorta_aiar_connection.models import AiarRuntime
    from errorta_query import pipeline

    monkeypatch.setattr(
        errorta_aiar_connection,
        "resolve_aiar_runtime",
        lambda: AiarRuntime(
            kind="disconnected",
            display_name="AIAR disconnected",
            connected=False,
            config_source="none",
        ),
    )
    monkeypatch.setattr("errorta_query.backend.aiar_retrieval_target",
                        lambda: ("http://aiar.test:8766", "tok"))
    # no residency remote → local path, then wrapped for remote retrieval
    monkeypatch.setattr("errorta_residency.config.load", lambda: None, raising=False)
    pipe = pipeline.default_pipeline()
    assert type(pipe).__name__ == "_RemoteRetrievalPipeline"
