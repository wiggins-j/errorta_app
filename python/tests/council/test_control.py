from __future__ import annotations

import asyncio

import pytest

from errorta_council.control import RunControl, TerminalRunError
from errorta_council.run_store import RunStore
from errorta_council.schema import EventStatus, EventType


@pytest.fixture
def seeded_run(tmp_errorta_home, runs_dir_path):
    """Create a fresh running run via the Phase 0 store."""
    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(
        room_id="rm",
        room_snapshot={"id": "rm", "members": []},
        prompt="hi",
        corpus_ids=[],
    )
    return store, meta


@pytest.mark.asyncio
async def test_pause_then_resume_is_idempotent(seeded_run) -> None:
    store, meta = seeded_run
    ctl = RunControl(run_store=store, run_id=meta.id)
    m1 = await ctl.request_pause(requested_by="user:1")
    assert m1.status == "paused"
    assert m1.paused_at is not None
    # Double pause: idempotent, no extra paused event.
    m2 = await ctl.request_pause(requested_by="user:1")
    assert m2.status == "paused"
    events = store.read_run(meta.id)[1]
    pause_events = [e for e in events if e.status == EventStatus.PAUSED]
    assert len(pause_events) == 1
    m3 = await ctl.request_resume(requested_by="user:1")
    assert m3.status == "running"
    assert m3.paused_at is None


@pytest.mark.asyncio
async def test_cancel_before_terminal_is_idempotent(seeded_run) -> None:
    store, meta = seeded_run
    ctl = RunControl(run_store=store, run_id=meta.id)
    m1, ev1 = await ctl.request_cancel(requested_by="user:1", reason="user_action")
    assert m1.cancel_requested_at is not None
    assert ev1.type == EventType.RUN_CANCEL_REQUESTED
    # Second cancel: no extra event, same meta.
    m2, ev2 = await ctl.request_cancel(requested_by="user:1", reason="user_action")
    assert ev2.sequence == ev1.sequence


@pytest.mark.asyncio
async def test_cancel_after_terminal_raises_409(seeded_run) -> None:
    store, meta = seeded_run
    # Force terminal via a transient writer token.
    token = store.acquire_writer(meta.id)
    try:
        store.append_event(
            meta.id,
            type=EventType.RUN_COMPLETED,
            status=EventStatus.COMPLETED,
            payload={"reason": "test"},
            writer=token,
        )
    finally:
        store.release_writer(token)
    ctl = RunControl(run_store=store, run_id=meta.id)
    with pytest.raises(TerminalRunError) as exc:
        await ctl.request_cancel(requested_by="user:1", reason="too_late")
    assert exc.value.http_status == 409


@pytest.mark.asyncio
async def test_await_unpaused_or_cancelled_returns_immediately_when_running(seeded_run) -> None:
    store, meta = seeded_run
    ctl = RunControl(run_store=store, run_id=meta.id)
    # Should not block.
    await ctl.await_unpaused_or_cancelled()


@pytest.mark.asyncio
async def test_await_unpaused_or_cancelled_returns_when_resumed(seeded_run) -> None:
    store, meta = seeded_run
    ctl = RunControl(run_store=store, run_id=meta.id)
    await ctl.request_pause(requested_by="user:1")

    async def resume_soon() -> None:
        await asyncio.sleep(0.05)
        await ctl.request_resume(requested_by="user:1")

    asyncio.create_task(resume_soon())
    await asyncio.wait_for(ctl.await_unpaused_or_cancelled(), timeout=1.0)


@pytest.mark.asyncio
async def test_submit_decision_records_choice_and_scope(seeded_run) -> None:
    store, meta = seeded_run
    # F031-09: park the run in awaiting_user_decision so the spec's
    # state-machine guard accepts the decision.
    store.merge_meta_fields(meta.id, status="awaiting_user_decision")
    ctl = RunControl(run_store=store, run_id=meta.id)
    m, ev = await ctl.submit_decision(
        choice="skip_member", scope="current_round", requested_by="user:1"
    )
    assert ev.type == EventType.RUN_STATUS_CHANGED
    assert ev.payload["decision"] == {"choice": "skip_member", "scope": "current_round"}


@pytest.mark.asyncio
async def test_submit_decision_unknown_choice_rejected(seeded_run) -> None:
    store, meta = seeded_run
    store.merge_meta_fields(meta.id, status="awaiting_user_decision")
    ctl = RunControl(run_store=store, run_id=meta.id)
    with pytest.raises(ValueError):
        await ctl.submit_decision(choice="explode", scope="current_turn", requested_by="user:1")


@pytest.mark.asyncio
async def test_submit_decision_rejected_outside_awaiting_user_decision(
    seeded_run,
) -> None:
    """F031-09 P2 lock — submit_decision raises DecisionNotApplicable
    when the run is not in awaiting_user_decision.
    """
    from errorta_council.control import DecisionNotApplicable

    store, meta = seeded_run
    ctl = RunControl(run_store=store, run_id=meta.id)
    with pytest.raises(DecisionNotApplicable):
        await ctl.submit_decision(
            choice="skip_member", scope="current_turn", requested_by="user:1",
        )
