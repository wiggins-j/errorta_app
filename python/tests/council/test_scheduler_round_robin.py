from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from errorta_council.control import RunControl
from errorta_council.limits import SchedulerPolicy
from errorta_council.run_store import RunStore
from errorta_council.scheduler import TurnScheduler
from errorta_council.schema import EventType
from errorta_council.topologies.round_robin import RoundRobinTopology


@dataclass
class _StubResult:
    content: str = "stub"
    provider: str = "fake"
    provider_class: str = "local"
    model: str = "stub-model"
    input_tokens: int | None = 10
    output_tokens: int | None = 5
    duration_ms: int = 1
    raw_usage_available: bool = True
    is_thinking_burn: bool = False


class _StubGateway:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def call(self, request: Any) -> _StubResult:
        self.calls.append(request)
        return _StubResult(content=f"from-{request.model}")

    async def is_reachable(self) -> bool:
        return True

    async def list_installed_models(self) -> list[str]:
        return ["stub-model"]


class _StubContextBuilder:
    async def build(self, *, run_meta: Any, member: dict, transcript: list) -> dict:
        return {
            "context_id": f"ctx-{member['id']}",
            "messages": [{"role": "user", "content": run_meta.prompt}],
        }


class _StubResourceGuard:
    async def admit(self, *, proposal: Any, member: dict) -> Any:
        from errorta_council.resources import AdmissionResult
        return AdmissionResult(
            admitted=True, classification="fits", reason_code=None, warnings=[]
        )

    def release(self, turn_id: str) -> None:
        pass


@pytest.mark.asyncio
async def test_two_member_one_round_runs_to_completion(tmp_errorta_home, runs_dir_path) -> None:
    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(
        room_id="rm",
        room_snapshot={
            "id": "rm",
            "members": [
                {"id": "m1", "enabled": True, "role": "member", "model": "stub-model", "provider": "fake"},
                {"id": "m2", "enabled": True, "role": "member", "model": "stub-model", "provider": "fake"},
            ],
        },
        prompt="hi",
        corpus_ids=[],
    )
    policy = SchedulerPolicy(max_rounds=1, per_turn_timeout_seconds=5)
    control = RunControl(run_store=store, run_id=meta.id)
    gateway = _StubGateway()
    sched = TurnScheduler(
        run_store=store,
        run_meta=meta,
        topology=RoundRobinTopology(),
        context_builder=_StubContextBuilder(),
        resource_guard=_StubResourceGuard(),
        gateway=gateway,
        control=control,
        policy=policy,
    )
    final = await asyncio.wait_for(sched.run(), timeout=2.0)
    assert final.status == "completed"
    _, events = store.read_run(meta.id)
    member_msgs = [e for e in events if e.type == EventType.MEMBER_MESSAGE]
    assert [e.member_id for e in member_msgs] == ["m1", "m2"]
    assert events[-1].type == EventType.RUN_COMPLETED

    # A FINAL_ANSWER event is emitted just before RUN_COMPLETED, carrying the
    # answer-of-record (the last member message, since no finalizer here).
    final_answers = [e for e in events if e.type == EventType.FINAL_ANSWER]
    assert len(final_answers) == 1, "exactly one FINAL_ANSWER on clean completion"
    fa = final_answers[0]
    assert fa.payload["content"] == "from-stub-model"
    assert fa.payload["member_id"] == "m2"  # last member to speak
    assert events[-2].type == EventType.FINAL_ANSWER  # immediately before terminal


@pytest.mark.asyncio
async def test_finalizer_member_message_is_the_final_answer(
    tmp_errorta_home, runs_dir_path
) -> None:
    """When a finalizer is configured, its message is the FINAL_ANSWER even if
    a non-finalizer speaks afterward in the same round."""
    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(
        room_id="rm-fin",
        room_snapshot={
            "id": "rm-fin",
            "finalization_policy": {"finalizer_member_id": "m1"},
            "members": [
                {"id": "m1", "enabled": True, "role": "finalizer",
                 "model": "fin-model", "provider": "fake"},
                {"id": "m2", "enabled": True, "role": "member",
                 "model": "stub-model", "provider": "fake"},
            ],
        },
        prompt="hi",
        corpus_ids=[],
    )
    policy = SchedulerPolicy(max_rounds=1, per_turn_timeout_seconds=5)
    control = RunControl(run_store=store, run_id=meta.id)
    sched = TurnScheduler(
        run_store=store,
        run_meta=meta,
        topology=RoundRobinTopology(),
        context_builder=_StubContextBuilder(),
        resource_guard=_StubResourceGuard(),
        gateway=_StubGateway(),
        control=control,
        policy=policy,
    )
    final = await asyncio.wait_for(sched.run(), timeout=2.0)
    assert final.status == "completed"
    _, events = store.read_run(meta.id)
    final_answers = [e for e in events if e.type == EventType.FINAL_ANSWER]
    assert len(final_answers) == 1
    # m1 is the finalizer; its content must win even though m2 spoke after it.
    assert final_answers[0].payload["member_id"] == "m1"
    assert final_answers[0].payload["content"] == "from-fin-model"


@pytest.mark.asyncio
async def test_max_messages_per_member_stops_run(tmp_errorta_home, runs_dir_path) -> None:
    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(
        room_id="rm",
        room_snapshot={
            "id": "rm",
            "members": [
                {"id": "m1", "enabled": True, "role": "member", "model": "stub-model", "provider": "fake"},
            ],
        },
        prompt="hi",
        corpus_ids=[],
    )
    policy = SchedulerPolicy(
        max_rounds=10, max_messages_per_member=1, per_turn_timeout_seconds=5
    )
    control = RunControl(run_store=store, run_id=meta.id)
    sched = TurnScheduler(
        run_store=store,
        run_meta=meta,
        topology=RoundRobinTopology(),
        context_builder=_StubContextBuilder(),
        resource_guard=_StubResourceGuard(),
        gateway=_StubGateway(),
        control=control,
        policy=policy,
    )
    final = await asyncio.wait_for(sched.run(), timeout=2.0)
    assert final.status == "completed"
    assert final.terminal_reason == "limits_exhausted"
    _, events = store.read_run(meta.id)
    assert sum(1 for e in events if e.type == EventType.MEMBER_MESSAGE) == 1


@pytest.mark.asyncio
async def test_cancel_before_first_turn_yields_cancelled_terminal(tmp_errorta_home, runs_dir_path) -> None:
    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(
        room_id="rm",
        room_snapshot={
            "id": "rm",
            "members": [
                {"id": "m1", "enabled": True, "role": "member", "model": "stub-model", "provider": "fake"},
            ],
        },
        prompt="hi",
        corpus_ids=[],
    )
    policy = SchedulerPolicy(max_rounds=2, per_turn_timeout_seconds=5)
    control = RunControl(run_store=store, run_id=meta.id)
    await control.request_cancel(requested_by="user:1", reason="user_action")
    sched = TurnScheduler(
        run_store=store,
        run_meta=meta,
        topology=RoundRobinTopology(),
        context_builder=_StubContextBuilder(),
        resource_guard=_StubResourceGuard(),
        gateway=_StubGateway(),
        control=control,
        policy=policy,
    )
    final = await asyncio.wait_for(sched.run(), timeout=2.0)
    assert final.status == "cancelled"
    _, events = store.read_run(meta.id)
    assert not any(e.type == EventType.MEMBER_MESSAGE for e in events)
    assert events[-1].type == EventType.RUN_CANCELLED
