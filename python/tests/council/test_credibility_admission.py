"""F078 Slice 4 (core) — claim-admission rule matrix."""
from __future__ import annotations

from errorta_council.credibility.admission import compute_admission
from errorta_council.credibility.models import Claim, CredidationReview
from errorta_council.schema import CredibilityPolicy


def _claim(**kw) -> Claim:
    base = dict(claim_id="c1", text="x", kind="factual", risk="normal",
                source_ids=["src_0001"])
    base.update(kw)
    return Claim(**base)


def _review(status="verified", support="direct", **kw) -> CredidationReview:
    base = dict(review_id="r", claim_id="c1", reviewer_member_id="m-2",
                status=status, support_quality=support)
    base.update(kw)
    return CredidationReview(**base)


def _adm(claim, reviews, *, groups=1, exhausted=False, **pol):
    return compute_admission(
        claim=claim, reviews=reviews, policy=CredibilityPolicy(**pol),
        independence_groups=groups, repair_exhausted=exhausted,
    )


def test_verified_direct_is_admitted() -> None:
    a = _adm(_claim(), [_review()])
    assert a.admission == "admitted"
    assert a.final_status == "verified"


def test_uncited_observation_excluded() -> None:
    a = _adm(_claim(kind="uncited_observation", source_ids=[]), [])
    assert a.admission == "excluded"


def test_no_reviews_repair_then_excluded() -> None:
    assert _adm(_claim(), []).admission == "repair_required"
    assert _adm(_claim(), [], exhausted=True).admission == "excluded"


def test_contradicted_excluded() -> None:
    a = _adm(_claim(), [_review(status="contradicted", support="contradicts")])
    assert a.admission == "excluded"
    assert a.final_status == "contradicted"


def test_indirect_support_gets_caveat() -> None:
    a = _adm(_claim(), [_review(support="indirect")])
    assert a.admission == "admitted_with_caveat"


def test_partial_only_repairs_then_caveat_when_exhausted() -> None:
    assert _adm(_claim(), [_review(status="partially_supported", support="indirect")]).admission == "repair_required"
    a = _adm(_claim(), [_review(status="partially_supported", support="indirect")], exhausted=True)
    assert a.admission == "admitted_with_caveat"


def test_unsupported_only_repairs_then_excluded() -> None:
    assert _adm(_claim(), [_review(status="unsupported", support="does_not_support")]).admission == "repair_required"
    assert _adm(_claim(), [_review(status="unsupported", support="does_not_support")], exhausted=True).admission == "excluded"


def test_key_claim_needs_two_reviewers_in_strict() -> None:
    claim = _claim(risk="high")  # high ⇒ key
    one = _adm(claim, [_review()], strictness="strict",
               min_independent_sources_per_high_risk_claim=1)
    assert one.admission == "repair_required"
    assert "second_reviewer_for_key_claim" in one.required_repairs
    two = _adm(claim, [_review(review_id="r1", reviewer_member_id="m-2"),
                       _review(review_id="r2", reviewer_member_id="m-3")],
               strictness="strict", min_independent_sources_per_high_risk_claim=1)
    assert two.admission == "admitted"


def test_high_risk_strict_needs_independent_sources() -> None:
    claim = _claim(risk="high", source_ids=["src_0001", "src_0002"])
    reviews = [_review(review_id="r1", reviewer_member_id="m-2"),
               _review(review_id="r2", reviewer_member_id="m-3")]
    # Only 1 independent group → unmet.
    a = _adm(claim, reviews, groups=1, strictness="strict",
             min_independent_sources_per_high_risk_claim=2)
    assert a.admission == "repair_required"
    assert "more_independent_sources" in a.required_repairs
    # 2 independent groups → admitted.
    b = _adm(claim, reviews, groups=2, strictness="strict",
             min_independent_sources_per_high_risk_claim=2)
    assert b.admission == "admitted"


def test_normal_claim_one_reviewer_ok() -> None:
    a = _adm(_claim(), [_review()], strictness="normal")
    assert a.admission == "admitted"


def test_factual_claim_with_no_fetched_source_never_admitted() -> None:
    # Reviewer P1: a verified review must NOT admit a claim whose citations were
    # all dropped (unfetched). source_ids=[] models "nothing fetched resolved".
    claim = _claim(source_ids=[])
    assert _adm(claim, [_review()]).admission == "repair_required"
    assert _adm(claim, [_review()]).final_status == "no_fetched_source"
    assert _adm(claim, [_review()], exhausted=True).admission == "excluded"


def test_two_reviews_from_same_member_is_not_two_reviewers() -> None:
    # Reviewer P1: one member submitting two reviews must not satisfy the
    # strict two-reviewer rule for a key claim.
    claim = _claim(risk="high")  # key
    same = [
        _review(review_id="r1", reviewer_member_id="m-2"),
        _review(review_id="r2", reviewer_member_id="m-2"),
    ]
    a = _adm(claim, same, strictness="strict", min_independent_sources_per_high_risk_claim=1)
    assert a.admission == "repair_required"
    assert "second_reviewer_for_key_claim" in a.required_repairs
    # Two DISTINCT reviewers → admitted.
    distinct = [
        _review(review_id="r1", reviewer_member_id="m-2"),
        _review(review_id="r2", reviewer_member_id="m-3"),
    ]
    b = _adm(claim, distinct, strictness="strict", min_independent_sources_per_high_risk_claim=1)
    assert b.admission == "admitted"


def test_review_event_ids_carried() -> None:
    a = compute_admission(claim=_claim(), reviews=[_review()],
                          policy=CredibilityPolicy(), independence_groups=1,
                          review_event_ids=["evt_1", "evt_2"])
    assert a.review_event_ids == ["evt_1", "evt_2"]
