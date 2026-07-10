"""QA P2 review-finding lock — engine path can reach corpus_egress_blocked.

The previous `_build_snapshot_loader` hardcoded
``residency.destination_scope=local`` and
``corpus_policy.max_egress_class=remote_eligible`` so the policy's new
``remote + local_only → block`` branch could not fire when driven
through the engine. This test seeds a room with those fields and
confirms the block surfaces end-to-end.
"""
from __future__ import annotations

import asyncio

import pytest

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
from errorta_council.gateway_local import LocalGateway
from errorta_council.limits import SchedulerPolicy
from errorta_council.paths import council_root
from errorta_council.run_store import RunStore
from errorta_council.schema import EventType


class _FakeGatewayMeta:
    async def is_reachable(self) -> bool:
        return True
    async def list_installed_models(self) -> list[str]:
        return ["stub-model"]


def _build_router(*, run_store, run_meta):
    root = council_root()
    return ContextRouter(
        retrieval=RetrievalSeam(pipeline=None),
        transforms=TransformPipeline(
            redaction=RedactionPipeline(version=REDACTION_VERSION),
            summary=SummaryPipeline(
                gateway=LocalGateway(), route_id="local.summary",
                allow_extractive_fallback=True,
            ),
            store=TransformStore(root=root / "transforms"),
        ),
        manifest_store=ContextManifestStore(root=root / "context-manifests"),
        run_snapshot_loader=_build_snapshot_loader(
            run_store=run_store, run_meta=run_meta,
        ),
    )


@pytest.mark.asyncio
async def test_engine_blocks_when_room_carries_remote_residency_local_only_corpus(
    tmp_errorta_home, runs_dir_path,
) -> None:
    """End-to-end: room declares remote residency + local_only corpus;
    member requests full_context. The engine must emit MEMBER_SKIPPED
    with reason=corpus_egress_blocked, NOT silently degrade.
    """
    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(
        room_id="rm-residency",
        room_snapshot={
            "id": "rm-residency",
            "context_access_ceiling": "full_context",
            "transcript_access_ceiling": "all_messages",
            "allow_full_context": True,
            "residency": {"destination_scope": "remote"},
            "corpus_policy": {"max_egress_class": "local_only"},
            "members": [
                {
                    "id": "m-1", "enabled": True, "role": "member",
                    "provider": "fake", "model": "stub-model",
                    "context_access": "full_context",
                    "transcript_access": "all_messages",
                    "gateway_route_id": "fake.local.deterministic",
                },
                {
                    "id": "m-2", "enabled": True, "role": "member",
                    "provider": "fake", "model": "stub-model",
                    "context_access": "full_context",
                    "transcript_access": "all_messages",
                    "gateway_route_id": "fake.local.deterministic",
                },
            ],
        },
        prompt="should block at corpus egress",
        corpus_ids=["aerospace"],
    )
    router = _build_router(run_store=store, run_meta=meta)
    final = await asyncio.wait_for(
        build_and_run(
            run_store=store, run_meta=meta,
            policy=SchedulerPolicy(
                max_rounds=1, max_messages_per_member=1,
                per_turn_timeout_seconds=5,
                stop_behavior="continue_local_only",
            ),
            gateway_meta=_FakeGatewayMeta(), hardware_scan_present=True,
            context_router=router,
        ),
        timeout=5.0,
    )
    assert final.status in ("completed", "failed"), final.status
    _, events = store.read_run(meta.id)
    skipped = [e for e in events if e.type == EventType.MEMBER_SKIPPED]
    reasons = [(e.payload or {}).get("reason") for e in skipped]
    assert "corpus_egress_blocked" in reasons, (
        f"engine path must surface corpus_egress_blocked; got reasons={reasons}"
    )
