"""F096 Slice 0 — contract canaries + FakeAiar self-test.

Two jobs:
1. Prove ``FakeAiar`` honors the frozen contract (docs/handoff/F096-aiar-contract.md)
   so Errorta B-slices can trust it as a stand-in for the real AIAR.
2. Pin the current Errorta drift. The ``xfail(strict=False)`` canaries describe
   the DESIRED end state; each flips to xpass the moment its slice lands (B1/B2/
   B3/H1/H2), giving a visible progress signal without ever breaking the suite.
"""
from __future__ import annotations

import dataclasses

import pytest

from tests.fakes.fake_aiar import FakeAiar


# --------------------------------------------------------------------------- #
# FakeAiar self-test — the seam is trustworthy                                #
# --------------------------------------------------------------------------- #

def _seeded() -> FakeAiar:
    aiar = FakeAiar()
    aiar.seed_corpus("welcome", [
        ("apache.md", "AIAR is published under the Apache-2.0 license."),
        ("phone.md", "Errorta never phones home; everything runs locally."),
    ])
    return aiar


def test_retrieve_returns_ordered_scored_hits_without_generation() -> None:
    aiar = _seeded()
    out = aiar.retrieve_chunks("what license is AIAR under", instance="welcome", k=8)
    assert out["schema_version"] == "aiar.retrieve.v1"
    assert out["count"] >= 1
    # score_kind/score_order are response-level (constant across hits), not per-hit.
    assert out["score_kind"] == "cosine_similarity"
    assert out["score_order"] == "higher_is_better"
    top = out["hits"][0]
    assert top["source"] == "apache.md"
    for key in ("chunk_id", "source", "title", "text", "score",
                "chunk_index", "category", "page_span", "metadata"):
        assert key in top
    assert "score_kind" not in top  # moved to response level
    # ordered by score desc
    scores = [h["score"] for h in out["hits"]]
    assert scores == sorted(scores, reverse=True)


def test_retrieve_unknown_instance_and_empty_query() -> None:
    aiar = _seeded()
    with pytest.raises(KeyError):
        aiar.retrieve_chunks("x", instance="nope")
    with pytest.raises(ValueError):
        aiar.retrieve_chunks("   ", instance="welcome")


def test_retrieve_no_hits_is_success_count_zero() -> None:
    out = _seeded().retrieve_chunks("zzzznomatch", instance="welcome")
    assert out["count"] == 0 and out["hits"] == []


def test_grounding_record_lookup_is_instance_scoped() -> None:
    aiar = FakeAiar()
    aiar.record_grounding(signature="sig1", verdict={"label": "correct"},
                          correction="use X", instance="A", prompt="how to X")
    assert aiar.lookup_grounding(signature="sig1", instance="A") is not None
    # the SAME signature under a different instance must not leak
    assert aiar.lookup_grounding(signature="sig1", instance="B") is None


def test_grounding_semantic_lookup_orders_and_thresholds() -> None:
    aiar = FakeAiar()
    aiar.record_grounding(signature="s", verdict={}, correction="",
                          instance="A", prompt="how do I configure the budget cap")
    near = aiar.lookup_similar_groundings(prompt="configure the budget cap",
                                          instance="A", threshold=0.2)
    far = aiar.lookup_similar_groundings(prompt="totally unrelated text",
                                         instance="A", threshold=0.2)
    assert near and "similarity" in near[0]
    assert far == []


def test_ingest_is_fail_closed_on_publish_and_explicit_publish_works() -> None:
    aiar = FakeAiar()
    # publish defaults to False (fail-closed) — ingested != ready-for-answers.
    unpub = aiar.ingest_documents([{"source": "a.md", "text": "hello world"}],
                                  instance="proj")
    assert unpub["status"] == "done" and unpub["chunks_added"] == 1
    assert unpub["published"] is False
    # explicit publish is a separate, deliberate step
    pub = aiar.ingest_documents([{"source": "c.md", "text": "more"}],
                                instance="proj", publish=True)
    assert pub["published"] is True
    bad = aiar.ingest_documents([{"source": "b.md", "text": "x"}],
                                instance="proj", _fail=True)
    assert bad["status"] == "failed" and bad["errors"]
    assert aiar.health("proj")["last_ingest_error"] == "fake ingest failure"


