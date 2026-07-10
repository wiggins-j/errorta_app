"""F064 — the FINAL_ANSWER of a consensus run carries who/threshold/round."""
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

AGREE_LINE = "We agree: drink water and rest. No changed views."


class _Gateway(LocalGateway):
    """Members always signal no-change → consensus on round 1; synthesizer
    writes the shared answer."""

    def __init__(self, *, agree: bool) -> None:
        super().__init__()
        self._agree = agree
        self._round = 0

    async def call(self, request: LocalCouncilModelRequest) -> LocalCouncilModelResult:
        if request.metadata.get("synthesis") == "consensus":
            content = "SHARED CONSENSUS ANSWER"
        elif self._agree:
            content = AGREE_LINE
        else:
            # Always "refining" — never a no-change signal → never consensus.
            self._round += 1
            content = f"Revised take #{self._round}; still reconsidering."
        return LocalCouncilModelResult(
            content=content, provider="fake", provider_class="local",
            model=request.model, input_tokens=None, output_tokens=None,
            duration_ms=1, raw_usage_available=False,
        )

    async def is_reachable(self) -> bool:
        return True


class _Meta:
    async def is_reachable(self) -> bool:
        return True

    async def list_installed_models(self) -> list[str]:
        return ["stub-model"]


def _room():
    def member(mid):
        return {
            "id": mid, "enabled": True, "role": "member",
            "provider": "fake", "model": "stub-model",
            "context_access": "prompt_only", "transcript_access": "all_messages",
            "gateway_route_id": "fake.local.deterministic",
        }
    return {
        "id": "rm-cd", "context_access_ceiling": "full_context",
        "transcript_access_ceiling": "all_messages", "allow_full_context": True,
        "members": [member("m-1"), member("m-2")],
        "topology": {"kind": "consensus_deliberation", "max_rounds": 2,
                     "max_messages_per_member": 2, "speaker_order": ["m-1", "m-2"]},
        "finalization_policy": {"mode": "consensus_report", "finalizer_member_id": None},
    }


def _final_answer(events):
    fa = [e for e in events if e.type == EventType.FINAL_ANSWER]
    return fa[-1] if fa else None


@pytest.mark.asyncio
async def test_consensus_final_answer_carries_detail(tmp_errorta_home, runs_dir_path):
    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(room_id="rm-cd", room_snapshot=_room(),
                            prompt="q", corpus_ids=[])
    final = await asyncio.wait_for(
        build_and_run(
            run_store=store, run_meta=meta,
            policy=SchedulerPolicy(max_rounds=2, max_messages_per_member=2),
            gateway_meta=_Meta(), hardware_scan_present=True,
            gateway=_Gateway(agree=True),
        ),
        timeout=10.0,
    )
    assert final.status == "completed"
    _, events = store.read_run(meta.id)
    fa = _final_answer(events)
    assert fa is not None
    consensus = fa.payload.get("consensus")
    assert consensus is not None, "consensus detail missing from FINAL_ANSWER"
    assert sorted(consensus["agreed_member_ids"]) == ["m-1", "m-2"]
    assert consensus["threshold"] == 2
    assert consensus["member_count"] == 2
    assert consensus["round"] == 1


@pytest.mark.asyncio
async def test_round_limit_run_has_no_consensus_detail(tmp_errorta_home, runs_dir_path):
    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(room_id="rm-cd", room_snapshot=_room(),
                            prompt="q", corpus_ids=[])
    final = await asyncio.wait_for(
        build_and_run(
            run_store=store, run_meta=meta,
            policy=SchedulerPolicy(max_rounds=2, max_messages_per_member=2),
            gateway_meta=_Meta(), hardware_scan_present=True,
            gateway=_Gateway(agree=False),  # never converges
        ),
        timeout=10.0,
    )
    assert final.status == "completed"
    _, events = store.read_run(meta.id)
    reason = next((e.payload or {}).get("reason") for e in events
                  if e.type == EventType.RUN_COMPLETED)
    assert reason != "consensus_reached"
    fa = _final_answer(events)
    if fa is not None:
        assert "consensus" not in fa.payload
