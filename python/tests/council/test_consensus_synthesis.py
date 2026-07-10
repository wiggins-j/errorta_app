"""F-consensus — finalization mode 'consensus_report' writes a synthesized
'Consensus' final answer once the council converges."""
from __future__ import annotations

import asyncio

import pytest

from errorta_council.engine import build_and_run
from errorta_council.gateway_local import (
    LocalCouncilModelRequest,
    LocalCouncilModelResult,
    LocalGateway,
)
from errorta_council.limits import SchedulerPolicy
from errorta_council.run_store import RunStore
from errorta_council.schema import EventType

SYNTH_SENTINEL = "CONSOLIDATED COUNCIL CONSENSUS ANSWER"
MEMBER_LINE = "We agree: drink water and rest. No changed views."


class _ConsensusGateway(LocalGateway):
    """Members converge immediately (prose no-change marker); the synthesis
    turn (metadata.synthesis == 'consensus') returns a distinct answer."""

    def __init__(self, *, fail_synthesis: bool = False) -> None:
        super().__init__()
        self.fail_synthesis = fail_synthesis
        self.synthesis_calls = 0

    async def call(self, request: LocalCouncilModelRequest) -> LocalCouncilModelResult:
        if request.metadata.get("synthesis") == "consensus":
            self.synthesis_calls += 1
            if self.fail_synthesis:
                raise RuntimeError("synthesizer boom")
            content = SYNTH_SENTINEL
        else:
            content = MEMBER_LINE
        return LocalCouncilModelResult(
            content=content, provider="fake", provider_class="local",
            model=request.model, input_tokens=None, output_tokens=None,
            duration_ms=1, raw_usage_available=False,
        )

    async def is_reachable(self) -> bool:
        return True


class _FakeMeta:
    async def is_reachable(self) -> bool:
        return True

    async def list_installed_models(self) -> list[str]:
        return ["stub-model"]


def _room(*, mode: str):
    def member(mid):
        return {
            "id": mid, "enabled": True, "role": "member",
            "provider": "fake", "model": "stub-model",
            "context_access": "prompt_only", "transcript_access": "all_messages",
            "gateway_route_id": "fake.local.deterministic",
        }
    return {
        "id": "rm-consensus",
        "context_access_ceiling": "full_context",
        "transcript_access_ceiling": "all_messages",
        "allow_full_context": True,
        "members": [member("m-1"), member("m-2")],
        "topology": {
            "kind": "consensus_deliberation", "max_rounds": 3,
            "max_messages_per_member": 3, "speaker_order": ["m-1", "m-2"],
        },
        "finalization_policy": {"mode": mode, "finalizer_member_id": None},
    }


def _final_answer(events):
    fa = [e for e in events if e.type == EventType.FINAL_ANSWER]
    return fa[-1] if fa else None


@pytest.mark.asyncio
async def test_consensus_report_emits_synthesized_consensus(
    tmp_errorta_home, runs_dir_path
) -> None:
    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(
        room_id="rm-consensus", room_snapshot=_room(mode="consensus_report"),
        prompt="i have a headache, what should i do?", corpus_ids=[],
    )
    gw = _ConsensusGateway()
    final = await asyncio.wait_for(
        build_and_run(
            run_store=store, run_meta=meta,
            policy=SchedulerPolicy(max_rounds=3, max_messages_per_member=3),
            gateway_meta=_FakeMeta(), hardware_scan_present=True, gateway=gw,
        ),
        timeout=10.0,
    )
    assert final.status == "completed"
    _, events = store.read_run(meta.id)
    reason = next(
        (e.payload or {}).get("reason") for e in events
        if e.type == EventType.RUN_COMPLETED
    )
    assert reason == "consensus_reached"
    assert gw.synthesis_calls == 1
    fa = _final_answer(events)
    assert fa is not None
    assert fa.payload["synthesis_mode"] == "consensus"
    assert fa.payload["content"] == SYNTH_SENTINEL  # synthesized, not a member's


@pytest.mark.asyncio
async def test_transcript_only_does_not_synthesize(
    tmp_errorta_home, runs_dir_path
) -> None:
    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(
        room_id="rm-consensus", room_snapshot=_room(mode="transcript_only"),
        prompt="q", corpus_ids=[],
    )
    gw = _ConsensusGateway()
    await asyncio.wait_for(
        build_and_run(
            run_store=store, run_meta=meta,
            policy=SchedulerPolicy(max_rounds=3, max_messages_per_member=3),
            gateway_meta=_FakeMeta(), hardware_scan_present=True, gateway=gw,
        ),
        timeout=10.0,
    )
    _, events = store.read_run(meta.id)
    assert gw.synthesis_calls == 0
    fa = _final_answer(events)
    assert fa is not None
    assert "synthesis_mode" not in fa.payload
    assert fa.payload["content"] == MEMBER_LINE  # answer-of-record, verbatim


@pytest.mark.asyncio
async def test_synthesis_failure_falls_back_to_answer_of_record(
    tmp_errorta_home, runs_dir_path
) -> None:
    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(
        room_id="rm-consensus", room_snapshot=_room(mode="consensus_report"),
        prompt="q", corpus_ids=[],
    )
    gw = _ConsensusGateway(fail_synthesis=True)
    final = await asyncio.wait_for(
        build_and_run(
            run_store=store, run_meta=meta,
            policy=SchedulerPolicy(max_rounds=3, max_messages_per_member=3),
            gateway_meta=_FakeMeta(), hardware_scan_present=True, gateway=gw,
        ),
        timeout=10.0,
    )
    # Fail-soft: the run still completes and surfaces the answer-of-record.
    assert final.status == "completed"
    _, events = store.read_run(meta.id)
    fa = _final_answer(events)
    assert fa is not None
    assert "synthesis_mode" not in fa.payload
    assert fa.payload["content"] == MEMBER_LINE
