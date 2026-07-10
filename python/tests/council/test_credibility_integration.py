"""F078 — end-to-end finalizer logic: fetched-source capture events → admitted
report. Exercises the same path _maybe_synthesize_credibility_report runs:
build sources from CREDIBILITY_SOURCE_CAPTURED events, parse transcript
packets/reviews, run the admission pipeline.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from errorta_council.credibility import parse_claim_packet, parse_review
from errorta_council.credibility.report import run_credibility_pipeline
from errorta_council.scheduler import (
    _credibility_source_type,
    _format_credibility_answer,
    build_credibility_sources,
)
from errorta_council.schema import CredibilityPolicy, EventType


def _capture(url, sha="h", call="evt"):
    return SimpleNamespace(
        type=EventType.CREDIBILITY_SOURCE_CAPTURED,
        payload={"url": url, "content_sha256": sha, "tool_call_event_id": call,
                 "fetched_at": "2026-06-15T00:00:00Z"},
    )


def test_source_type_heuristic() -> None:
    assert _credibility_source_type("https://www.nasa.gov/report") == "government"
    assert _credibility_source_type("https://arxiv.org/abs/1") == "official"
    assert _credibility_source_type("https://mit.edu/paper") == "peer_reviewed_paper"
    assert _credibility_source_type("https://randomblog.example/x") == "unknown"


def test_build_sources_from_capture_events() -> None:
    events = [
        _capture("https://standards.gov/spec", sha="a", call="e1"),
        SimpleNamespace(type=EventType.MEMBER_MESSAGE, payload={"content": "noise"}),
        _capture("https://standards.gov/spec2", sha="b", call="e2"),
    ]
    store = build_credibility_sources("run-1", events)
    assert len(store.sources) == 2
    types = {s.source_type for s in store.sources.values()}
    assert types == {"government"}


def test_build_sources_tolerates_malformed_events() -> None:
    # Fail-closed support: the source builder must not raise on junk/partial
    # events (the finalizer relies on this to degrade to an incomplete report).
    events = [
        SimpleNamespace(type=EventType.CREDIBILITY_SOURCE_CAPTURED, payload={}),  # no url
        SimpleNamespace(type=EventType.CREDIBILITY_SOURCE_CAPTURED, payload=None),
        SimpleNamespace(type=EventType.MEMBER_MESSAGE, payload={"content": "{bad json"}),
        _capture("https://ok.gov/x"),
    ]
    store = build_credibility_sources("run-x", events)
    assert len(store.sources) == 1  # only the well-formed capture minted


def test_end_to_end_admits_verified_excludes_unfetched() -> None:
    events = [_capture("https://standards.gov/spec", sha="a", call="e1")]
    store = build_credibility_sources("run-1", events)
    src = next(iter(store.sources.values()))

    pkt1 = parse_claim_packet("m1", json.dumps({"claims": [
        {"claim_id": "c1", "text": "LRU evicts LRU entries.", "kind": "factual",
         "source_ids": ["https://standards.gov/spec"]}]}))
    pkt2 = parse_claim_packet("m2", json.dumps({"claims": [
        {"claim_id": "c2", "text": "Made up.", "kind": "factual",
         "source_ids": ["https://never-fetched.example/x"]}]}))
    reviews = parse_review("m2", json.dumps({"claim_id": "c1", "status": "verified",
                                             "support_quality": "direct"}))
    reviews += parse_review("m1", json.dumps({"claim_id": "c2", "status": "verified",
                                              "support_quality": "direct"}))

    report = run_credibility_pipeline(
        packets=[pkt1, pkt2], reviews=reviews, store=store,
        policy=CredibilityPolicy(enabled=True),
    )
    # c1 admitted (fetched + verified). c2 cited a URL never captured → dropped,
    # so it contributes no source to the map (marquee guarantee).
    assert "c1" in report.claims_used
    assert report.source_map and report.source_map[0]["source_id"] == src.source_id
    assert all(s["url"] != "https://never-fetched.example/x" for s in report.source_map)

    answer = _format_credibility_answer(report)
    assert "verified claim" in answer.lower()
    assert "Confidence:" in answer
