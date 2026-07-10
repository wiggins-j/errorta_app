"""F078 credibility pipeline + report builder (pure, deterministic).

Ties the Slice 4 cores together over a finished transcript:

1. parse member claim-packet / review JSON out of message content;
2. build the EvidenceStore from the run's web_fetch tool events (sources exist
   ONLY because Errorta fetched them — naked URLs and search snippets never
   become evidence);
3. resolve each claim's cited sources against the fetched set;
4. run compute_admission per claim using the submitted reviews;
5. assemble a ``CredibilityReport`` that cites only admitted claim/source pairs.

No I/O, no events — the scheduler/finalizer calls this and records the result.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .admission import compute_admission
from .evidence_store import EvidenceStore
from .models import (
    Claim,
    ClaimAdmission,
    ClaimPacket,
    CredidationReview,
    source_tier,
    source_tier_label,
)

_URL_RE = re.compile(r"https?://", re.IGNORECASE)


def _loads(content: str) -> dict[str, Any] | None:
    """Best-effort: parse the first JSON object embedded in a member message."""
    if not content:
        return None
    try:
        obj = json.loads(content)
        return obj if isinstance(obj, dict) else None
    except (ValueError, TypeError):
        pass
    start = content.find("{")
    end = content.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(content[start : end + 1])
            return obj if isinstance(obj, dict) else None
        except (ValueError, TypeError):
            return None
    return None


def parse_claim_packet(member_id: str, content: str) -> ClaimPacket | None:
    """Parse a claim packet from member output. Returns None when the message
    carries no recognizable packet (e.g. a pure tool-call turn, a peer review,
    or a digest_v1 envelope whose ``claims`` are a different shape)."""
    obj = _loads(content)
    if not obj or "claims" not in obj:
        return None
    # The digest_v1 dialect also carries a ``claims`` array, but its claim
    # objects have a different shape (no ``claim_id``). It is NOT a claim
    # packet — let parse_digest_claims handle it. (Without this guard a stray
    # digest message in the transcript would crash Claim.from_dict.)
    if isinstance(obj.get("v"), str) and obj["v"].startswith("digest_v"):
        return None
    raw = dict(obj)
    raw["member_id"] = member_id
    raw.setdefault("packet_id", f"pkt_{member_id}")
    # Keep only well-formed claim dicts (must carry a claim_id). A malformed
    # entry must never crash the parser — a Credibility run reads peers' raw,
    # model-authored messages and any one of them can be off-shape.
    cleaned: list[dict[str, Any]] = []
    for c in (raw.get("claims") or []):
        if not isinstance(c, dict) or not isinstance(c.get("claim_id"), (str, int)):
            continue
        c = dict(c)
        c["claim_id"] = str(c["claim_id"])
        cleaned.append(c)
    raw["claims"] = cleaned
    if not raw["claims"]:
        return None
    try:
        return ClaimPacket.from_dict(raw)
    except (TypeError, ValueError):
        return None


def parse_review(reviewer_member_id: str, content: str) -> list[CredidationReview]:
    """Parse one or more credidation reviews from a reviewer's message."""
    obj = _loads(content)
    if not obj:
        return []
    items = obj.get("reviews") if isinstance(obj.get("reviews"), list) else None
    if items is None:
        items = [obj] if "claim_id" in obj and "status" in obj else []
    out: list[CredidationReview] = []
    for i, it in enumerate(items):
        if not isinstance(it, dict) or "claim_id" not in it:
            continue
        raw = dict(it)
        raw["reviewer_member_id"] = reviewer_member_id
        raw.setdefault("review_id", f"rev_{reviewer_member_id}_{i}")
        out.append(CredidationReview.from_dict(raw))
    return out


def is_naked_url_citation(token: str) -> bool:
    """A citation that is a bare URL (not a minted source id) — rejected as
    evidence; only fetched-and-minted sources count."""
    return bool(_URL_RE.match(token.strip()))


# digest_v1 claim line, e.g.  "claim_1 high Honolulu is the capital. [c:https://x]"
_DIGEST_CLAIM_RE = re.compile(
    r"^\s*claim[_\s-]*(\S+)\s+(low|normal|high|time_sensitive)\s+(.*)$",
    re.IGNORECASE | re.MULTILINE,
)
# Citation token; tolerates the [c:c:url] prefix-doubling bug.
_DIGEST_CITE_RE = re.compile(r"\[c:(?:c:)*\s*(https?://[^\]\s]+)\]")


