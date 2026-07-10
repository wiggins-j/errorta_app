"""F085 — provenance tier tag for sources."""
from __future__ import annotations

from errorta_council.credibility import (
    run_credibility_pipeline,
    source_tier,
    source_tier_label,
)
from errorta_council.credibility.evidence_store import EvidenceStore
from errorta_council.credibility.models import (
    Claim,
    ClaimPacket,
    CredidationReview,
    Source,
)
from errorta_council.schema import CredibilityPolicy
from errorta_council.scheduler import _format_credibility_answer


def test_tier_rollup():
    assert source_tier("government") == "primary"
    assert source_tier("peer_reviewed_paper") == "primary"
    assert source_tier("reputable_news") == "reporting"
    assert source_tier("blog") == "opinion"
    assert source_tier("forum") == "opinion"
    assert source_tier("unknown") == "unknown"
    assert source_tier("nonsense") == "unknown"
    assert source_tier(None) == "unknown"
    assert source_tier_label("blog") == "opinion"
    assert source_tier_label("government") == "primary"
    assert source_tier_label("unknown") == "unverified"


def _report_with_source(source_type: str):
    store = EvidenceStore(run_id="r1")
    store.sources["s1"] = Source(
        source_id="s1", url="https://example.test/x", canonical_url="https://example.test/x",
        title="X", source_type=source_type, fetched_at="2026-01-01T00:00:00Z",
    )
    pkt = ClaimPacket(packet_id="p", member_id="m1", claims=[
        Claim(claim_id="m1:c1", text="A documented fact.", kind="factual",
              source_ids=["https://example.test/x"])])
    review = CredidationReview(review_id="r", reviewer_member_id="m2", claim_id="m1:c1",
                               status="verified", support_quality="direct", reason="ok")
    return run_credibility_pipeline(
        packets=[pkt], reviews=[review], store=store,
        policy=CredibilityPolicy.from_dict({}), leader_answer="ans")


def test_source_map_carries_tier():
    rep = _report_with_source("blog")
    assert rep.source_map[0]["tier"] == "opinion"
    assert rep.source_map[0]["tier_label"] == "opinion"
    d = rep.to_dict()
    assert d["source_map"][0]["tier"] == "opinion"


def test_format_answer_tags_opinion_and_shows_legend():
    text = _format_credibility_answer(_report_with_source("blog"))
    assert "· opinion" in text
    assert "individual viewpoint" in text  # legend present


def test_format_answer_primary_source_no_legend():
    text = _format_credibility_answer(_report_with_source("government"))
    assert "· primary" in text
    assert "individual viewpoint" not in text  # no legend when nothing soft
