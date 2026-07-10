"""F078 Credibility-mode typed data contracts (replay-safe).

Every record (de)serializes through ``_split_unknown`` / ``_emit_nested`` so
forward-compat keys round-trip, mirroring the room schema. None of these carry
raw page bytes beyond a policy-capped excerpt — full content lives in the side
store (F044). Events reference these by id, never by value.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..schema import _emit_nested, _split_unknown

CREDIBILITY_SOURCE_TYPES: frozenset[str] = frozenset({
    "official", "primary_document", "peer_reviewed_paper", "government",
    "standards_body", "company_docs", "reputable_news", "trade_publication",
    "blog", "forum", "unknown",
})

# F085: provenance tiers. A source's TYPE (above) rolls up to a coarse TIER that
# signals how much epistemic weight a citation carries — the basis for the
# inline provenance tag. Opinion/unknown sources are allowed (more viewpoints on
# the internet is good) but labeled so a reader weighs them as individual
# perspective, not corroborated reporting. A perspective can still be the seed of
# a strong argument — the tag is transparency, not exclusion.
_SOURCE_TIER_BY_TYPE: dict[str, str] = {
    "official": "primary",
    "primary_document": "primary",
    "peer_reviewed_paper": "primary",
    "government": "primary",
    "standards_body": "primary",
    "reputable_news": "reporting",
    "trade_publication": "reporting",
    "company_docs": "reporting",
    "blog": "opinion",
    "forum": "opinion",
    "unknown": "unknown",
}
# Short label shown next to a citation, e.g. "veracalloway.com · opinion".
_SOURCE_TIER_LABEL: dict[str, str] = {
    "primary": "primary",
    "reporting": "reporting",
    "opinion": "opinion",
    "unknown": "unverified",
}


def source_tier(source_type: str | None) -> str:
    """Roll a source_type up to its provenance tier: primary | reporting |
    opinion | unknown. Unrecognized types are treated as 'unknown'."""
    return _SOURCE_TIER_BY_TYPE.get(str(source_type or "").strip(), "unknown")


def source_tier_label(source_type: str | None) -> str:
    """The short word shown next to a citation for this source's tier."""
    return _SOURCE_TIER_LABEL[source_tier(source_type)]


CLAIM_KINDS: frozenset[str] = frozenset({
    "factual", "definition", "statistic", "quote", "time_sensitive",
    "interpretation", "recommendation", "uncited_observation",
})

CLAIM_RISKS: frozenset[str] = frozenset({"low", "normal", "high", "time_sensitive"})

REVIEW_STATUSES: frozenset[str] = frozenset({
    "verified", "partially_supported", "unsupported", "contradicted",
    "source_unreachable", "source_unreliable", "stale",
    "needs_primary_source", "not_reviewable",
})

SUPPORT_QUALITIES: frozenset[str] = frozenset({
    "direct", "indirect", "inferential", "does_not_support", "contradicts",
})

ADMISSIONS: frozenset[str] = frozenset({
    "admitted", "admitted_with_caveat", "repair_required", "excluded",
})

# A claim is "key" when the author marks it or its risk forces it (the floor).
_KEY_RISKS: frozenset[str] = frozenset({"high", "time_sensitive"})


def is_key_claim(*, key: bool, risk: str) -> bool:
    """Risk-derived key floor: risk in {high, time_sensitive} ⇒ key; the author
    may raise key-ness but never lower the floor (spec §"Key claims")."""
    return bool(key) or risk in _KEY_RISKS


def member_is_steelman(member: dict[str, Any] | None) -> bool:
    """F084: True when this member is a designated steelman advocate — it argues
    its assigned ``steelman_topic`` as forcefully as possible and MAY construct
    supporting evidence/citations. Its claims are quarantined downstream (never
    admitted, never source-supported, never promoted to the corpus) and labeled
    unverified everywhere. The flag lives in the member's metadata bag so it
    round-trips through the room editor without a schema change."""
    md = dict((member or {}).get("metadata") or {})
    return bool(md.get("steelman"))