def parse_digest_claims(member_id: str, content: str) -> list[Claim]:
    """Fallback parser for the digest_v1 dialect some (esp. local) models emit
    instead of a JSON claim packet. Extracts each ``claim_N <risk> <text> [c:url]``
    line into a Claim, citing the URLs (de-doubled). Returns [] when the message
    isn't digest claims."""
    out: list[Claim] = []
    for m in _DIGEST_CLAIM_RE.finditer(content or ""):
        cid, risk, rest = m.group(1), m.group(2).lower(), m.group(3)
        urls = _DIGEST_CITE_RE.findall(rest)
        text = _DIGEST_CITE_RE.sub("", rest).strip()
        out.append(Claim(
            claim_id=f"{member_id}:{cid}", text=text or rest.strip(),
            kind="factual", risk=risk if risk in {"low", "normal", "high", "time_sensitive"} else "normal",
            source_ids=urls,
        ))
    return out


@dataclass(frozen=True)
class CredibilityReport:
    mode: str = "credibility_report"
    answer: str = ""
    claims_used: list[str] = field(default_factory=list)
    source_map: list[dict[str, Any]] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    excluded_claims: list[dict[str, str]] = field(default_factory=list)
    confidence: str = "medium"
    admissions: list[ClaimAdmission] = field(default_factory=list)
    verification_incomplete: bool = False
    # F081: a debate-quality flag (e.g. "unchallenged_consensus" when no opposing
    # case was mounted). Empty when the debate was sound.
    quality_flag: str = ""
    # F082: per-admitted-claim disposition (sourced | revised | inference |
    # indirect) + the revised-down text; the bare-caveat rate; and finalizer
    # citation failures (Pillar 2).
    dispositions: list[dict[str, str]] = field(default_factory=list)
    caveat_rate: float = 0.0
    finalizer_citation_failures: list[dict[str, str]] = field(default_factory=list)
    # F084: claims from designated steelman advocates. Quarantined — NEVER
    # admitted, NEVER counted as source-supported, NEVER in source_map. Kept here
    # only so the UI can show "Steelman arguments (unverified)" distinctly. Each
    # entry: {claim_id, member_id, topic, text, cited: [str]}.
    steelman_claims: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "answer": self.answer,
            "claims_used": list(self.claims_used),
            "source_map": [dict(s) for s in self.source_map],
            "caveats": list(self.caveats),
            "excluded_claims": [dict(e) for e in self.excluded_claims],
            "confidence": self.confidence,
            "admissions": [a.to_dict() for a in self.admissions],
            "verification_incomplete": self.verification_incomplete,
            "quality_flag": self.quality_flag,
            "dispositions": [dict(d) for d in self.dispositions],
            "caveat_rate": self.caveat_rate,
            "finalizer_citation_failures": [dict(f) for f in self.finalizer_citation_failures],
            "steelman_claims": [dict(s) for s in self.steelman_claims],
        }


def _resolve_citations(claim: Claim, store: EvidenceStore, url_to_id: dict[str, str]) -> list[str]:
    """Map a claim's cited source tokens to MINTED source ids, dropping any that
    were never fetched (naked URLs / snippet references / unknown ids)."""
    resolved: list[str] = []
    for token in claim.source_ids:
        t = token.strip()
        if t in store.sources:
            resolved.append(t)
        elif t in url_to_id:
            resolved.append(url_to_id[t])
        # else: dropped — not a fetched source.
    return resolved


