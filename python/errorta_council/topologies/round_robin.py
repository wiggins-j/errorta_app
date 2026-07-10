"""Pure round-robin topology. No I/O, no event writes (invariant 2)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from errorta_council.limits import ReasonCode, SchedulerPolicy
from errorta_council.state import RunCounters


@dataclass(frozen=True)
class TurnProposal:
    member_id: str
    round: int
    turn_index: int
    # QA 2026-06-12: optional cursor override. When set, the scheduler
    # slices ``prior_events[:transcript_cursor]`` before passing to context
    # build, so the topology can freeze visibility (e.g. round-1 blind
    # parallel for consensus deliberation). ``None`` keeps the default
    # behavior (read every event up to now).
    transcript_cursor: int | None = None


@dataclass(frozen=True)
class RunCompletion:
    reason: str
    # F064: optional structured detail about HOW the run completed (e.g. for
    # consensus: which members agreed, the threshold, the round). The scheduler
    # may stamp this onto the terminal event so the UI can explain it. Always
    # member-ids + counts — never raw content (byte isolation).
    detail: dict[str, Any] | None = None


class RoundRobinTopology:
    """Iterate enabled members in order, increment round on wrap-around."""

    def propose_next(
        self, run: dict[str, Any], transcript: list[Any]
    ) -> TurnProposal | RunCompletion:
        members: list[dict[str, Any]] = run["members"]
        counters: RunCounters = run["counters"]
        policy: SchedulerPolicy = run["policy"]

        enabled = [m for m in members if m.get("enabled", True)]
        if not enabled:
            return RunCompletion(reason=ReasonCode.NO_ELIGIBLE_MEMBERS.value)

        # Global total cap pre-empts everything else.
        if (
            policy.max_total_member_messages is not None
            and counters.total_messages_completed >= policy.max_total_member_messages
        ):
            return RunCompletion(reason=ReasonCode.LIMITS_EXHAUSTED.value)

        # Filter by per-member cap. Attempts (completed + skipped + failed)
        # are the gate — without that, a context-blocked or
        # admission-blocked member would re-propose forever because
        # ``completed`` never increments for them.
        per_member_cap = policy.max_messages_per_member

        def _attempts(m_id: str) -> int:
            # ``attempts_by_member`` is the F031-09 review-finding field
            # that includes blocked-skip turns. Fall back to ``completed``
            # so callers that construct RunCounters directly (legacy
            # fixtures, recovery) still produce the same proposals they
            # always did when no skips have happened.
            attempts = counters.attempts_by_member.get(m_id, 0)
            completed = counters.completed_messages_by_member.get(m_id, 0)
            return max(attempts, completed)

        eligible = [
            m for m in enabled
            if per_member_cap is None or _attempts(m["id"]) < per_member_cap
        ]
        if not eligible:
            return RunCompletion(reason=ReasonCode.LIMITS_EXHAUSTED.value)

        # Pick the eligible member with the lowest attempt count. This is
        # the round-robin invariant: every eligible member gets their
        # slot per round before any member's second slot. Ties broken by
        # original ``enabled`` order.
        min_count = min(_attempts(m["id"]) for m in eligible)
        next_member = next(
            m for m in eligible if _attempts(m["id"]) == min_count
        )

        # Round index: round we are about to be in. Use attempts (not
        # completed) so a round full of blocked members still advances.
        if all(
            _attempts(m["id"]) >= counters.round_index
            for m in enabled
        ):
            new_round = counters.round_index + 1
        else:
            new_round = max(counters.round_index, 1)

        if policy.max_rounds is not None and new_round > policy.max_rounds:
            return RunCompletion(reason=ReasonCode.LIMITS_EXHAUSTED.value)

        return TurnProposal(
            member_id=next_member["id"],
            round=new_round,
            turn_index=counters.total_messages_completed,
        )