def steelman_topic(member: dict[str, Any] | None) -> str:
    """F084: the proposition a steelman member argues FOR (e.g. 'Existence of
    Santa'). Empty string when unset — the prompt then falls back to the
    member's own system instructions."""
    md = dict((member or {}).get("metadata") or {})
    topic = md.get("steelman_topic")
    return topic.strip() if isinstance(topic, str) else ""


@dataclass(frozen=True)
class Source:
    """A source EXISTS only when ToolGateway fetched it (spec §Source Capture).
    Search-result snippets never become a Source."""

    source_id: str
    url: str
    canonical_url: str = ""
    title: str = ""
    publisher: str = ""
    author: str = ""
    published_at: str | None = None
    fetched_at: str = ""
    content_sha256: str = ""
    source_type: str = "unknown"
    egress_class: str = "public_web"
    independence_group_id: str = ""
    tool_call_event_id: str = ""
    extract_refs: list[str] = field(default_factory=list)
    quality_flags: list[str] = field(default_factory=list)
    _extras: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return _emit_nested(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Source":
        known, extras = _split_unknown(cls, raw or {})
        return cls(**known, _extras=extras)


@dataclass(frozen=True)
class EvidenceSpan:
    span_ref: str
    source_id: str
    text_sha256: str = ""
    char_start: int = 0
    char_end: int = 0
    excerpt: str = ""
    _extras: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return _emit_nested(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "EvidenceSpan":
        known, extras = _split_unknown(cls, raw or {})
        return cls(**known, _extras=extras)


@dataclass(frozen=True)
class Claim:
    claim_id: str
    text: str
    kind: str = "factual"
    risk: str = "normal"
    key: bool = False
    source_ids: list[str] = field(default_factory=list)
    support_span_refs: list[str] = field(default_factory=list)
    confidence: str = "medium"
    recency_sensitive: bool = False
    member_notes: str = ""
    _extras: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def is_key(self) -> bool:
        return is_key_claim(key=self.key, risk=self.risk)

    def to_dict(self) -> dict[str, Any]:
        return _emit_nested(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Claim":
        known, extras = _split_unknown(cls, raw or {})
        return cls(**known, _extras=extras)


@dataclass(frozen=True)
class ClaimPacket:
    packet_id: str
    member_id: str
    answer_fragment: str = ""
    claims: list[Claim] = field(default_factory=list)
    coverage_notes: list[str] = field(default_factory=list)
    _extras: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        d = _emit_nested(self)
        d["claims"] = [c.to_dict() for c in self.claims]
        return d

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ClaimPacket":
        known, extras = _split_unknown(cls, raw or {})
        known.pop("claims", None)
        claims = [Claim.from_dict(c) for c in (raw or {}).get("claims") or []]
        return cls(**known, claims=claims, _extras=extras)


@dataclass(frozen=True)
class CredidationReview:
    review_id: str
    claim_id: str
    reviewer_member_id: str
    status: str = "not_reviewable"
    source_quality: str = "unknown"
    support_quality: str = "does_not_support"
    reason: str = ""
    contradicting_source_ids: list[str] = field(default_factory=list)
    suggested_repair: str | None = None
    _extras: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return _emit_nested(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "CredidationReview":
        known, extras = _split_unknown(cls, raw or {})
        return cls(**known, _extras=extras)


@dataclass(frozen=True)
class ClaimAdmission:
    claim_id: str
    admission: str = "excluded"
    final_status: str = "unsupported"
    required_repairs: list[str] = field(default_factory=list)
    review_event_ids: list[str] = field(default_factory=list)
    # F082: the actionable disposition (sourced | revised | inference | indirect |
    # excluded) and, for an overclaim, the revised-down claim text the report
    # should cite instead of the original.
    disposition: str = ""
    revised_text: str = ""
    _extras: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return _emit_nested(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ClaimAdmission":
        known, extras = _split_unknown(cls, raw or {})
        return cls(**known, _extras=extras)
