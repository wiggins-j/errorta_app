"""F001 Pipeline.query() boundary tests.

Locks the new retrieval seam (PM decision 2026-06-12) used by Council.
The Pipeline Protocol gains a query() method; StubPipeline returns []
(no AIAR); AiarPipeline delegates to AIAR's retrieval surface.

NO `import aiar` in this test file — it asserts on the seam, not AIAR.
"""
from __future__ import annotations

import pytest

from errorta_query.models import QueryResult
from errorta_query.pipeline import Pipeline, StubPipeline


def test_query_result_is_frozen():
    r = QueryResult(
        content="hi",
        corpus_id="c1",
        chunk_id="ch1",
        citation_id="ct1",
        score=0.9,
        tokens=1,
    )
    with pytest.raises((AttributeError, Exception)):
        r.content = "MUTATED"  # type: ignore[misc]


def test_stub_pipeline_query_returns_empty():
    stub = StubPipeline()
    out = stub.query(prompt="any prompt", corpus_ids=["welcome"], top_k=8)
    assert out == []


def test_stub_pipeline_query_empty_corpus_ids_returns_empty():
    stub = StubPipeline()
    out = stub.query(prompt="any prompt", corpus_ids=[], top_k=8)
    assert out == []


def test_pipeline_protocol_has_query_method():
    # Structural check: any object with .query(prompt=, corpus_ids=, top_k=)
    # is a Pipeline for retrieval purposes. We assert the StubPipeline
    # satisfies it (runtime check uses isinstance with @runtime_checkable
    # if Pipeline is decorated; otherwise just verify the attribute).
    stub: Pipeline = StubPipeline()
    assert hasattr(stub, "query")
    assert callable(stub.query)


# ---------------------------------------------------------------------------
# QA P2 lock — empty-content chunks must not surface as retrieved sources.
# ---------------------------------------------------------------------------
#
# Earlier `AiarPipeline.query` would emit `QueryResult(content="")` when the
# upstream chunk lacked both `text` and `content` fields. That made the
# Council inspection drawer report populated `source_counts.retrieved_snippet`
# while the actual gateway request carried zero retrieval bytes — a real
# misleading UX. The adapter now skips those chunks and logs a
# `retrieval_adapter_empty_chunk_skipped` warning.


def _fake_chunk(*, text="", chunk_id="", citation_id="", score=None, tokens=None):
    """Minimal dict shape the AIAR adapter coerces from."""
    return {
        "text": text,
        "chunk_id": chunk_id,
        "citation_id": citation_id,
        "score": score,
        "tokens": tokens,
    }


def test_aiar_adapter_skips_empty_content_chunks(monkeypatch):
    """When the upstream chunk has no text/content, the adapter must NOT
    emit a QueryResult — otherwise the inspection drawer reports a
    retrieved_snippet that doesn't exist (QA P2 finding).
    """
    from errorta_judge import aiar_adapter as adapter_mod

    # Bypass __init__ — it imports `aiar` which isn't installed in this test
    # env. The query() method only needs `_invoke_answer_prompt`, which we
    # monkeypatch below.
    pipe = object.__new__(adapter_mod.AiarPipeline)

    # Stub the AIAR call site so the adapter pulls from our fixture chunks.
    def _fake_invoke_answer_prompt(self, prompt, corpus, *, judge=False, judge_model=None):
        return {
            "retrieval": {
                "chunks": [
                    _fake_chunk(text="real content here",
                               chunk_id="ch-real", citation_id="ct-real",
                               score=0.9, tokens=3),
                    _fake_chunk(text="",  # ← empty — must be skipped
                               chunk_id="ch-empty", citation_id="ct-empty",
                               score=0.5, tokens=None),
                ],
            },
        }
    monkeypatch.setattr(
        adapter_mod.AiarPipeline,
        "_invoke_answer_prompt",
        _fake_invoke_answer_prompt,
    )

    out = pipe.query(prompt="q", corpus_ids=["welcome"], top_k=8)
    assert len(out) == 1, (
        f"expected 1 result (empty-content chunk skipped); got {len(out)}: {out}"
    )
    assert out[0].chunk_id == "ch-real"
    assert out[0].content == "real content here"


def test_aiar_adapter_query_strict_raises_on_retrieval_exception(monkeypatch):
    from errorta_judge import aiar_adapter as adapter_mod

    pipe = object.__new__(adapter_mod.AiarPipeline)

    def _raise(self, prompt, corpus, *, judge=False, judge_model=None):
        raise RuntimeError("retrieval transport failed")

    monkeypatch.setattr(adapter_mod.AiarPipeline, "_invoke_answer_prompt", _raise)

    assert pipe.query(prompt="q", corpus_ids=["welcome"], top_k=8) == []
    with pytest.raises(RuntimeError, match="retrieval transport failed"):
        pipe.query_strict(prompt="q", corpus_ids=["welcome"], top_k=8)


def test_aiar_adapter_query_strict_rejects_all_empty_chunks(monkeypatch):
    from errorta_judge import aiar_adapter as adapter_mod

    pipe = object.__new__(adapter_mod.AiarPipeline)

    def _fake_invoke_answer_prompt(self, prompt, corpus, *, judge=False, judge_model=None):
        return {
            "retrieval": {
                "chunks": [
                    _fake_chunk(
                        text="",
                        chunk_id="ch-empty",
                        citation_id="ct-empty",
                        score=0.5,
                    ),
                ],
            },
        }

    monkeypatch.setattr(
        adapter_mod.AiarPipeline,
        "_invoke_answer_prompt",
        _fake_invoke_answer_prompt,
    )

    assert pipe.query(prompt="q", corpus_ids=["welcome"], top_k=8) == []
    with pytest.raises(RuntimeError, match="malformed_chunks"):
        pipe.query_strict(prompt="q", corpus_ids=["welcome"], top_k=8)


def test_remote_retrieval_wrapper_exposes_strict_retrieval(monkeypatch):
    """F009-02 regression: the remote-AIAR retrieval wrapper (returned by
    default_pipeline when a remote retrieval target is configured) must offer
    query_strict, or the Service API silently falls back to best-effort query."""
    from errorta_query import pipeline as pl

    captured: dict = {}

    def _fake_retrieve(*, prompt, corpus_ids, top_k, strict=False):
        captured["strict"] = strict
        return []

    monkeypatch.setattr(
        "errorta_query.aiar_retrieve.remote_aiar_retrieve", _fake_retrieve)
    wrap = pl._RemoteRetrievalPipeline(inner=object())
    assert callable(getattr(wrap, "query_strict", None))
    wrap.query_strict(prompt="p", corpus_ids=["c"], top_k=4)
    assert captured["strict"] is True
