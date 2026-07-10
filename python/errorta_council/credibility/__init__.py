"""F078 Credibility mode — evidence data model + store.

Slice 2 ships the typed, replay-safe data contracts (sources, evidence spans,
claim packets, credidation reviews, claim admissions) plus the in-memory
``EvidenceStore`` that mints source ids and assigns independence groups. The
research/credidation scheduler phases (Slice 3+) build on these; nothing here
touches the network — sources are ingested from ToolGateway results upstream.
"""
from __future__ import annotations

from .models import (
    ADMISSIONS,
    CLAIM_KINDS,
    CLAIM_RISKS,
    CREDIBILITY_SOURCE_TYPES,
    REVIEW_STATUSES,
    SUPPORT_QUALITIES,
    Claim,
    ClaimAdmission,
    ClaimPacket,
    CredidationReview,
    EvidenceSpan,
    Source,
    is_key_claim,
    member_is_steelman,
    source_tier,
    source_tier_label,
    steelman_topic,
)
from .evidence_store import EvidenceStore
from .admission import compute_admission
from .credidation import assign_reviewers
from .report import (
    CredibilityReport,
    parse_claim_packet,
    parse_digest_claims,
    parse_review,
    run_credibility_pipeline,
)

__all__ = [
    "ADMISSIONS",
    "CredibilityReport",
    "assign_reviewers",
    "compute_admission",
    "parse_claim_packet",
    "parse_digest_claims",
    "parse_review",
    "run_credibility_pipeline",
    "CLAIM_KINDS",
    "CLAIM_RISKS",
    "CREDIBILITY_SOURCE_TYPES",
    "REVIEW_STATUSES",
    "SUPPORT_QUALITIES",
    "Claim",
    "ClaimAdmission",
    "ClaimPacket",
    "CredidationReview",
    "EvidenceSpan",
    "EvidenceStore",
    "Source",
    "is_key_claim",
    "member_is_steelman",
    "source_tier",
    "source_tier_label",
    "steelman_topic",
]
