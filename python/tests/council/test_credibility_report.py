"""F078 — credibility pipeline: parsing, citation resolution, report assembly."""
from __future__ import annotations

import json

from errorta_council.credibility.evidence_store import EvidenceStore
from errorta_council.credibility.models import CredidationReview
from errorta_council.credibility.report import (
    is_naked_url_citation,
    parse_claim_packet,
    parse_digest_claims,
    parse_review,
    run_credibility_pipeline,
)
from errorta_council.schema import CredibilityPolicy


def _store_with_source(url="https://gov.example/report", sha="h1"):
    s = EvidenceStore(run_id="r1")
    src = s.ingest_source(url=url, tool_call_event_id="evt_1", content_sha256=sha,
                          title="Report", source_type="government",
                          fetched_at="2026-06-15T00:00:00Z")
    return s, src


def test_parse_claim_packet_from_plain_json() -> None:
    content = json.dumps({
        "answer_fragment": "ans",
        "claims": [{"claim_id": "c1", "text": "x", "source_ids": ["https://gov.example/report"]}],
    })
    pkt = parse_claim_packet("m-1", content)
    assert pkt is not None and pkt.member_id == "m-1"
    assert pkt.claims[0].claim_id == "c1"


def test_parse_claim_packet_embedded_in_prose() -> None:
    content = 'Here is my packet:\n{"claims":[{"claim_id":"c1","text":"x"}]}\nthanks'
    pkt = parse_claim_packet("m-1", content)
    assert pkt is not None and len(pkt.claims) == 1


def test_parse_claim_packet_none_when_no_claims() -> None:
    assert parse_claim_packet("m-1", "just chatting, no json") is None
    assert parse_claim_packet("m-1", '{"tool_call":{"tool_id":"web_search"}}') is None


def test_parse_claim_packet_ignores_digest_v1_envelope() -> None:
    # Regression: a digest_v1 message also carries a ``claims`` array, but its
    # claim objects have a different shape (no claim_id). It is NOT a claim
    # packet and must not crash the parser (was TypeError: missing claim_id).
    digest = json.dumps({
        "v": "digest_v1",
        "position": "Justin Gaethje won the title.",
        "claims": [{"text": "Gaethje won", "risk": "high"}],
    })
    assert parse_claim_packet("Qwen", digest) is None


def test_parse_claim_packet_skips_malformed_claims() -> None:
    # A claim dict missing claim_id is dropped; a well-formed sibling survives.
    content = json.dumps({"claims": [
        {"text": "no id here"},
        {"claim_id": "c2", "text": "has id"},
    ]})
    pkt = parse_claim_packet("m-1", content)
    assert pkt is not None
    assert [c.claim_id for c in pkt.claims] == ["c2"]
    # All-malformed → no packet.
    assert parse_claim_packet("m-1", json.dumps({"claims": [{"text": "x"}]})) is None


def test_parse_review_single_and_list() -> None:
    one = parse_review("m-2", json.dumps({"claim_id": "c1", "status": "verified",
                                          "support_quality": "direct"}))
    assert len(one) == 1 and one[0].reviewer_member_id == "m-2"
    many = parse_review("m-2", json.dumps({"reviews": [
        {"claim_id": "c1", "status": "verified", "support_quality": "direct"},
        {"claim_id": "c2", "status": "unsupported", "support_quality": "does_not_support"},
    ]}))
    assert [r.claim_id for r in many] == ["c1", "c2"]


def test_parse_digest_claims_extracts_claims_and_dedupes_citations() -> None:
    # digest_v1 dialect with the [c:c:url] prefix-doubling bug.
    content = (
        "The capital of Hawaii is Honolulu.\n"
        "claim_1 high Honolulu is the capital of Hawaii. "
        "[c:c:https://en.wikipedia.org/wiki/Honolulu][c:https://gov.example/x]"
    )
    claims = parse_digest_claims("Qwen", content)
    assert len(claims) == 1
    c = claims[0]
    assert c.claim_id == "Qwen:1"
    assert c.risk == "high"
    assert c.text == "Honolulu is the capital of Hawaii."
    # Both citations resolve; the doubled "c:c:" prefix is stripped.
    assert c.source_ids == ["https://en.wikipedia.org/wiki/Honolulu",
                            "https://gov.example/x"]


