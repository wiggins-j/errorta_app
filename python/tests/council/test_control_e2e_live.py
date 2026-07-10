"""End-to-end control tests against a live scheduler thread.

Locks the post-review P1 gaps:
- Route-issued pause actually halts an in-flight run; resume releases it.
- Route-issued cancel/decision events show up in the transcript via the
  scheduler's pending-control-event drain (P1 — durable audit trail).
"""
from __future__ import annotations

import asyncio

import pytest

from errorta_council.control import RunControl
from errorta_council.limits import SchedulerPolicy
from errorta_council.local_context import LocalContextBuilder
from errorta_council.resources import AdmissionResult
from errorta_council.run_store import RunStore
from errorta_council.scheduler import TurnScheduler
from errorta_council.schema import EventStatus, EventType
from errorta_council.topologies.round_robin import RoundRobinTopology


class _SlowFakeGateway:
    """Each call() takes a configurable amount of wall time so the test
    can land control actions WHILE the scheduler is mid-turn."""

    def __init__(self, *, per_call_seconds: float = 0.2) -> None:
        self.calls = 0
        self._per_call_seconds = per_call_seconds

    async def is_reachable(self) -> bool:
        return True

    async def list_installed_models(self) -> list[str]:
        return ["stub-model"]

    async def call(self, request):
        from errorta_council.gateway_local import LocalCouncilModelResult
        self.calls += 1
        await asyncio.sleep(self._per_call_seconds)
        return LocalCouncilModelResult(
            content=f"reply-{self.calls}",
            provider="fake", provider_class="local",
            model=request.model,
            input_tokens=1, output_tokens=1,
            duration_ms=1, raw_usage_available=True,
        )


class _PassGuard:
    async def admit(self, *, proposal, member):
        return AdmissionResult(
            admitted=True, classification="fits", reason_code=None, warnings=[],
        )

    def release(self, turn_id: str) -> None:
        return None


def _seed_run(store: RunStore, *, member_count: int = 6):
    members = [
        {"id": f"m{i}", "enabled": True, "role": "member",
         "model": "stub-model", "provider": "fake"}
        for i in range(member_count)
    ]
    return store.create_run(
        room_id="rm",
        room_snapshot={"id": "rm", "members": members},
        prompt="hi", corpus_ids=[],
    )


@pytest.mark.asyncio
async def test_route_pause_actually_halts_live_run(
    tmp_errorta_home, runs_dir_path
) -> None:
    """P1: a route-issued pause stops the scheduler from emitting more turns."""
    store = RunStore(runs_dir=runs_dir_path)
    meta = _seed_run(store, member_count=6)
    policy = SchedulerPolicy(
        max_rounds=1, max_messages_per_member=1, per_turn_timeout_seconds=5,
    )
    sched_control = RunControl(run_store=store, run_id=meta.id)
    gw = _SlowFakeGateway(per_call_seconds=0.15)
    sched = TurnScheduler(
        run_store=store, run_meta=meta,
        topology=RoundRobinTopology(),
        context_builder=LocalContextBuilder(max_input_chars=4096),
        resource_guard=_PassGuard(),
        gateway=gw, control=sched_control, policy=policy,
    )
    sched_task = asyncio.create_task(sched.run())

    # Wait for one member message to land so we know the scheduler is alive.
    for _ in range(100):
        _, events = store.read_run(meta.id)
        if any(e.type == EventType.MEMBER_MESSAGE for e in events):
            break
        await asyncio.sleep(0.02)
    else:
        sched_task.cancel()
        raise AssertionError("scheduler never emitted a MEMBER_MESSAGE")

    # Route-side pause (a SEPARATE RunControl instance — like the FastAPI
    # route handler that constructs a fresh one per request).
    route_control = RunControl(run_store=store, run_id=meta.id)
    await route_control.request_pause(requested_by="user:1")

    # Give the scheduler enough time to react to the pause and quiesce.
    await asyncio.sleep(0.4)
    _, events_after_pause = store.read_run(meta.id)
    member_msgs_at_pause = sum(
        1 for e in events_after_pause if e.type == EventType.MEMBER_MESSAGE
    )

    # Wait again — the count must not have grown while paused.
    await asyncio.sleep(0.5)
    _, events_still_paused = store.read_run(meta.id)
    member_msgs_still_paused = sum(
        1 for e in events_still_paused if e.type == EventType.MEMBER_MESSAGE
    )
    assert member_msgs_still_paused == member_msgs_at_pause, (
        "scheduler kept emitting member messages after pause "
        f"({member_msgs_at_pause} → {member_msgs_still_paused})"
    )

    # Resume — scheduler proceeds and eventually terminates.
    await route_control.request_resume(requested_by="user:1")
    final = await asyncio.wait_for(sched_task, timeout=5.0)
    assert final.status == "completed"


