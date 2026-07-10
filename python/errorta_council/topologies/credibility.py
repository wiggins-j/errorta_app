"""F078 Credibility topology — pure ordering, no I/O (invariant 2).

Two member rounds then a leader synthesis:

1. **Research + claim** (round 1): every enabled member speaks once. The member
   calls web_search / web_fetch (the scheduler's F039 tool loop handles those)
   and emits a claim-packet JSON.
2. **Credidation** (round 2): every enabled member speaks once, emitting peer
   review JSON for claims assigned to them.

After both rounds the topology returns ``RunCompletion(reason="finalized")``;
the scheduler runs the ``credibility_report`` finalizer, which parses the
transcript + fetched-source events and admits only verified claims.

Repair is handled at admission time (``compute_admission`` settles unresolved
claims once the repair budget is spent) — a live repair *round* is a later
refinement; v1 ships the claims + credidation + finalize cycle.
"""
from __future__ import annotations

from typing import Any

from errorta_council.topologies.round_robin import (
    RoundRobinTopology,
    RunCompletion,
    TurnProposal,
)


class CredibilityTopology:
    """Round-robin members across the room's configured rounds, then hand off to
    the credibility finalizer.

    Because the scheduler runs ONE model message per turn (a tool-call turn is
    distinct from a claim-packet turn), members need several turns to research
    (web_search / web_fetch), then write a claim packet, then — in later rounds —
    write peer reviews. So the room configures ``topology.max_rounds`` to give
    that headroom, and the ordering is plain round-robin. The marquee guarantee
    ("no citation without a fetched source AND a verifying peer review") is NOT
    enforced by turn ordering — it is enforced at finalization by the admission
    gate (``run_credibility_pipeline``), which is the code-level check. On
    completion this relabels the reason to ``finalized`` so the scheduler runs
    the credibility_report finalizer.
    """

    def __init__(self) -> None:
        self._rr = RoundRobinTopology()

    def propose_next(
        self, run: dict[str, Any], transcript: list[Any]
    ) -> TurnProposal | RunCompletion:
        result = self._rr.propose_next(run, transcript)
        if isinstance(result, RunCompletion):
            members = [m for m in run.get("members", []) if m.get("enabled", True)]
            return RunCompletion(
                reason="finalized",
                detail={
                    "topology": "credibility",
                    "underlying_reason": result.reason,
                    "member_count": len(members),
                },
            )
        return result
