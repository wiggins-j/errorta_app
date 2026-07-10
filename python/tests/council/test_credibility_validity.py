"""F082 slice 2 — argument-validity judge routes inference claims."""
from __future__ import annotations

import json
import pytest

from errorta_council.credibility.admission import compute_admission
from errorta_council.credibility.models import Claim, CredidationReview
from errorta_council.credibility.validity import ArgumentValidityJudge, ValidityResult
from errorta_council.schema import CredibilityPolicy


def _claim():
    return Claim(claim_id="c1", text="Therefore X follows", kind="factual", source_ids=["src_0001"])


def _review():
    return [CredidationReview(review_id="r1", claim_id="c1", reviewer_member_id="m-2",
                             status="verified", support_quality="direct")]


@pytest.mark.asyncio
async def test_validity_judge_parses_verdict_and_caches():
    calls = {"n": 0}
    async def call(_s, _u):
        calls["n"] += 1
        return json.dumps({"verdict": "invalid", "reason": "non sequitur"})
    j = ArgumentValidityJudge(call)
    r1 = await j.assess(claim_text="X", supporting_texts=["A", "B"])
    r2 = await j.assess(claim_text="X", supporting_texts=["B", "A"])  # order-insensitive cache
    assert r1.verdict == "invalid" and calls["n"] == 1 and r2.verdict == "invalid"


@pytest.mark.asyncio
async def test_validity_failclosed_on_garbage():
    async def call(_s, _u):
        return "not json"
    r = await ArgumentValidityJudge(call).assess(claim_text="X", supporting_texts=[])
    assert r.verdict == "unresolved"


def test_admission_inference_invalid_excludes():
    adm = compute_admission(
        claim=_claim(), reviews=_review(),
        policy=CredibilityPolicy(enabled=True, require_entailment=True),
        independence_groups=1, entailment="inference", validity="invalid",
    )
    assert adm.admission == "excluded" and adm.final_status == "inference_invalid"


def test_admission_inference_valid_admits_as_flagged_inference():
    adm = compute_admission(
        claim=_claim(), reviews=_review(),
        policy=CredibilityPolicy(enabled=True, require_entailment=True),
        independence_groups=1, entailment="inference", validity="valid",
    )
    assert adm.admission == "admitted_with_caveat" and adm.disposition == "inference"


def test_admission_inference_unresolved_validity_stays_flagged():
    # Validity judge failed → fail-soft: keep as a flagged inference, not excluded.
    adm = compute_admission(
        claim=_claim(), reviews=_review(),
        policy=CredibilityPolicy(enabled=True, require_entailment=True),
        independence_groups=1, entailment="inference", validity="unresolved",
    )
    assert adm.admission == "admitted_with_caveat" and adm.disposition == "inference"