def run_credibility_pipeline(
    *,
    packets: list[ClaimPacket],
    reviews: list[CredidationReview],
    store: EvidenceStore,
    policy,
    leader_answer: str = "",
    repair_exhausted: bool = True,
    tool_failure: bool = False,
    entailment_by_claim: dict[str, str] | None = None,
    revised_text_by_claim: dict[str, str] | None = None,
    validity_by_claim: dict[str, str] | None = None,
    finalizer_citation_failures: list[dict[str, str]] | None = None,
    steelman_member_ids: set[str] | None = None,
    steelman_topics: dict[str, str] | None = None,
) -> CredibilityReport:
    """Compute admissions over the transcript and assemble the report.

    ``store`` already holds the sources minted from web_fetch events. Claims may
    cite either minted source ids or the fetched URL; anything else is dropped
    (the naked-URL / snippet guard). ``repair_exhausted`` reflects whether the
    repair budget is spent (the finalizer calls this after the last pass).
    """
    url_to_id = {
        (s.canonical_url or s.url): s.source_id for s in store.sources.values()
    }
    # Map each claim to its author so we can drop self-reviews — a member may
    # never verify its own claim (Reviewer P1). A review whose reviewer is the
    # claim's author is discarded before admission.
    author_of: dict[str, str] = {}
    for packet in packets:
        for claim in packet.claims:
            author_of[claim.claim_id] = packet.member_id
    reviews_by_claim: dict[str, list[CredidationReview]] = {}
    for r in reviews:
        if r.reviewer_member_id and r.reviewer_member_id == author_of.get(r.claim_id):
            continue  # self-review — not counted
        reviews_by_claim.setdefault(r.claim_id, []).append(r)

    claims_used: list[str] = []
    caveats: list[str] = []
    excluded: list[dict[str, str]] = []
    admissions: list[ClaimAdmission] = []
    dispositions: list[dict[str, str]] = []
    used_source_ids: set[str] = set()
    bare_caveat_count = 0  # F082: caveats that are STILL just an asterisk (indirect)
    steelman_ids = set(steelman_member_ids or set())
    topics = dict(steelman_topics or {})
    steelman_claims: list[dict[str, Any]] = []

    for packet in packets:
        # F084: a steelman advocate's claims are quarantined. They never enter
        # the admission gate, never count as source-supported, never reach
        # source_map/confidence — they are surfaced separately and labeled
        # unverified. Their (possibly constructed) citations are kept verbatim
        # for display only.
        if packet.member_id in steelman_ids:
            for claim in packet.claims:
                steelman_claims.append({
                    "claim_id": claim.claim_id,
                    "member_id": packet.member_id,
                    "topic": topics.get(packet.member_id, ""),
                    "text": claim.text,
                    "cited": list(claim.source_ids),
                })
            continue
        for claim in packet.claims:
            resolved = _resolve_citations(claim, store, url_to_id)
            # Rebuild the claim with only fetched-source citations so admission
            # judges real evidence, not naked URLs.
            judged = Claim(
                claim_id=claim.claim_id, text=claim.text, kind=claim.kind,
                risk=claim.risk, key=claim.key, source_ids=resolved,
                support_span_refs=claim.support_span_refs,
                confidence=claim.confidence, recency_sensitive=claim.recency_sensitive,
                member_notes=claim.member_notes,
            )
            adm = compute_admission(
                claim=judged,
                reviews=reviews_by_claim.get(claim.claim_id, []),
                policy=policy,
                independence_groups=store.independence_group_count(resolved),
                repair_exhausted=repair_exhausted,
                entailment=(entailment_by_claim or {}).get(claim.claim_id),
                revised_text=(revised_text_by_claim or {}).get(claim.claim_id, ""),
                validity=(validity_by_claim or {}).get(claim.claim_id),
            )
            admissions.append(adm)
            if adm.admission in ("admitted", "admitted_with_caveat"):
                claims_used.append(claim.claim_id)
                used_source_ids.update(resolved)
                # F082: record the actionable disposition (what survives), and a
                # human caveat line keyed off it — not a uniform "(indirect)".
                cited_text = adm.revised_text if adm.disposition == "revised" and adm.revised_text else claim.text
                dispositions.append({
                    "claim_id": claim.claim_id, "disposition": adm.disposition,
                    "text": cited_text, "revised_text": adm.revised_text,
                })
                if adm.disposition == "revised":
                    caveats.append(
                        f"Claim {claim.claim_id} narrowed to what the source supports: "
                        f"“{adm.revised_text}”."
                    )
                elif adm.disposition == "inference":
                    caveats.append(
                        f"Claim {claim.claim_id} admitted as an inference "
                        f"(not directly stated by the source)."
                    )
                elif adm.admission == "admitted_with_caveat":
                    bare_caveat_count += 1
                    caveats.append(
                        f"Claim {claim.claim_id} admitted with caveat "
                        f"({adm.final_status})."
                    )
            else:
                excluded.append({"claim_id": claim.claim_id, "reason": adm.final_status})

    source_map = [
        {
            "source_id": s.source_id, "url": s.canonical_url or s.url,
            "title": s.title, "source_type": s.source_type, "fetched_at": s.fetched_at,
            # F085: provenance tier + label for the inline provenance tag.
            "tier": source_tier(s.source_type),
            "tier_label": source_tier_label(s.source_type),
        }
        for sid, s in store.sources.items()
        if sid in used_source_ids
    ]

    if not admissions:
        confidence = "low"
    elif any(a.admission == "admitted" for a in admissions) and not caveats:
        confidence = "high" if len(claims_used) >= 2 else "medium"
    elif claims_used:
        confidence = "medium"
    else:
        confidence = "low"

    caveat_rate = (bare_caveat_count / len(claims_used)) if claims_used else 0.0

    return CredibilityReport(
        answer=leader_answer,
        claims_used=claims_used,
        source_map=source_map,
        caveats=caveats,
        excluded_claims=excluded,
        confidence=confidence,
        admissions=admissions,
        verification_incomplete=bool(tool_failure),
        dispositions=dispositions,
        caveat_rate=round(caveat_rate, 3),
        finalizer_citation_failures=list(finalizer_citation_failures or []),
        steelman_claims=steelman_claims,
    )
