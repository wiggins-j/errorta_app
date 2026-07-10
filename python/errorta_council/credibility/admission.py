"""F078 claim-admission logic (pure, deterministic).

Given a claim, its peer credidation reviews, the room policy, and how many
independent source groups back it, decide whether the claim may be cited in the
final answer. This is the gate that stops citation theater: a claim is admitted
only with a verifying non-author review, and key/high-risk claims face stricter
source + reviewer requirements. No I/O, no events — the scheduler records the
returned ClaimAdmission.
"""
from __future__ import annotations

from .models import Claim, ClaimAdmission, CredidationReview, is_key_claim

# Review statuses that verify support.
_VERIFYING = {"verified"}
# Support qualities that count as real support for a "verified" status.
_SUPPORTING_QUALITY = {"direct", "indirect"}
# Statuses/qualities that hard-exclude (the source argues against the claim).
_CONTRADICTING_STATUS = {"contradicted"}
# Statuses that are repairable failures (not outright contradictions).
_WEAK_STATUS = {
    "unsupported", "source_unreachable", "source_unreliable", "stale",
    "needs_primary_source", "not_reviewable",
}
# Qualities that warrant a caveat even on an otherwise-supporting review.
_CAVEAT_QUALITY = {"indirect", "inferential"}


def compute_admission(
    *,
    claim: Claim,
    reviews: list[CredidationReview],
    policy,  # CredibilityPolicy (avoid import cycle; duck-typed)
    independence_groups: int,
    review_event_ids: list[str] | None = None,
    repair_exhausted: bool = False,
    entailment: str | None = None,
    revised_text: str = "",
    validity: str | None = None,
) -> ClaimAdmission:
    """Map a claim's reviews to a ClaimAdmission.

    ``independence_groups`` is the count of DISTINCT independence groups among
    the claim's sources (from EvidenceStore.independence_group_count).
    ``repair_exhausted`` is True on the final pass: a still-unresolved claim can
    no longer ask for repair, so it settles to admitted_with_caveat (if any
    verifying review exists) or excluded.

    ``entailment`` (F081) is the verifier's grade for whether the cited fetched
    source actually supports the claim — one of ``entails | partially_entails |
    unsupported | contradicts | unresolved``, or None when the gate didn't run.
    It is an ADDITIONAL AND-gate: a claim must clear entailment AND mandatory
    peer review. A contradicted source excludes outright; under
    ``require_entailment`` an unsupported/unresolved grade fails the claim
    closed (never silently admitted). Entailment never substitutes for peer
    review.
    """
    ev_ids = list(review_event_ids or [])

    def _disposition(admission: str) -> tuple[str, str]:
        # F082: the actionable disposition the report renders instead of a bare
        # caveat. Primary signal is the entailment grade.
        if admission in ("repair_required",):
            return "", ""
        if admission == "excluded":
            return "excluded", ""
        if entailment == "overclaim":
            return "revised", revised_text
        if entailment == "inference":
            return "inference", ""
        if admission == "admitted_with_caveat":
            return "indirect", ""
        return "sourced", ""

    def _result(admission: str, final_status: str, repairs: list[str] | None = None) -> ClaimAdmission:
        disp, rev = _disposition(admission)
        return ClaimAdmission(
            claim_id=claim.claim_id, admission=admission, final_status=final_status,
            required_repairs=repairs or [], review_event_ids=ev_ids,
            disposition=disp, revised_text=rev,
        )

    # An uncited observation is never a factual citation basis.
    if claim.kind == "uncited_observation":
        return _result("excluded", "uncited_observation")

    # Marquee guarantee: a factual claim must rest on at least one FETCHED
    # source. Upstream resolution drops citations that were never fetched, so
    # empty source_ids here means "no real evidence" — never admit, regardless
    # of how the review reads. (Reviewer P1: a verified review must not be able
    # to admit a claim whose only citation was an unfetched URL.)
    if not claim.source_ids:
        if repair_exhausted:
            return _result("excluded", "no_fetched_source")
        return _result("repair_required", "no_fetched_source", ["needs_fetched_source"])

    # F081 entailment gate — runs BEFORE peer review. The cited source must
    # actually support the claim; this is the code-enforced version of "does
    # this source say what the member claims it says."
    # The verifier is a fallible LLM, so the gate's REJECTION power is reserved
    # for the one unambiguous signal — the source argues the OPPOSITE. A weak
    # "unsupported" only downgrades to a caveat, and an "unresolved" (the
    # verifier failed or couldn't decide) has NO effect: a tool failure must
    # never exclude a peer-reviewed claim. (Earlier this hard-excluded on
    # unresolved, which let a flaky verifier nuke an entire well-cited debate.)
    entailment_caveat = False
    if entailment == "contradicts":
        # The cited source argues the opposite — exclude regardless of reviews.
        return _result("excluded", "entailment_contradicted")
    if entailment == "inference" and validity == "invalid":
        # F082: the source is silent AND the validity judge ruled the inference
        # an unsupported leap — exclude. (valid / unresolved → flagged inference.)
        return _result("excluded", "inference_invalid")
    if entailment in ("unsupported", "partially_entails", "overclaim", "inference"):
        # F082: overclaim (source supports a weaker version → revise-down) and
        # inference (source silent → route to validity) admit-with-caveat with a
        # distinct DISPOSITION (set in _disposition) — the report renders the
        # actionable outcome, not a uniform asterisk.
        entailment_caveat = True
    # entailment in (None, "unresolved") → no effect; fall through to peer review.

    # No non-author review yet -> must be reviewed before it can be admitted.
    # A fetched source alone is not enough for Credibility mode because the
    # feature's contract requires peer credidation.
    if not reviews:
        if not repair_exhausted:
            return _result("repair_required", "unreviewed", ["needs_review"])
        return _result("excluded", "unreviewed")

    # Any reviewer who found the source argues against the claim kills it.
    if any(r.status in _CONTRADICTING_STATUS or r.support_quality == "contradicts"
           for r in reviews):
        return _result("excluded", "contradicted")

    verifying = [
        r for r in reviews
        if r.status in _VERIFYING and r.support_quality in _SUPPORTING_QUALITY
    ]
    partial = [r for r in reviews if r.status == "partially_supported"]
    weak = [r for r in reviews if r.status in _WEAK_STATUS]
    caveat_needed = (
        bool(partial)
        or entailment_caveat  # F081: a partially-entailing source admits with caveat
        or any(r.support_quality in _CAVEAT_QUALITY for r in verifying)
    )

    key = is_key_claim(key=claim.key, risk=claim.risk)
    high_risk = claim.risk in {"high", "time_sensitive"}
    strict = getattr(policy, "strictness", "normal") == "strict"

    # Requirement checks (only meaningful once we have at least one verifier).
    unmet: list[str] = []
    if key and len(claim.source_ids) < int(getattr(policy, "min_sources_per_key_claim", 1)):
        unmet.append("more_sources_for_key_claim")
    need_two = strict or bool(getattr(policy, "require_two_reviewers_for_key_claims", False))
    # Count DISTINCT reviewers, not review objects — one member submitting two
    # reviews must not satisfy "two reviewers" (Reviewer P1).
    distinct_verifiers = {r.reviewer_member_id for r in verifying}
    if key and need_two and len(distinct_verifiers) < 2:
        unmet.append("second_reviewer_for_key_claim")
    if high_risk and strict:
        need_indep = int(getattr(policy, "min_independent_sources_per_high_risk_claim", 2))
        if independence_groups < need_indep:
            unmet.append("more_independent_sources")

    if not verifying:
        # Only weak/partial verdicts. Repairable unless we're out of passes.
        if repair_exhausted:
            if partial:
                return _result("admitted_with_caveat", "partially_supported")
            return _result("excluded", "unsupported")
        return _result("repair_required", "unsupported" if weak else "partially_supported",
                       ["strengthen_support"])

    # We have at least one verifying review.
    if unmet:
        if repair_exhausted:
            # Out of passes: keep it but flag the shortfall.
            return _result("admitted_with_caveat", "verified_with_gaps", unmet)
        return _result("repair_required", "requirements_unmet", unmet)

    if caveat_needed:
        return _result("admitted_with_caveat", "verified_indirect")
    return _result("admitted", "verified")
