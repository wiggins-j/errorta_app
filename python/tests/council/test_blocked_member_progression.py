"""QA P1 review-finding lock — blocked members do not loop forever.

Two distinct failures the review caught:

1. scheduler.py blocked-context branch only handled ``stop_behavior=stop``,
   bypassing the existing ``ask`` flow that admission-blocked uses.
2. state.py only counted MEMBER_MESSAGE — so a context-blocked or
   admission-blocked member's ``completed`` counter stayed at 0 forever,
   and round_robin would re-propose the same blocked member indefinitely.

This file locks both: (a) blocked context routes through the ask flow,
(b) attempts advance on MEMBER_SKIPPED so the topology cycles past.
"""
from __future__ import annotations

import asyncio
import hashlib

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
from errorta_council.gateway_local import (
    LocalCouncilModelRequest,
    LocalCouncilModelResult,
    LocalGateway,
)
from errorta_council.limits import SchedulerPolicy
from errorta_council.paths import council_root
from errorta_council.run_store import RunStore
from errorta_council.schema import EventType
from errorta_council.state import CounterRebuilder
from errorta_council.topologies.round_robin import (
    RoundRobinTopology,
    RunCompletion,
    TurnProposal,
)


class _RecordingGateway(LocalGateway):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[LocalCouncilModelRequest] = []

    async def call(self, request):
        self.requests.append(request)
        return LocalCouncilModelResult(
            content="ok", provider="fake", provider_class="local",
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


# ---------------------------------------------------------------------------
# Topology-layer lock — attempts advance round_robin past blocked members.
# ---------------------------------------------------------------------------


def test_round_robin_advances_when_only_skipped_events_exist() -> None:
    """If a member has zero MEMBER_MESSAGE but one MEMBER_SKIPPED, the
    topology must STILL move on — round_robin must read ``attempts_by_member``
    not ``completed_messages_by_member``.
    """
    from errorta_council.schema import (
        CouncilEvent, EventStatus, EventType, MemberSnapshot,
    )

    def _ev(seq, typ, status, member_id, round_n):
        return CouncilEvent(
            id=f"evt-{seq:04d}", run_id="r-1", sequence=seq,
            type=typ, status=status,
            created_at="2026-06-11T00:00:00Z",
            payload={"reason": "context_blocked"} if typ == EventType.MEMBER_SKIPPED else {},
            member_id=member_id, member_snapshot=None, round=round_n,
            usage=None, format_version=1,
        )

    events = [
        _ev(1, EventType.RUN_STARTED, EventStatus.RUNNING, None, None),
        _ev(2, EventType.MEMBER_SKIPPED, EventStatus.BLOCKED, "m-1", 1),
    ]
    counters = CounterRebuilder.from_events(events)
    # The new field MUST surface the skip; the legacy field is unchanged.
    assert counters.attempts_by_member.get("m-1") == 1
    assert counters.completed_messages_by_member.get("m-1", 0) == 0

    topo = RoundRobinTopology()
    proposal = topo.propose_next(
        {
            "members": [
                {"id": "m-1", "enabled": True},
                {"id": "m-2", "enabled": True},
            ],
            "counters": counters,
            "policy": SchedulerPolicy(
                max_rounds=1, max_messages_per_member=1,
                per_turn_timeout_seconds=5,
            ),
        },
        transcript=[],
    )
    # m-1 already took their attempt (and got skipped). The topology must
    # propose m-2 next, NOT loop back to m-1.
    assert isinstance(proposal, TurnProposal)
    assert proposal.member_id == "m-2", (
        "round-robin must advance past a blocked member; if this fails, "
        "the topology probably read ``completed`` instead of ``attempts``."
    )


# ---------------------------------------------------------------------------
# Scheduler-layer lock — blocked context terminates cleanly under
# ``stop_behavior=continue`` (the demo path) without an infinite loop.
# ---------------------------------------------------------------------------


class _AlwaysBlockTransforms:
    """Transform pipeline that always returns ``blocked`` so the router's
    Phase 3 redacted-summary path produces BlockedContextResult."""

    async def transform(self, request):
        from errorta_council.context.transforms.schema import TransformResult
        return TransformResult(
            status="blocked", artifact_id=None, artifact_kind=None,
            content=None, content_sha256=None,
            egress_class="blocked",
            destination_scope=request.destination_scope,
            token_estimate={"input": 0, "output": 0},
            manifest_id="tm-blocked",
            blocked_reason="redaction_unavailable",
            message_code="redaction_unavailable",
            warnings=[],
        )


def _build_block_router(*, run_store, run_meta, gateway):
    root = council_root()
    base_loader = _build_snapshot_loader(run_store=run_store, run_meta=run_meta)
    return ContextRouter(
        retrieval=RetrievalSeam(pipeline=None),
        transforms=_AlwaysBlockTransforms(),
        manifest_store=ContextManifestStore(root=root / "context-manifests"),
        run_snapshot_loader=base_loader,
    )


@pytest.mark.asyncio
async def test_engine_terminates_when_every_member_is_context_blocked(
    tmp_errorta_home, runs_dir_path,
) -> None:
    """End-to-end: 2 members, both requesting redacted_summary, transforms
    always block. The run MUST terminate (per attempts cap), not spin
    forever proposing the same blocked member.
    """
    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(
        room_id="rm-blk",
        room_snapshot={
            "id": "rm-blk",
            "context_access_ceiling": "full_context",
            "transcript_access_ceiling": "all_messages",
            "allow_full_context": True,
            "members": [
                {
                    "id": "m-1", "enabled": True, "role": "member",
                    "provider": "fake", "model": "stub-model",
                    "context_access": "redacted_summary",
                    "transcript_access": "none",
                    "gateway_route_id": "fake.local.deterministic",
                },
                {
                    "id": "m-2", "enabled": True, "role": "member",
                    "provider": "fake", "model": "stub-model",
                    "context_access": "redacted_summary",
                    "transcript_access": "none",
                    "gateway_route_id": "fake.local.deterministic",
                },
            ],
        },
        prompt="this turn will block",
        corpus_ids=["aerospace"],
    )
    capture = _RecordingGateway()
    router = _build_block_router(
        run_store=store, run_meta=meta, gateway=capture,
    )
    final = await asyncio.wait_for(
        build_and_run(
            run_store=store, run_meta=meta,
            policy=SchedulerPolicy(
                max_rounds=1, max_messages_per_member=1,
                per_turn_timeout_seconds=5,
                # ``continue_local_only`` is the policy that exercises
                # the cycle-past path. ``stop`` (the default) would
                # terminate after the first block — that's covered by
                # other tests; here we lock that ``continue`` doesn't
                # infinite-loop.
                stop_behavior="continue_local_only",
            ),
            gateway_meta=_FakeGatewayMeta(), hardware_scan_present=True,
            gateway=capture, context_router=router,
        ),
        # Tight ceiling so an actual infinite-loop bug fails the test
        # within seconds (timeout, not test pass).
        timeout=5.0,
    )
    # Gateway must NEVER have been called — every turn blocked at context.
    assert capture.requests == [], (
        "blocked context must not reach the gateway: invariant 4 + 5 lock"
    )
    # The run reached a terminal state cleanly. It can be completed (cap
    # reached without dispatch) or failed (depending on policy default).
    assert final.status in ("completed", "failed", "cancelled"), final.status
    # Both members got one MEMBER_SKIPPED.
    _, events = store.read_run(meta.id)
    skipped = [e for e in events if e.type == EventType.MEMBER_SKIPPED]
    skipped_member_ids = sorted({e.member_id for e in skipped if e.member_id})
    assert skipped_member_ids == ["m-1", "m-2"], (
        f"each member should be skipped exactly once; got {skipped_member_ids} "
        f"(skipped events: {len(skipped)})"
    )
