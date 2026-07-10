"""F049 slice 3 — end-to-end: a mid-run user interjection reaches the NEXT
member (and not the ones who already spoke).

Mirrors the spec scenario:
    Mem1 ; Mem2 ; <user adds message> ; Mem3 sees it.

The capturing gateway pushes the interjection into the run's pending-control
queue DURING member 2's call (exactly what the route does when the scheduler
holds the writer). The scheduler drains it at the top of member 3's turn, so
member 3's context carries it while members 1 & 2's did not.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from errorta_council.engine import build_and_run
from errorta_council.gateway_local import (
    LocalCouncilModelRequest,
    LocalCouncilModelResult,
    LocalGateway,
)
from errorta_council.limits import SchedulerPolicy
from errorta_council.run_store import RunStore

INTERJECTION = "ZQ_INTERJECT_v1 please optimize for memory, not speed"


class _InjectingGateway(LocalGateway):
    """Records requests; pushes a user interjection during member 2's call."""

    def __init__(self, store: RunStore, run_id: str) -> None:
        super().__init__()
        self.requests: list[LocalCouncilModelRequest] = []
        self._store = store
        self._run_id = run_id

    async def call(self, request: LocalCouncilModelRequest) -> LocalCouncilModelResult:
        self.requests.append(request)
        if request.metadata.get("member_id") == "m2":
            # The user types a message while member 2 is speaking.
            self._store.push_pending_control_event(
                self._run_id,
                event_spec={
                    "type": "user_interjection",
                    "status": "completed",
                    "payload": {"content": INTERJECTION, "author": "user",
                                "requested_by": "user"},
                },
            )
        return LocalCouncilModelResult(
            content=f"ANSWER_FROM_{request.metadata.get('member_id', '?')}",
            provider="fake", provider_class="local", model=request.model,
            input_tokens=None, output_tokens=None,
            duration_ms=0, raw_usage_available=False,
        )

    async def is_reachable(self) -> bool:
        return False


class _FakeGatewayMeta:
    async def is_reachable(self) -> bool:
        return True

    async def list_installed_models(self) -> list[str]:
        return ["stub-model"]


def _bytes(req: LocalCouncilModelRequest) -> bytes:
    return json.dumps({"messages": req.messages}, sort_keys=True).encode("utf-8")


@pytest.mark.asyncio
async def test_interjection_reaches_next_member_only(tmp_errorta_home, runs_dir_path):
    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(
        room_id="rm-int",
        room_snapshot={
            "id": "rm-int",
            "transcript_access_ceiling": "all_messages",
            "members": [
                {"id": "m1", "enabled": True, "role": "member", "provider": "fake",
                 "model": "stub-model", "transcript_access": "all_messages"},
                {"id": "m2", "enabled": True, "role": "member", "provider": "fake",
                 "model": "stub-model", "transcript_access": "all_messages"},
                {"id": "m3", "enabled": True, "role": "member", "provider": "fake",
                 "model": "stub-model", "transcript_access": "all_messages"},
            ],
        },
        prompt="Design a cache",
        corpus_ids=[],
    )
    gw = _InjectingGateway(store, meta.id)
    final = await asyncio.wait_for(
        build_and_run(
            run_store=store, run_meta=meta,
            policy=SchedulerPolicy(max_rounds=1, max_messages_per_member=1,
                                   per_turn_timeout_seconds=5),
            gateway_meta=_FakeGatewayMeta(),
            hardware_scan_present=True,
            gateway=gw,
        ),
        timeout=5.0,
    )
    assert final.status == "completed"

    by_member = {r.metadata.get("member_id"): _bytes(r) for r in gw.requests}
    sentinel = INTERJECTION.encode("utf-8")
    # The next member to speak after the interjection sees it...
    assert sentinel in by_member["m3"], "m3 (next member) must see the interjection"
    # ...but the members who already spoke did not (read-once causality).
    assert sentinel not in by_member["m1"]
    assert sentinel not in by_member["m2"]

    # And it is durably recorded as a USER_INTERJECTION transcript event.
    from errorta_council.schema import EventType
    _, events = store.read_run(meta.id)
    interjections = [e for e in events if e.type == EventType.USER_INTERJECTION]
    assert len(interjections) == 1
    assert interjections[0].payload["content"] == INTERJECTION
    # m3 spoke after it -> delivered -> no "undelivered" diagnostic note.
    notes = [e for e in events if e.type == EventType.DIAGNOSTIC_NOTE
             and (e.payload or {}).get("note") == "interjections_after_final_turn"]
    assert notes == []


class _LateInjectGateway(LocalGateway):
    """Injects during the LAST member's call — no member speaks after it."""

    def __init__(self, store: RunStore, run_id: str, last_member: str) -> None:
        super().__init__()
        self._store = store
        self._run_id = run_id
        self._last = last_member

    async def call(self, request: LocalCouncilModelRequest) -> LocalCouncilModelResult:
        if request.metadata.get("member_id") == self._last:
            self._store.push_pending_control_event(
                self._run_id,
                event_spec={"type": "user_interjection", "status": "completed",
                            "payload": {"content": "too late", "author": "user"}},
            )
        return LocalCouncilModelResult(
            content="ok", provider="fake", provider_class="local",
            model=request.model, input_tokens=None, output_tokens=None,
            duration_ms=0, raw_usage_available=False)

    async def is_reachable(self) -> bool:
        return False


@pytest.mark.asyncio
async def test_interjection_after_final_turn_is_flagged(tmp_errorta_home, runs_dir_path):
    # An interjection that arrives during the last member's turn is recorded but
    # no member consumes it -> the run emits an honest DIAGNOSTIC_NOTE instead of
    # silently swallowing it.
    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(
        room_id="rm-late",
        room_snapshot={
            "id": "rm-late",
            "members": [
                {"id": "only", "enabled": True, "role": "member", "provider": "fake",
                 "model": "stub-model", "transcript_access": "all_messages"},
            ],
        },
        prompt="one shot",
        corpus_ids=[],
    )
    gw = _LateInjectGateway(store, meta.id, "only")
    final = await asyncio.wait_for(
        build_and_run(
            run_store=store, run_meta=meta,
            policy=SchedulerPolicy(max_rounds=1, max_messages_per_member=1,
                                   per_turn_timeout_seconds=5),
            gateway_meta=_FakeGatewayMeta(),
            hardware_scan_present=True,
            gateway=gw,
        ),
        timeout=5.0,
    )
    assert final.status == "completed"
    from errorta_council.schema import EventType
    _, events = store.read_run(meta.id)
    notes = [e for e in events if e.type == EventType.DIAGNOSTIC_NOTE
             and (e.payload or {}).get("note") == "interjections_after_final_turn"]
    assert len(notes) == 1
    assert notes[0].payload["count"] == 1
