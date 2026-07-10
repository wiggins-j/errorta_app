"""F082 slice 3 — free-prose citation extraction + finalizer audit."""
from __future__ import annotations

from errorta_council.credibility.entailment import extract_prose_citations
from errorta_council.credibility.report import CredibilityReport, run_credibility_pipeline
from errorta_council.credibility.evidence_store import EvidenceStore
from errorta_council.schema import CredibilityPolicy


def test_extract_prose_citations_finds_source_sentences():
    text = ("The IEP says the hard problem remains after functional explanation "
            "(iep.utm.edu). I think this is overstated. Another point with no source here.")
    cites = extract_prose_citations(text, ["https://iep.utm.edu/hard-problem/"])
    assert len(cites) == 1
    sent, url = cites[0]
    assert "hard problem remains" in sent and url == "https://iep.utm.edu/hard-problem/"


def test_extract_prose_citations_skips_short_and_unsourced():
    assert extract_prose_citations("No source. Tiny.", ["https://x.example/a"]) == []
    assert extract_prose_citations("", ["https://x.example/a"]) == []


def test_extract_prose_matches_full_url():
    text = "As stated at https://andrewmbailey.com/ap/Against_Materialism.pdf the case is made."
    cites = extract_prose_citations(text, ["https://andrewmbailey.com/ap/Against_Materialism.pdf"])
    assert len(cites) == 1


def test_report_carries_finalizer_citation_failures():
    store = EvidenceStore(run_id="r1")
    report = run_credibility_pipeline(
        packets=[], reviews=[], store=store,
        policy=CredibilityPolicy(enabled=True),
        finalizer_citation_failures=[{"claim_id": "GPT:c5", "reason": "contradicts"}],
    )
    assert report.finalizer_citation_failures == [{"claim_id": "GPT:c5", "reason": "contradicts"}]
    assert report.to_dict()["finalizer_citation_failures"][0]["claim_id"] == "GPT:c5"
