"""P1 review finding lock — transcript content flows across rounds.

The scheduler writes ``MEMBER_MESSAGE`` events with
``payload["content"]`` (errorta_council/scheduler.py: see
``MEMBER_MESSAGE`` emission). The Phase 3 router previously read
``payload["text"]`` instead, so once the engine wired the router in,
round-2 members would receive an empty-string body for round-1's
output. This test asserts the round-2 member's transcript block carries
the actual prior-round content.

Scope: 2 members × 2 rounds, transcript_access=all_messages, with a
gateway that captures the messages the scheduler hands it.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from errorta_council.context.engine_adapter import RouterContextAdapter
from errorta_council.context.manifest_store import ContextManifestStore
from errorta_council.context.retrieval import RetrievalSeam
from errorta_council.context.router import ContextRouter
from errorta_council.context.transforms.pipeline import TransformPipeline
from errorta_council.context.transforms.redaction import (
    REDACTION_VERSION,
    RedactionPipeline,
)
from errorta_council.context.transforms.store import TransformStore
from errorta_council.context.transforms.summarization import SummaryPipeline
from errorta_council.engine import _build_snapshot_loader, build_and_run
from errorta_council.gateway_local import (
    LocalCouncilModelRequest,
    LocalCouncilModelResult,
    LocalGateway,
)
from errorta_council.limits import SchedulerPolicy
from errorta_council.paths import council_root
from errorta_council.run_store import RunStore


SENTINEL_M1_ROUND1 = "ZQ_M1_ROUND1_SAID_THIS_alpha42"


class _CaptureGateway(LocalGateway):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[LocalCouncilModelRequest] = []

    async def call(self, request):
        self.requests.append(request)
        # m-1's first turn says the sentinel; subsequent turns say anything.
        member_id = request.metadata.get("member_id")
        if (
            member_id == "m-1"
            and not any(r.metadata.get("member_id") == "m-1" for r in self.requests[:-1])
        ):
            content = SENTINEL_M1_ROUND1
        else:
            content = f"answer_from_{member_id}"
        return LocalCouncilModelResult(
            content=content,
            provider="fake", provider_class="local",
            model=request.model,
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


def _build_all_messages_router(*, run_store, run_meta, gateway) -> ContextRouter:
    """Router with transcript_access_ceiling=all_messages so transcript flows."""
    root = council_root()
    manifest_store = ContextManifestStore(root=root / "context-manifests")
    transform_store = TransformStore(root=root / "transforms")
    redaction = RedactionPipeline(version=REDACTION_VERSION)
    summary = SummaryPipeline(
        gateway=gateway, route_id="local.summary",
        allow_extractive_fallback=True,
    )
    transforms = TransformPipeline(
        redaction=redaction, summary=summary, store=transform_store,
    )
    base_loader = _build_snapshot_loader(run_store=run_store, run_meta=run_meta)

    def loader(run_id):
        snap = base_loader(run_id)
        snap["room"]["transcript_access_ceiling"] = "all_messages"
        snap["topology"]["transcript_access_ceiling"] = "all_messages"
        return snap

    return ContextRouter(
        retrieval=RetrievalSeam(pipeline=None),
        transforms=transforms,
        manifest_store=manifest_store,
        run_snapshot_loader=loader,
    )


@pytest.mark.asyncio
async def test_round_2_member_sees_round_1_member_message_content(
    tmp_errorta_home, runs_dir_path,
) -> None:
    """The marquee P1 review-finding lock for the payload-key fix."""
    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(
        room_id="rm-tx",
        room_snapshot={
            "id": "rm-tx",
            "context_access_ceiling": "full_context",
            "transcript_access_ceiling": "all_messages",
            "allow_full_context": True,
            "members": [
                {
                    "id": "m-1", "enabled": True, "role": "member",
                    "provider": "fake", "model": "stub-model",
                    "context_access": "prompt_only",
                    "transcript_access": "all_messages",
                    "gateway_route_id": "fake.local.deterministic",
                },
                {
                    "id": "m-2", "enabled": True, "role": "member",
                    "provider": "fake", "model": "stub-model",
                    "context_access": "prompt_only",
                    "transcript_access": "all_messages",
                    "gateway_route_id": "fake.local.deterministic",
                },
            ],
        },
        prompt="round-trip test",
        corpus_ids=[],
    )
    capture = _CaptureGateway()
    router = _build_all_messages_router(
        run_store=store, run_meta=meta, gateway=capture,
    )

    final = await asyncio.wait_for(
        build_and_run(
            run_store=store, run_meta=meta,
            policy=SchedulerPolicy(
                max_rounds=2, max_messages_per_member=2,
                per_turn_timeout_seconds=5,
            ),
            gateway_meta=_FakeGatewayMeta(), hardware_scan_present=True,
            gateway=capture, context_router=router,
        ),
        timeout=5.0,
    )
    assert final.status == "completed"
    # Sanity: 4 gateway calls (2 members × 2 rounds).
    assert len(capture.requests) == 4, [
        r.metadata.get("member_id") for r in capture.requests
    ]

    # m-2 round 2 is the 4th call (round_robin order m-1, m-2, m-1, m-2).
    m2_round2 = capture.requests[-1]
    assert m2_round2.metadata.get("member_id") == "m-2"
    msg_blob = json.dumps(m2_round2.messages, sort_keys=True)
    # The sentinel m-1 emitted in round 1 must be visible in m-2's round-2
    # context — proving the router reads payload.content (not payload.text).
    assert SENTINEL_M1_ROUND1 in msg_blob, (
        "P1 finding regression: round-2 transcript empty. Router probably "
        "reverted to payload['text'] instead of payload['content']."
    )
