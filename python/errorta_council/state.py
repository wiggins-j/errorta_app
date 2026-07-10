"""Scheduler-derived counters reconstructed from the append-only event log."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from errorta_council.schema import CouncilEvent, EventType


@dataclass(frozen=True)
class RunCounters:
    """Counters reconstructed from the append-only event log.

    ``completed_messages_by_member`` and ``total_messages_completed``
    count only ``MEMBER_MESSAGE`` events — these are the meters the
    budget caps were specified against.

    ``attempts_by_member`` counts ``MEMBER_MESSAGE`` PLUS
    ``MEMBER_SKIPPED`` PLUS ``MEMBER_FAILED``. It is the meter the
    topology must use for round-progression and per-member quota
    decisions — without it, a member that gets context-blocked or
    admission-blocked is re-proposed indefinitely (no MEMBER_MESSAGE
    is ever appended, so the legacy ``completed`` counters stay at
    zero forever). Maps directly to the QA review-finding lock.
    """
    completed_messages_by_member: dict[str, int] = field(default_factory=dict)
    attempts_by_member: dict[str, int] = field(default_factory=dict)
    total_messages_completed: int = 0
    round_index: int = 0
    terminal_reason: str | None = None


class CounterRebuilder:
    """Pure function over an event list. No I/O."""

    @staticmethod
    def from_events(events: Iterable[CouncilEvent]) -> RunCounters:
        by_member: dict[str, int] = {}
        attempts: dict[str, int] = {}
        total = 0
        max_round = 0
        terminal_reason: str | None = None
        for ev in events:
            if ev.round is not None and ev.round > max_round:
                max_round = ev.round
            if (
                ev.type == EventType.MEMBER_MESSAGE
                and ev.member_id is not None
                # F037: expert-callout answers are MEMBER_MESSAGE events with a
                # non-member target id. They must NOT count toward deliberation
                # meters (total/by_member/attempts) or they prematurely trip the
                # topology's max_total_member_messages cap and end runs early.
                and not (ev.payload or {}).get("is_callout")
            ):
                by_member[ev.member_id] = by_member.get(ev.member_id, 0) + 1
                attempts[ev.member_id] = attempts.get(ev.member_id, 0) + 1
                total += 1
            elif (
                ev.type in (EventType.MEMBER_SKIPPED, EventType.MEMBER_FAILED)
                and ev.member_id is not None
            ):
                # Skipped + failed turns count toward the member's
                # round-attempt total so round-robin advances past
                # permanently-blocked members instead of looping on them.
                attempts[ev.member_id] = attempts.get(ev.member_id, 0) + 1
            elif ev.type in (
                EventType.RUN_COMPLETED,
                EventType.RUN_FAILED,
                EventType.RUN_CANCELLED,
            ):
                terminal_reason = ev.payload.get("reason") if ev.payload else None
        return RunCounters(
            completed_messages_by_member=by_member,
            attempts_by_member=attempts,
            total_messages_completed=total,
            round_index=max_round,
            terminal_reason=terminal_reason,
        )
