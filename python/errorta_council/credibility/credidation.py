"""F078 credidation reviewer assignment (pure, deterministic).

Assign each submitted claim to one or more NON-AUTHOR reviewers, distributing
load round-robin across the available members. Key/high-risk claims get two
reviewers under strict mode (when enough non-author members exist). No I/O —
the scheduler turns these assignments into per-reviewer turns.
"""
from __future__ import annotations

from .models import ClaimPacket, is_key_claim


def assign_reviewers(
    *,
    packets: list[ClaimPacket],
    member_ids: list[str],
    strictness: str = "normal",
    require_two_reviewers_for_key_claims: bool = False,
) -> dict[str, list[str]]:
    """Return ``{claim_id: [reviewer_member_id, ...]}``.

    - never self-review (the claim's author is excluded);
    - round-robin across eligible members for even load;
    - two reviewers for key claims when ``strict`` or
      ``require_two_reviewers_for_key_claims`` AND >= 2 non-author members exist;
    - deterministic: same inputs → same assignment (no RNG/clock).
    """
    # Map claim_id → author so we can exclude the author.
    author_of: dict[str, str] = {}
    ordered_claims: list[tuple[str, str, int]] = []  # (claim_id, author, want)
    want_two = (strictness == "strict") or bool(require_two_reviewers_for_key_claims)

    for packet in packets:
        for claim in packet.claims:
            if claim.kind == "uncited_observation":
                continue  # not a factual citation → no peer review needed
            author_of[claim.claim_id] = packet.member_id
            key = is_key_claim(key=claim.key, risk=claim.risk)
            want = 2 if (key and want_two) else 1
            ordered_claims.append((claim.claim_id, packet.member_id, want))

    assignments: dict[str, list[str]] = {}
    # A rotating cursor over member_ids gives even, deterministic distribution.
    cursor = 0
    n = len(member_ids)
    for claim_id, author, want in ordered_claims:
        eligible = [m for m in member_ids if m != author]
        if not eligible:
            assignments[claim_id] = []
            continue
        chosen: list[str] = []
        # Walk the rotating cursor, skipping the author, until we have `want`
        # distinct reviewers (capped at the number of eligible members).
        target = min(want, len(eligible))
        guard = 0
        while len(chosen) < target and guard < n * 2 + 2:
            cand = member_ids[cursor % n] if n else None
            cursor += 1
            guard += 1
            if cand is None or cand == author or cand in chosen:
                continue
            chosen.append(cand)
        assignments[claim_id] = chosen
    return assignments
