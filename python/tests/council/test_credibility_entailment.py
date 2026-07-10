"""F081 slice 1 — the entailment gate."""
from __future__ import annotations

import asyncio

import pytest

from errorta_council.credibility.admission import compute_admission
from errorta_council.credibility.entailment import (
    EntailmentResult,
    GatewayEntailmentVerifier,
    aggregate_grades,
    select_candidate_spans,
)
from errorta_council.credibility.models import Claim, CredidationReview
from errorta_council.credibility.report import run_credibility_pipeline
from errorta_council.credibility.evidence_store import EvidenceStore
from errorta_council.schema import CredibilityPolicy


# ---- verifier --------------------------------------------------------------

def _verifier(reply: str):
    calls = {"n": 0}

    async def _call(system, user):
        calls["n"] += 1
        return reply

    return GatewayEntailmentVerifier(_call), calls


def test_select_candidate_spans_picks_relevant_window():
    src = ("Filler about weather. " * 40) + " The capital of Alabama is Montgomery. " + ("More filler. " * 40)
    spans = select_candidate_spans(src, "What is the capital of Alabama?", k=1, window=120)
    assert spans and "Montgomery" in " ".join(spans)


@pytest.mark.asyncio
async def test_verifier_grades_and_caches():
    v, calls = _verifier('{"grade":"entails","span":"X is Y.","reason":"states it"}')
    r1 = await v.verify(claim_text="X is Y", source_text="X is Y. more", source_sha256="s1")
    assert r1.grade == "entails" and r1.supports and r1.span_sha256
    # second identical call is served from cache (no extra model call)
    r2 = await v.verify(claim_text="X is Y", source_text="X is Y. more", source_sha256="s1")
    assert r2.grade == "entails" and calls["n"] == 1


@pytest.mark.asyncio
async def test_verifier_failclosed_on_garbage():
    v, _ = _verifier("not json at all")
    r = await v.verify(claim_text="c", source_text="s", source_sha256="s1")
    assert r.grade == "unresolved" and not r.supports


def test_aggregate_grades_multi_source():
    assert aggregate_grades(["entails", "unsupported"]) == "entails"
    assert aggregate_grades(["entails", "contradicts"]) == "contradicts"  # contradiction poisons
    assert aggregate_grades(["partially_entails", "unsupported"]) == "partially_entails"
    assert aggregate_grades(["unsupported"]) == "unsupported"
    assert aggregate_grades([]) == "unresolved"


# ---- admission composition -------------------------------------------------

def _claim():
    return Claim(claim_id="c1", text="x", kind="factual", source_ids=["src_0001"])


def _verified_review():
    return [CredidationReview(review_id="r1", claim_id="c1", reviewer_member_id="m-2",
                              status="verified", support_quality="direct")]


def test_admission_contradicted_source_excludes_even_with_verified_review():
    adm = compute_admission(
        claim=_claim(), reviews=_verified_review(),
        policy=CredibilityPolicy(enabled=True, require_entailment=True),
        independence_groups=1, entailment="contradicts",
    )
    assert adm.admission == "excluded" and adm.final_status == "entailment_contradicted"


def test_admission_unsupported_source_caveats_not_excludes():
    # The verifier is fallible: a weak "unsupported" only DOWNGRADES to a caveat,
    # never a hard exclude (reserve that for a clear contradiction).
    adm = compute_admission(
        claim=_claim(), reviews=_verified_review(),
        policy=CredibilityPolicy(enabled=True, require_entailment=True),
        independence_groups=1, entailment="unsupported",
    )
    assert adm.admission == "admitted_with_caveat"


def test_admission_entails_plus_review_admits():
    adm = compute_admission(
        claim=_claim(), reviews=_verified_review(),
        policy=CredibilityPolicy(enabled=True, require_entailment=True),
        independence_groups=1, entailment="entails",
    )
    assert adm.admission == "admitted"


def test_admission_partial_entailment_caveats():
    adm = compute_admission(
        claim=_claim(), reviews=_verified_review(),
        policy=CredibilityPolicy(enabled=True, require_entailment=True),
        independence_groups=1, entailment="partially_entails",
    )
    assert adm.admission == "admitted_with_caveat"


def test_admission_entails_still_needs_peer_review():
    # Entailment is an AND-gate, not a substitute: no review → not admitted.
    adm = compute_admission(
        claim=_claim(), reviews=[],
        policy=CredibilityPolicy(enabled=True, require_entailment=True),
        independence_groups=1, repair_exhausted=True, entailment="entails",
    )
    assert adm.admission == "excluded" and adm.final_status == "unreviewed"


def test_admission_unresolved_does_not_exclude_a_reviewed_claim():
    # A verifier failure (unresolved) must NOT punish a peer-reviewed claim —
    # it falls through to the normal review gate (this is the bug that nuked a
    # whole well-cited debate when the verifier kept timing out).
    adm = compute_admission(
        claim=_claim(), reviews=_verified_review(),
        policy=CredibilityPolicy(enabled=True, require_entailment=True),
        independence_groups=1, entailment="unresolved",
    )
    assert adm.admission == "admitted"