def test_ingest_partial_success_semantics() -> None:
    # A3 (v0.2.4): three explicit cases B3 must distinguish from "ready".
    # _errors marks the trailing N docs as failed-to-ingest (0 chunks each).
    aiar = FakeAiar()
    # added + some errors -> done WITH non-empty errors (not failed): a.md adds,
    # b.md fails.
    partial = aiar.ingest_documents(
        [{"source": "a.md", "text": "x"}, {"source": "b.md", "text": "y"}],
        instance="p", _errors=["b.md skipped"])
    assert partial["status"] == "done" and partial["chunks_added"] == 1
    assert partial["errors"] == ["b.md skipped"]
    assert partial["accepted"] == 2  # accepted = docs submitted, not reduced by failures
    # all-duplicate re-ingest -> done, 0 added, duplicates>0 (idempotent)
    dup = aiar.ingest_documents([{"source": "a.md", "text": "x"}], instance="p")
    assert dup["status"] == "done" and dup["chunks_added"] == 0 and dup["duplicates"] == 1
    # 0 chunks + errors -> failed (never "ready")
    failed = aiar.ingest_documents([{"source": "c.md", "text": "z"}],
                                   instance="p", _errors=["embedder down"])
    assert failed["status"] == "failed" and failed["chunks_added"] == 0
    # over-supplying errors clamps (can't fail more docs than were submitted)
    clamped = aiar.ingest_documents([{"source": "d.md", "text": "q"}],
                                    instance="p", _errors=["e1", "e2", "e3"])
    assert clamped["status"] == "failed" and clamped["accepted"] == 1


def test_telemetry_keys_schema_and_sources_gating() -> None:
    aiar = _seeded()
    meta = aiar.answer_prompt("license?", instance="welcome")
    assert meta["schema_version"] == "aiar.answer.v1"
    for key in ("call_id", "instance", "model", "grounded", "reground_applied",
                "rag_enabled", "retrieval", "latency"):
        assert key in meta
    # A4: sources attach ONLY when include_sources=True (F001 provenance opt-in).
    assert "sources" not in meta
    with_src = aiar.answer_prompt("license?", instance="welcome", include_sources=True)
    assert with_src["sources"] and with_src["sources"][0]["source"] == "apache.md"
    trace = aiar.get_call(meta["call_id"])
    # trace redacts answer/sources bytes, keeps trace fields + a source count
    assert "answer" not in trace and "sources" not in trace
    assert trace["call_id"] == meta["call_id"] and "source_count" in trace


def test_capability_manifest_shape() -> None:
    man = FakeAiar().capability_manifest()
    assert man["schema_version"] == "aiar.capabilities.v1"
    assert man["schemas"]["retrieve"] == "aiar.retrieve.v1"
    assert "backend_id" in man  # so Errorta can answer "which AIAR is this?"
    assert set(man["features"]) >= {
        "pure_retrieve", "remote_ingest", "grounding_v1", "semantic_grounding"}
    # 0.2.* train: semantic grounding is deferred (A5); Errorta F024 must gate.
    assert man["features"]["semantic_grounding"] is False


def test_semantic_lookup_is_opt_in_capability() -> None:
    # The method exists (so it's testable when A5 lands) but is OFF by default;
    # a test can flip it on to mirror a post-A5 build.
    on = FakeAiar(features={"semantic_grounding": True})
    assert on.capability_manifest()["features"]["semantic_grounding"] is True


# --------------------------------------------------------------------------- #
# Closed-gap assertions — these started as drift canaries; B1/H1 landed, so they #
# are now plain assertions. (RemoteHttpPipeline.query → real AIAR retrieve is    #
# covered hermetically in tests/test_f096_b1_remote_retrieve.py.)                #
# --------------------------------------------------------------------------- #

def test_query_result_exposes_source_provenance() -> None:
    from errorta_query.models import QueryResult
    names = {f.name for f in dataclasses.fields(QueryResult)}
    # B1 added source/title/page_span/metadata for AIAR retrieve provenance.
    assert {"source", "title", "page_span", "metadata"} <= names


def test_query_result_source_is_optional_and_safe() -> None:
    # H1 + B1: a QueryResult built without provenance still has a readable,
    # default `source` (no AttributeError on the project-grounding adapter path).
    from errorta_query.models import QueryResult
    qr = QueryResult(content="t", corpus_id="c", chunk_id="ch",
                     citation_id="ci", score=1.0, tokens=3)
    assert qr.source is None and qr.metadata == {}
