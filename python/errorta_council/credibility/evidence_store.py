"""F078 in-memory, replay-safe evidence store + source independence grouping.

The store mints source ids and assigns independence groups deterministically
(counter-based — no clock/RNG, so replay is exact). It is the single source-
minting seam: the Slice 3 research phase calls ``ingest_source`` ONLY from
ToolGateway result ingestion, which is how the spec's "a source exists only
when Errorta fetched it" invariant is enforced in code.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .models import (
    Claim,
    ClaimAdmission,
    ClaimPacket,
    CredidationReview,
    EvidenceSpan,
    Source,
)

# Small multi-label public-suffix set so eTLD+1 grouping doesn't collapse e.g.
# bbc.co.uk and itv.co.uk into "co.uk". Not exhaustive — the v1 limitation is
# documented in the spec (ownership graphs are out of scope).
_MULTI_TLDS: frozenset[str] = frozenset({
    "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "net.uk",
    "com.au", "net.au", "org.au", "gov.au", "edu.au",
    "co.jp", "or.jp", "ne.jp", "go.jp",
    "co.nz", "govt.nz", "co.in", "gov.in", "co.za",
    "com.br", "gov.br", "com.cn", "gov.cn",
})


def _host_from_url(url: str) -> str:
    """Extract the host from a URL string WITHOUT importing ``urllib`` (banned
    in errorta_council by the invariant-3 egress guard — urllib carries
    urllib.request). Best-effort: strip scheme, path/query/fragment, userinfo,
    and port."""
    s = url.strip()
    if "://" in s:
        s = s.split("://", 1)[1]
    # Cut authority at the first path/query/fragment delimiter.
    for delim in ("/", "?", "#"):
        idx = s.find(delim)
        if idx != -1:
            s = s[:idx]
    if "@" in s:  # strip userinfo
        s = s.rsplit("@", 1)[1]
    if s.startswith("["):  # IPv6 literal [::1]:port
        s = s[1 : s.find("]")] if "]" in s else s[1:]
    elif ":" in s:  # strip :port
        s = s.split(":", 1)[0]
    return s


def registrable_domain(url: str) -> str:
    """eTLD+1 of a URL's host, lower-cased. Empty when unparseable.

    Heuristic: strip a known multi-label suffix to keep one extra label,
    otherwise take the last two labels. Good enough for v1 independence
    grouping; full PSL parsing is out of scope.
    """
    if not url:
        return ""
    host = _host_from_url(url).lower().strip(".")
    if not host:
        return ""
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    last2 = ".".join(labels[-2:])
    if last2 in _MULTI_TLDS and len(labels) >= 3:
        return ".".join(labels[-3:])
    return last2


@dataclass
class EvidenceStore:
    """Per-run evidence. Mutable (the scheduler appends across phases); replay
    rebuilds it from the recorded records."""

    run_id: str
    sources: dict[str, Source] = field(default_factory=dict)
    spans: dict[str, EvidenceSpan] = field(default_factory=dict)
    packets: dict[str, ClaimPacket] = field(default_factory=dict)
    reviews: dict[str, CredidationReview] = field(default_factory=dict)
    admissions: dict[str, ClaimAdmission] = field(default_factory=dict)
    _source_seq: int = 0
    _group_seq: int = 0

    # --- source minting + independence ------------------------------------

    def _next_source_id(self) -> str:
        self._source_seq += 1
        return f"src_{self._source_seq:04d}"

    def _matches_group(self, candidate: Source, existing: Source) -> bool:
        cu = candidate.canonical_url or candidate.url
        eu = existing.canonical_url or existing.url
        if cu and eu and cu == eu:
            return True
        if candidate.content_sha256 and candidate.content_sha256 == existing.content_sha256:
            return True
        cd = registrable_domain(candidate.canonical_url or candidate.url)
        ed = registrable_domain(existing.canonical_url or existing.url)
        if cd and cd == ed and candidate.author and candidate.author == existing.author:
            return True
        return False

    def _assign_group(self, candidate: Source) -> str:
        for existing in self.sources.values():
            if self._matches_group(candidate, existing):
                return existing.independence_group_id
        self._group_seq += 1
        return f"ig_{self._group_seq}"

    def ingest_source(
        self,
        *,
        url: str,
        tool_call_event_id: str,
        canonical_url: str = "",
        title: str = "",
        publisher: str = "",
        author: str = "",
        published_at: str | None = None,
        fetched_at: str = "",
        content_sha256: str = "",
        source_type: str = "unknown",
        egress_class: str = "public_web",
        quality_flags: list[str] | None = None,
    ) -> Source:
        """Mint + store a Source from a ToolGateway fetch result. Assigns the
        source id and its independence group. This is the ONLY way a Source
        enters the store."""
        source_id = self._next_source_id()
        provisional = Source(
            source_id=source_id, url=url,
            canonical_url=canonical_url or url, title=title, publisher=publisher,
            author=author, published_at=published_at, fetched_at=fetched_at,
            content_sha256=content_sha256, source_type=source_type,
            egress_class=egress_class, tool_call_event_id=tool_call_event_id,
            quality_flags=list(quality_flags or []),
        )
        group_id = self._assign_group(provisional)
        from dataclasses import replace as _replace

        source = _replace(provisional, independence_group_id=group_id)
        self.sources[source_id] = source
        return source

    def independence_group_count(self, source_ids: list[str]) -> int:
        """Distinct independence groups among the given sources (the count that
        drives the high-risk independence requirement)."""
        groups = {
            self.sources[sid].independence_group_id
            for sid in source_ids
            if sid in self.sources
        }
        return len(groups)

    # --- spans / packets / reviews / admissions ---------------------------

    def add_span(self, span: EvidenceSpan) -> None:
        self.spans[span.span_ref] = span

    def add_packet(self, packet: ClaimPacket) -> None:
        self.packets[packet.packet_id] = packet

    def add_review(self, review: CredidationReview) -> None:
        self.reviews[review.review_id] = review

    def set_admission(self, admission: ClaimAdmission) -> None:
        self.admissions[admission.claim_id] = admission

    def claims(self) -> list[Claim]:
        return [c for p in self.packets.values() for c in p.claims]

    def get_claim(self, claim_id: str) -> Claim | None:
        for c in self.claims():
            if c.claim_id == claim_id:
                return c
        return None

    def reviews_for(self, claim_id: str) -> list[CredidationReview]:
        return [r for r in self.reviews.values() if r.claim_id == claim_id]

    # --- replay-safe serialization ----------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "source_seq": self._source_seq,
            "group_seq": self._group_seq,
            "sources": [s.to_dict() for s in self.sources.values()],
            "spans": [s.to_dict() for s in self.spans.values()],
            "packets": [p.to_dict() for p in self.packets.values()],
            "reviews": [r.to_dict() for r in self.reviews.values()],
            "admissions": [a.to_dict() for a in self.admissions.values()],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "EvidenceStore":
        store = cls(run_id=str(raw.get("run_id", "")))
        store._source_seq = int(raw.get("source_seq", 0))
        store._group_seq = int(raw.get("group_seq", 0))
        for s in raw.get("sources") or []:
            src = Source.from_dict(s)
            store.sources[src.source_id] = src
        for s in raw.get("spans") or []:
            span = EvidenceSpan.from_dict(s)
            store.spans[span.span_ref] = span
        for p in raw.get("packets") or []:
            pkt = ClaimPacket.from_dict(p)
            store.packets[pkt.packet_id] = pkt
        for r in raw.get("reviews") or []:
            rev = CredidationReview.from_dict(r)
            store.reviews[rev.review_id] = rev
        for a in raw.get("admissions") or []:
            adm = ClaimAdmission.from_dict(a)
            store.admissions[adm.claim_id] = adm
        return store
