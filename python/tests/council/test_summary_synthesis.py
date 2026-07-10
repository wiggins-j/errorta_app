"""F031-28 — finalization mode 'summary' writes an abstractive rapporteur summary.

Unlike consensus_report, summary fires on ANY terminal reason (here a round_robin
run that hits the round cap WITHOUT converging), so a non-consensus run still gets
a faithful summary tagged synthesis_mode='summary'. Fail-soft like consensus.
"""
from __future__ import annotations

import asyncio
import hashlib
import json

import pytest

from errorta_council.context.manifest_store import ContextManifestStore
from errorta_council.context.router import ContextRouter
from errorta_council.context.transforms.schema import SourceEnvelope, TransformResult
from errorta_council.engine import build_and_run
from errorta_council.gateway_local import (
    LocalCouncilModelRequest,
    LocalCouncilModelResult,
    LocalGateway,
)
from errorta_council.limits import SchedulerPolicy
from errorta_council.run_store import RunStore
from errorta_council.schema import EventType

SUMMARY_SENTINEL = "RAPPORTEUR SUMMARY: most said A, one argued B."
MEMBER_LINE = "I think the answer is A."
CORPUS_SENTINEL = (
    "ZQ_SUMMARY_FINALIZER_CORPUS_SENTINEL: raw private propulsion note, "
    "classification=ITAR_RESTRICTED_FAKE"
)


class _SummaryGateway(LocalGateway):
    """Members give a plain answer (no consensus signal -> the round_robin run
    ends at the cap, NOT 'consensus_reached'); the summary turn
    (metadata.synthesis == 'summary') returns a distinct rapporteur answer."""

    def __init__(self, *, fail_synthesis: bool = False) -> None:
        super().__init__()
        self.fail_synthesis = fail_synthesis
        self.summary_calls = 0
        self.summary_requests: list[LocalCouncilModelRequest] = []

    async def call(self, request: LocalCouncilModelRequest) -> LocalCouncilModelResult:
        if request.metadata.get("synthesis") == "summary":
            self.summary_calls += 1
            self.summary_requests.append(request)
            if self.fail_synthesis:
                raise RuntimeError("rapporteur boom")
            content = SUMMARY_SENTINEL
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


class _AlwaysSentinelRetrieval:
    def fetch(self, *, member_id, prompt, corpus_ids, transcript_cursor, top_k=8):
        return [
            SourceEnvelope(
                class_="retrieved_snippet",
                corpus_id="aerospace",
                chunk_id="ch-001",
                citation_id="ct-001",
                content=CORPUS_SENTINEL,
                content_sha256=hashlib.sha256(CORPUS_SENTINEL.encode()).hexdigest(),
                tokens=len(CORPUS_SENTINEL.split()),
                sensitivity="may_contain_corpus",
            )
        ]


class _RedactingTransforms:
    def __init__(self) -> None:
        self.calls = []

    async def transform(self, request):
        self.calls.append(request)
        redacted = "Summary: private propulsion note referenced; details redacted."
        return TransformResult(
            status="allowed",
            artifact_id="summary-finalizer-redacted",
            artifact_kind="redacted_summary",
            content=redacted,
            content_sha256=hashlib.sha256(redacted.encode()).hexdigest(),
            egress_class="local",
            destination_scope=request.destination_scope,
            token_estimate={"input": 8, "output": 8},
            manifest_id="tm-summary-finalizer-redacted",
            blocked_reason=None,
            message_code=None,
            warnings=[],
        )


def _room(*, mode: str, context_access: str = "prompt_only"):
    def member(mid):
        return {
            "id": mid, "enabled": True, "role": "member",
            "provider": "fake", "model": "stub-model",
            "context_access": context_access, "transcript_access": "all_messages",
            "gateway_route_id": "fake.local.deterministic",
        }
    return {
        "id": "rm-summary",
        "context_access_ceiling": "full_context",
        "transcript_access_ceiling": "all_messages",
        "allow_full_context": True,
        "members": [member("m-1"), member("m-2")],
        # round_robin so the run ends at the round cap, NOT on consensus.
        "topology": {
            "kind": "round_robin", "max_rounds": 1,
            "max_messages_per_member": 1, "speaker_order": ["m-1", "m-2"],
        },
        "finalization_policy": {"mode": mode, "finalizer_member_id": None},
    }