def test_admission_lenient_ignores_missing_entailment():
    # require_entailment off → today's behavior (admitted on verified review).
    adm = compute_admission(
        claim=_claim(), reviews=_verified_review(),
        policy=CredibilityPolicy(enabled=True, require_entailment=False),
        independence_groups=1, entailment=None,
    )
    assert adm.admission == "admitted"


# ---- pipeline threading ----------------------------------------------------

def _store_with_source(url="https://gov.example/report", sha="h1"):
    s = EvidenceStore(run_id="r1")
    src = s.ingest_source(url=url, tool_call_event_id="e1", content_sha256=sha,
                          title="R", source_type="government", fetched_at="t")
    return s, src


def test_pipeline_excludes_non_entailed_claim():
    store, src = _store_with_source()
    pkt_content = '{"claims":[{"claim_id":"c1","text":"x","source_ids":["%s"]}]}' % src.url
    from errorta_council.credibility.report import parse_claim_packet
    pkt = parse_claim_packet("m-1", pkt_content)
    reviews = [CredidationReview(review_id="r1", claim_id="c1", reviewer_member_id="m-2",
                                 status="verified", support_quality="direct")]
    report = run_credibility_pipeline(
        packets=[pkt], reviews=reviews, store=store,
        policy=CredibilityPolicy(enabled=True, require_entailment=True),
        entailment_by_claim={"c1": "contradicts"},
    )
    assert report.claims_used == []
    assert report.excluded_claims and report.excluded_claims[0]["reason"] == "entailment_contradicted"


# ---- F082 slice 1: actionable caveat fork --------------------------------

def test_aggregate_grades_includes_new_grades():
    from errorta_council.credibility.entailment import aggregate_grades
    assert aggregate_grades(["overclaim", "unsupported"]) == "overclaim"
    assert aggregate_grades(["inference", "unsupported"]) == "inference"
    assert aggregate_grades(["entails", "overclaim"]) == "entails"
    assert aggregate_grades(["contradicts", "overclaim"]) == "contradicts"


@pytest.mark.asyncio
async def test_verifier_parses_overclaim_with_revised_text():
    import json
    from errorta_council.credibility.entailment import GatewayEntailmentVerifier

    async def call(_s, _u):
        return json.dumps({"grade": "overclaim", "span": "X is difficult.",
                           "revised_text": "X is difficult.", "reason": "weaker"})
    res = await GatewayEntailmentVerifier(call).verify(
        claim_text="X is impossible", source_text="X is difficult.", source_sha256="s")
    assert res.grade == "overclaim" and res.revised_text == "X is difficult."
    assert res.is_caveat and res.supports


def test_admission_overclaim_disposition_revised():
    adm = compute_admission(
        claim=_claim(), reviews=_verified_review(),
        policy=CredibilityPolicy(enabled=True, require_entailment=True),
        independence_groups=1, entailment="overclaim", revised_text="X is difficult.",
    )
    assert adm.admission == "admitted_with_caveat"
    assert adm.disposition == "revised" and adm.revised_text == "X is difficult."


def test_admission_inference_disposition():
    adm = compute_admission(
        claim=_claim(), reviews=_verified_review(),
        policy=CredibilityPolicy(enabled=True, require_entailment=True),
        independence_groups=1, entailment="inference",
    )
    assert adm.admission == "admitted_with_caveat" and adm.disposition == "inference"


def test_admission_entails_disposition_sourced():
    adm = compute_admission(
        claim=_claim(), reviews=_verified_review(),
        policy=CredibilityPolicy(enabled=True, require_entailment=True),
        independence_groups=1, entailment="entails",
    )
    assert adm.admission == "admitted" and adm.disposition == "sourced"


def test_pipeline_revises_down_and_reports_disposition():
    store, src = _store_with_source()
    from errorta_council.credibility.report import parse_claim_packet
    pkt = parse_claim_packet("m-1", '{"claims":[{"claim_id":"c1","text":"X is impossible","source_ids":["%s"]}]}' % src.url)
    reviews = [CredidationReview(review_id="r1", claim_id="c1", reviewer_member_id="m-2",
                                 status="verified", support_quality="direct")]
    report = run_credibility_pipeline(
        packets=[pkt], reviews=reviews, store=store,
        policy=CredibilityPolicy(enabled=True, require_entailment=True),
        entailment_by_claim={"c1": "overclaim"},
        revised_text_by_claim={"c1": "X is difficult."},
    )
    assert "c1" in report.claims_used
    disp = {d["claim_id"]: d for d in report.dispositions}
    assert disp["c1"]["disposition"] == "revised"
    assert disp["c1"]["text"] == "X is difficult."  # report cites the narrowed claim
    # an overclaim is NOT a bare caveat → caveat_rate stays 0
    assert report.caveat_rate == 0.0
