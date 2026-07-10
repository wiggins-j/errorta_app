"""F031-09 ask/decision end-to-end through the scheduler.

Locks the post-review P1 gap: submit_decision is durable AND the scheduler
actually observes the projection — entering awaiting_user_decision when
admission blocks under stop_behavior="ask", then resuming when the
decision arrives.
"""
from __future__ import annotations

import asyncio
import threading

import pytest

from errorta_council.control import RunControl
from errorta_council.limits import SchedulerPolicy
from errorta_council.local_context import LocalContextBuilder
from errorta_council.resources import AdmissionResult
from errorta_council.run_store import RunStore
from errorta_council.scheduler import TurnScheduler
from errorta_council.schema import EventType, EventStatus
from errorta_council.topologies.round_robin import RoundRobinTopology


class _AlwaysBlockedGuard:
    """Resource guard whose admit() always returns admitted=False.

    Forces the scheduler into the "ask" branch on the very first proposal.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def admit(self, *, proposal, member):
        self.calls += 1
        return AdmissionResult(
            admitted=False, classification="unavailable",
            reason_code="local_model_missing", warnings=[],
        )

    def release(self, turn_id: str) -> None:
        return None


class _FakeGateway:
    async def is_reachable(self): return True
    async def list_installed_models(self): return []
    async def call(self, request):
        raise AssertionError("gateway must not be called when admission blocks")


def _seed_run(store: RunStore):
    return store.create_run(
        room_id="rm",
        room_snapshot={
            "id": "rm",
            "members": [
                {"id": "m1", "enabled": True, "role": "member",
                 "model": "stub-model", "provider": "fake"},
                {"id": "m2", "enabled": True, "role": "member",
                 "model": "stub-model", "provider": "fake"},
            ],
        },
        prompt="hi",
        corpus_ids=[],
    )


@pytest.mark.asyncio
async def test_ask_branch_enters_awaiting_user_decision_and_resumes_on_skip(
    tmp_errorta_home, runs_dir_path
) -> None:
    """The marquee P1-end-to-end: ask → durable awaiting state → decision → resume."""
    store = RunStore(runs_dir=runs_dir_path)
    meta = _seed_run(store)
    policy = SchedulerPolicy(
        max_rounds=1, max_messages_per_member=1,
        per_turn_timeout_seconds=2, stop_behavior="ask",
    )
    control = RunControl(run_store=store, run_id=meta.id)
    sched = TurnScheduler(
        run_store=store,
        run_meta=meta,
        topology=RoundRobinTopology(),
        context_builder=LocalContextBuilder(max_input_chars=4096),
        resource_guard=_AlwaysBlockedGuard(),
        gateway=_FakeGateway(),
        control=control,
        policy=policy,
    )

    sched_task = asyncio.create_task(sched.run())

    # Poll until the run is durably awaiting a user decision.
    for _ in range(100):
        fresh, _events = store.read_run(meta.id)
        if fresh.status == "awaiting_user_decision":
            break
        await asyncio.sleep(0.02)
    else:
        sched_task.cancel()
        raise AssertionError("run never entered awaiting_user_decision")

    # The status-change event must have been emitted under the scheduler's
    # writer token (i.e. carries the right shape and round/member).
    _, events = store.read_run(meta.id)
    awaiting_events = [
        e for e in events
        if e.type == EventType.RUN_STATUS_CHANGED
        and e.status == EventStatus.AWAITING_USER_DECISION
    ]
    assert len(awaiting_events) == 1
    awaiting = awaiting_events[0]
    assert awaiting.payload.get("status_change") == "awaiting_user_decision"
    assert awaiting.payload.get("reason_code") == "local_model_missing"

    # Submit a "skip remainder_of_run" decision: the scheduler should consume
    # it, clear last_decision, and terminate as completed/limits_exhausted.
    await control.submit_decision(
        choice="skip_member", scope="remainder_of_run", requested_by="user:1",
    )

    final = await asyncio.wait_for(sched_task, timeout=2.0)
    assert final.status == "completed"
    fresh, _ = store.read_run(meta.id)
    assert fresh.last_decision is None, "consumed decision should be cleared"
    # Status returns to "completed" (terminal) — not stuck in awaiting.
    assert fresh.status == "completed"


@pytest.mark.asyncio
async def test_ask_branch_stop_choice_cancels_run(
    tmp_errorta_home, runs_dir_path
) -> None:
    """choice="stop" durably triggers terminal cancellation."""
    store = RunStore(runs_dir=runs_dir_path)
    meta = _seed_run(store)
    policy = SchedulerPolicy(
        max_rounds=1, max_messages_per_member=1,
        per_turn_timeout_seconds=2, stop_behavior="ask",
    )
    control = RunControl(run_store=store, run_id=meta.id)
    sched = TurnScheduler(
        run_store=store,
        run_meta=meta,
        topology=RoundRobinTopology(),
        context_builder=LocalContextBuilder(max_input_chars=4096),
        resource_guard=_AlwaysBlockedGuard(),
        gateway=_FakeGateway(),
        control=control,
        policy=policy,
    )

    sched_task = asyncio.create_task(sched.run())
    for _ in range(100):
        fresh, _ev = store.read_run(meta.id)
        if fresh.status == "awaiting_user_decision":
            break
        await asyncio.sleep(0.02)
    else:
        sched_task.cancel()
        raise AssertionError("run never entered awaiting_user_decision")

    await control.submit_decision(
        choice="stop", scope="remainder_of_run", requested_by="user:1",
    )
    final = await asyncio.wait_for(sched_task, timeout=2.0)
    assert final.status == "cancelled"