def _context_router(store: RunStore, meta, manifest_root, transforms):
    room = dict(meta.room_snapshot or {})

    def load(run_id: str):
        try:
            _, events = store.read_run(run_id)
        except Exception:
            events = []
        return {
            "run_id": run_id,
            "events": [e.to_dict() for e in events],
            "members": [
                {
                    **dict(m),
                    "member_id": m.get("id"),
                    "provider_class": "fake",
                }
                for m in room.get("members", [])
            ],
            "room": {
                "context_access_ceiling": room.get(
                    "context_access_ceiling", "full_context"
                ),
                "transcript_access_ceiling": room.get(
                    "transcript_access_ceiling", "all_messages"
                ),
                "allow_full_context": room.get("allow_full_context", True),
            },
            "topology": {
                "context_access_ceiling": room.get(
                    "context_access_ceiling", "full_context"
                ),
                "transcript_access_ceiling": room.get(
                    "transcript_access_ceiling", "all_messages"
                ),
            },
            "residency": {"destination_scope": "local"},
            "corpus_policy": {"max_egress_class": "remote_eligible"},
        }

    return ContextRouter(
        retrieval=_AlwaysSentinelRetrieval(),
        transforms=transforms,
        manifest_store=ContextManifestStore(root=manifest_root),
        run_snapshot_loader=load,
    )


def _final_answer(events):
    fa = [e for e in events if e.type == EventType.FINAL_ANSWER]
    return fa[-1] if fa else None


def _reason(events):
    return next(
        ((e.payload or {}).get("reason") for e in events
         if e.type == EventType.RUN_COMPLETED),
        None,
    )


async def _run(store, meta, gw):
    return await asyncio.wait_for(
        build_and_run(
            run_store=store, run_meta=meta,
            policy=SchedulerPolicy(max_rounds=1, max_messages_per_member=1),
            gateway_meta=_FakeMeta(), hardware_scan_present=True, gateway=gw,
        ),
        timeout=10.0,
    )


async def _run_with_router(store, meta, gw, router):
    return await asyncio.wait_for(
        build_and_run(
            run_store=store, run_meta=meta,
            policy=SchedulerPolicy(max_rounds=1, max_messages_per_member=1),
            gateway_meta=_FakeMeta(), hardware_scan_present=True, gateway=gw,
            context_router=router,
        ),
        timeout=10.0,
    )


@pytest.mark.asyncio
async def test_summary_synthesizes_on_non_consensus_ending(
    tmp_errorta_home, runs_dir_path
) -> None:
    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(
        room_id="rm-summary", room_snapshot=_room(mode="summary"),
        prompt="what is the answer?", corpus_ids=[],
    )
    gw = _SummaryGateway()
    final = await _run(store, meta, gw)
    assert final.status == "completed"
    _, events = store.read_run(meta.id)
    # The marquee: the run did NOT converge, yet summary still ran.
    assert _reason(events) != "consensus_reached"
    assert gw.summary_calls == 1
    fa = _final_answer(events)
    assert fa is not None
    assert fa.payload["synthesis_mode"] == "summary"
    assert fa.payload["content"] == SUMMARY_SENTINEL  # synthesized, not a member's


@pytest.mark.asyncio
async def test_summary_synthesis_uses_context_router_without_raw_corpus_leak(
    tmp_errorta_home, runs_dir_path, tmp_path
) -> None:
    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(
        room_id="rm-summary",
        room_snapshot=_room(mode="summary", context_access="redacted_summary"),
        prompt="what is the answer?",
        corpus_ids=["aerospace"],
    )
    transforms = _RedactingTransforms()
    gw = _SummaryGateway()
    final = await _run_with_router(
        store,
        meta,
        gw,
        _context_router(store, meta, tmp_path / "manifests", transforms),
    )
    assert final.status == "completed"
    assert gw.summary_calls == 1
    assert transforms.calls
    summary_blob = json.dumps(
        {
            "messages": gw.summary_requests[-1].messages,
            "metadata": gw.summary_requests[-1].metadata,
        },
        sort_keys=True,
    )
    assert CORPUS_SENTINEL not in summary_blob
    assert "private propulsion note referenced; details redacted" in summary_blob


@pytest.mark.asyncio
async def test_summary_failure_falls_back_to_answer_of_record(
    tmp_errorta_home, runs_dir_path
) -> None:
    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(
        room_id="rm-summary", room_snapshot=_room(mode="summary"),
        prompt="q", corpus_ids=[],
    )
    gw = _SummaryGateway(fail_synthesis=True)
    final = await _run(store, meta, gw)
    assert final.status == "completed"  # fail-soft
    _, events = store.read_run(meta.id)
    fa = _final_answer(events)
    assert fa is not None
    assert "synthesis_mode" not in fa.payload
    assert fa.payload["content"] == MEMBER_LINE  # answer-of-record, verbatim


@pytest.mark.asyncio
async def test_transcript_only_does_not_summarize(
    tmp_errorta_home, runs_dir_path
) -> None:
    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(
        room_id="rm-summary", room_snapshot=_room(mode="transcript_only"),
        prompt="q", corpus_ids=[],
    )
    gw = _SummaryGateway()
    await _run(store, meta, gw)
    _, events = store.read_run(meta.id)
    assert gw.summary_calls == 0
    fa = _final_answer(events)
    assert fa is not None
    assert "synthesis_mode" not in fa.payload