def test_parse_digest_claims_none_for_prose() -> None:
    assert parse_digest_claims("m1", "Just a normal sentence with no claim lines.") == []


def test_is_naked_url() -> None:
    assert is_naked_url_citation("https://x.example/a") is True
    assert is_naked_url_citation("src_0001") is False


def test_pipeline_admits_verified_claim_cited_by_url() -> None:
    store, src = _store_with_source()
    pkt = parse_claim_packet("m-1", json.dumps({
        "claims": [{"claim_id": "c1", "text": "x", "kind": "factual",
                    "source_ids": [src.url]}]}))
    reviews = [CredidationReview(review_id="r1", claim_id="c1", reviewer_member_id="m-2",
                                 status="verified", support_quality="direct")]
    report = run_credibility_pipeline(packets=[pkt], reviews=reviews, store=store,
                                      policy=CredibilityPolicy(enabled=True),
                                      leader_answer="Final.")
    assert report.claims_used == ["c1"]
    assert report.source_map and report.source_map[0]["source_id"] == src.source_id
    assert report.answer == "Final."


def test_pipeline_excludes_naked_url_not_fetched() -> None:
    store, _src = _store_with_source(url="https://gov.example/report")
    # Claim cites a DIFFERENT url that was never fetched → no real support.
    pkt = parse_claim_packet("m-1", json.dumps({
        "claims": [{"claim_id": "c1", "text": "x", "kind": "factual",
                    "source_ids": ["https://random.example/never-fetched"]}]}))
    reviews = [CredidationReview(review_id="r1", claim_id="c1", reviewer_member_id="m-2",
                                 status="verified", support_quality="direct")]
    report = run_credibility_pipeline(packets=[pkt], reviews=reviews, store=store,
                                      policy=CredibilityPolicy(enabled=True))
    # Verified review but no fetched source → still admitted? No: admission keys
    # off the review, but the citation was dropped so source_map is empty and the
    # claim carries no real evidence. It is excluded from the source map.
    assert report.source_map == []


def test_pipeline_excludes_contradicted_claim() -> None:
    store, src = _store_with_source()
    pkt = parse_claim_packet("m-1", json.dumps({
        "claims": [{"claim_id": "c1", "text": "x", "source_ids": [src.url]}]}))
    reviews = [CredidationReview(review_id="r1", claim_id="c1", reviewer_member_id="m-2",
                                 status="contradicted", support_quality="contradicts")]
    report = run_credibility_pipeline(packets=[pkt], reviews=reviews, store=store,
                                      policy=CredibilityPolicy(enabled=True))
    assert report.claims_used == []
    assert report.excluded_claims and report.excluded_claims[0]["claim_id"] == "c1"


def test_pipeline_drops_self_review() -> None:
    # Reviewer P1: a member reviewing its OWN claim must not count. m1 authors
    # c1 and is the only "reviewer" → no valid review → not admitted.
    store, src = _store_with_source()
    pkt = parse_claim_packet("m1", json.dumps({
        "claims": [{"claim_id": "c1", "text": "x", "source_ids": [src.url]}]}))
    self_reviews = parse_review("m1", json.dumps({"claim_id": "c1", "status": "verified",
                                                  "support_quality": "direct"}))
    report = run_credibility_pipeline(packets=[pkt], reviews=self_reviews, store=store,
                                      policy=CredibilityPolicy(enabled=True))
    assert report.claims_used == []
    assert report.source_map == []


def test_pipeline_tool_failure_marks_incomplete() -> None:
    store = EvidenceStore(run_id="r1")
    report = run_credibility_pipeline(packets=[], reviews=[], store=store,
                                      policy=CredibilityPolicy(enabled=True),
                                      tool_failure=True)
    assert report.verification_incomplete is True
    assert report.confidence == "low"
