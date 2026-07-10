"""Deterministic text rendering for F036 digest_v1."""
from __future__ import annotations

from typing import Any


def render_digest_v1(digest: dict[str, Any], *, member_id: str = "", round_n: int | None = None) -> str:
    prefix = member_id
    if round_n is not None:
        prefix = f"{prefix} (round {round_n})" if prefix else f"round {round_n}"
    lines = []
    if prefix:
        lines.append(f"{prefix}: position - {digest.get('position', '')}")
    else:
        lines.append(f"position - {digest.get('position', '')}")
    claims = []
    for claim in digest.get("claims") or []:
        # Render citations using the [c:id] marker syntax so the
        # CitationRegistry alias scan (`_MARKER_RE` in citations.py) picks
        # them up for the citation-appendix block. QA review 2026-06-12
        # caught that the prior "cites c1,c2" format never matched and
        # later members lost the citation index for prior digest claims.
        cite_part = ""
        cites = [str(c) for c in (claim.get("cites") or [])]
        if cites:
            cite_part = ", cites " + " ".join(f"[c:{c}]" for c in cites)
        claims.append(
            f"{claim.get('id')} ({claim.get('confidence', 'medium')}{cite_part}): {claim.get('text', '')}"
        )
    if claims:
        lines.append("claims: " + " ".join(claims))
    disputes = []
    for item in digest.get("dispute") or []:
        disputes.append(
            f"{item.get('member', '?')}.{item.get('claim', '?')}: {item.get('why', '')}"
        )
    if disputes:
        lines.append("disputes: " + " ".join(disputes))
    delta = digest.get("delta")
    if delta:
        lines.append(f"delta: {delta}")
    open_items = digest.get("open") or []
    if open_items:
        lines.append("open: " + " | ".join(str(x) for x in open_items))
    answer = digest.get("answer_fragment")
    if answer:
        lines.append(f"answer_fragment: {answer}")
    return "\n".join(lines)


__all__ = ["render_digest_v1"]