@pytest.mark.asyncio
async def test_route_cancel_event_lands_in_transcript(
    tmp_errorta_home, runs_dir_path
) -> None:
    """P1: the cancel control event appears in the event log even when the
    scheduler held the writer at request time (pending-event queue drain)."""
    store = RunStore(runs_dir=runs_dir_path)
    meta = _seed_run(store, member_count=4)
    policy = SchedulerPolicy(
        max_rounds=1, max_messages_per_member=1, per_turn_timeout_seconds=5,
    )
    sched_control = RunControl(run_store=store, run_id=meta.id)
    gw = _SlowFakeGateway(per_call_seconds=0.15)
    sched = TurnScheduler(
        run_store=store, run_meta=meta,
        topology=RoundRobinTopology(),
        context_builder=LocalContextBuilder(max_input_chars=4096),
        resource_guard=_PassGuard(),
        gateway=gw, control=sched_control, policy=policy,
    )
    sched_task = asyncio.create_task(sched.run())

    # Wait for the scheduler to be alive.
    for _ in range(100):
        _, events = store.read_run(meta.id)
        if any(e.type == EventType.MEMBER_MESSAGE for e in events):
            break
        await asyncio.sleep(0.02)

    # Route-side cancel — fresh RunControl, scheduler holds the writer.
    route_control = RunControl(run_store=store, run_id=meta.id)
    new_meta, ev = await route_control.request_cancel(
        requested_by="user:1", reason="ui_stop_button",
    )
    # The route layer cannot append the event directly here — confirmed by
    # the meta-only fallback returning None.
    assert ev is None
    assert new_meta.cancel_requested_at is not None

    # Wait for terminal — the scheduler drains the pending event AND emits
    # the terminal RUN_CANCELLED.
    final = await asyncio.wait_for(sched_task, timeout=5.0)
    assert final.status == "cancelled"

    _, events = store.read_run(meta.id)
    types_statuses = [(e.type, e.status) for e in events]
    assert (EventType.RUN_CANCEL_REQUESTED, EventStatus.CANCEL_REQUESTED) in types_statuses, (
        "route-issued cancel must surface as RUN_CANCEL_REQUESTED in the transcript"
    )
    assert events[-1].type == EventType.RUN_CANCELLED


@pytest.mark.asyncio
async def test_route_pause_event_lands_in_transcript(
    tmp_errorta_home, runs_dir_path
) -> None:
    """P1: pause / resume control events appear in the transcript."""
    store = RunStore(runs_dir=runs_dir_path)
    meta = _seed_run(store, member_count=4)
    policy = SchedulerPolicy(
        max_rounds=1, max_messages_per_member=1, per_turn_timeout_seconds=5,
    )
    sched_control = RunControl(run_store=store, run_id=meta.id)
    gw = _SlowFakeGateway(per_call_seconds=0.15)
    sched = TurnScheduler(
        run_store=store, run_meta=meta,
        topology=RoundRobinTopology(),
        context_builder=LocalContextBuilder(max_input_chars=4096),
        resource_guard=_PassGuard(),
        gateway=gw, control=sched_control, policy=policy,
    )
    sched_task = asyncio.create_task(sched.run())

    for _ in range(100):
        _, events = store.read_run(meta.id)
        if any(e.type == EventType.MEMBER_MESSAGE for e in events):
            break
        await asyncio.sleep(0.02)

    route_control = RunControl(run_store=store, run_id=meta.id)
    await route_control.request_pause(requested_by="user:1")
    await asyncio.sleep(0.3)
    await route_control.request_resume(requested_by="user:1")

    final = await asyncio.wait_for(sched_task, timeout=5.0)
    assert final.status == "completed"
    _, events = store.read_run(meta.id)
    statuses = [
        e.status for e in events if e.type == EventType.RUN_STATUS_CHANGED
    ]
    assert EventStatus.PAUSED in statuses, "missing RUN_STATUS_CHANGED + PAUSED"
    assert EventStatus.RESUMED in statuses, "missing RUN_STATUS_CHANGED + RESUMED"
