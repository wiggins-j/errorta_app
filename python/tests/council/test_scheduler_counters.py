from __future__ import annotations

from errorta_council.schema import CouncilEvent, EventStatus, EventType
from errorta_council.state import (
    CounterRebuilder,
    RunCounters,
)


def _ev(seq: int, type_: EventType, *, member_id: str | None = None,
        round_: int | None = None, status: EventStatus = EventStatus.COMPLETED,
        payload: dict | None = None) -> CouncilEvent:
    return CouncilEvent(
        format_version=1,
        id=f"e{seq}",
        run_id="r1",
        sequence=seq,
        type=type_,
        status=status,
        created_at="2026-06-11T00:00:00Z",
        payload=payload or {},
        member_id=member_id,
        round=round_,
    )


def test_member_message_increments_once() -> None:
    events = [
        _ev(1, EventType.RUN_STARTED),
        _ev(2, EventType.MEMBER_MESSAGE, member_id="m1", round_=1),
        _ev(3, EventType.MEMBER_MESSAGE, member_id="m2", round_=1),
        _ev(4, EventType.MEMBER_MESSAGE, member_id="m1", round_=2),
    ]
    counters = CounterRebuilder.from_events(events)
    assert counters.completed_messages_by_member == {"m1": 2, "m2": 1}
    assert counters.total_messages_completed == 3
    assert counters.round_index == 2


def test_skipped_blocked_failed_cancelled_do_not_increment() -> None:
    events = [
        _ev(1, EventType.MEMBER_MESSAGE, member_id="m1", round_=1),
        _ev(2, EventType.MEMBER_SKIPPED, member_id="m1", round_=1, status=EventStatus.SKIPPED),
        _ev(3, EventType.MEMBER_FAILED, member_id="m1", round_=1, status=EventStatus.FAILED),
        _ev(4, EventType.MEMBER_CANCELLED, member_id="m1", round_=1, status=EventStatus.CANCELLED),
    ]
    counters = CounterRebuilder.from_events(events)
    assert counters.completed_messages_by_member == {"m1": 1}
    assert counters.total_messages_completed == 1


def test_terminal_reason_extracted() -> None:
    events = [
        _ev(1, EventType.RUN_COMPLETED, status=EventStatus.COMPLETED,
            payload={"reason": "limits_exhausted"}),
    ]
    counters = CounterRebuilder.from_events(events)
    assert counters.terminal_reason == "limits_exhausted"


def test_empty_log_yields_zero_counters() -> None:
    counters = CounterRebuilder.from_events([])
    assert counters.completed_messages_by_member == {}
    assert counters.total_messages_completed == 0
    assert counters.round_index == 0
    assert counters.terminal_reason is None
