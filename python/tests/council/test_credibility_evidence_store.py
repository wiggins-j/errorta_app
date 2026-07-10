"""F078 Slice 2 — evidence store: minting, independence grouping, replay."""
from __future__ import annotations

from errorta_council.credibility import (
    Claim,
    ClaimAdmission,
    ClaimPacket,
    CredidationReview,
    EvidenceSpan,
    EvidenceStore,
    is_key_claim,
)
from errorta_council.credibility.evidence_store import registrable_domain


def test_ingest_mints_sequential_ids() -> None:
    s = EvidenceStore(run_id="r1")
    a = s.ingest_source(url="https://a.example/x", tool_call_event_id="evt_1",
                        content_sha256="aaa")
    b = s.ingest_source(url="https://b.example/y", tool_call_event_id="evt_2",
                        content_sha256="bbb")
    assert a.source_id == "src_0001"
    assert b.source_id == "src_0002"
    # Different sites/hashes → different independence groups.
    assert a.independence_group_id != b.independence_group_id
    assert s.independence_group_count([a.source_id, b.source_id]) == 2


def test_identical_canonical_url_shares_group() -> None:
    s = EvidenceStore(run_id="r1")
    a = s.ingest_source(url="https://a.example/x?utm=1", canonical_url="https://a.example/x",
                        tool_call_event_id="e1", content_sha256="h1")
    b = s.ingest_source(url="https://a.example/x?ref=2", canonical_url="https://a.example/x",
                        tool_call_event_id="e2", content_sha256="h2")
    assert a.independence_group_id == b.independence_group_id
    assert s.independence_group_count([a.source_id, b.source_id]) == 1


def test_identical_content_hash_shares_group() -> None:
    s = EvidenceStore(run_id="r1")
    a = s.ingest_source(url="https://wire.example/a", tool_call_event_id="e1",
                        content_sha256="SAME")
    b = s.ingest_source(url="https://reprint.example/b", tool_call_event_id="e2",
                        content_sha256="SAME")  # syndicated copy
    assert a.independence_group_id == b.independence_group_id


def test_same_domain_and_author_shares_group() -> None:
    s = EvidenceStore(run_id="r1")
    a = s.ingest_source(url="https://news.example/1", tool_call_event_id="e1",
                        content_sha256="h1", author="Jane Doe")
    b = s.ingest_source(url="https://news.example/2", tool_call_event_id="e2",
                        content_sha256="h2", author="Jane Doe")
    assert a.independence_group_id == b.independence_group_id
    # Different author on same domain → independent.
    c = s.ingest_source(url="https://news.example/3", tool_call_event_id="e3",
                        content_sha256="h3", author="John Roe")
    assert c.independence_group_id != a.independence_group_id


def test_registrable_domain_handles_multi_tld() -> None:
    assert registrable_domain("https://www.bbc.co.uk/news") == "bbc.co.uk"
    assert registrable_domain("https://sub.example.com/x") == "example.com"
    assert registrable_domain("not a url") == "not a url" or registrable_domain("") == ""


def test_key_claim_floor() -> None:
    assert is_key_claim(key=False, risk="high") is True
    assert is_key_claim(key=False, risk="time_sensitive") is True
    assert is_key_claim(key=False, risk="normal") is False
    assert is_key_claim(key=True, risk="normal") is True


def test_full_round_trip_replay() -> None:
    s = EvidenceStore(run_id="r1")
    src = s.ingest_source(url="https://a.example/x", tool_call_event_id="e1",
                          content_sha256="h1", title="Report")
    s.add_span(EvidenceSpan(span_ref=f"{src.source_id}#span_1", source_id=src.source_id,
                            text_sha256="t1", char_start=0, char_end=20, excerpt="hi"))
    s.add_packet(ClaimPacket(packet_id="pkt_1", member_id="m-1", answer_fragment="ans",
                             claims=[Claim(claim_id="c1", text="x", risk="high",
                                           source_ids=[src.source_id])]))
    s.add_review(CredidationReview(review_id="rev_1", claim_id="c1",
                                   reviewer_member_id="m-2", status="verified",
                                   support_quality="direct"))
    s.set_admission(ClaimAdmission(claim_id="c1", admission="admitted",
                                   final_status="verified"))

    restored = EvidenceStore.from_dict(s.to_dict())
    assert restored.run_id == "r1"
    assert restored.sources[src.source_id].title == "Report"
    assert restored.get_claim("c1").is_key is True
    assert [r.status for r in restored.reviews_for("c1")] == ["verified"]
    assert restored.admissions["c1"].admission == "admitted"
    # Minting continues from the restored sequence (no id collision).
    nxt = restored.ingest_source(url="https://b.example/y", tool_call_event_id="e9")
    assert nxt.source_id == "src_0002"
