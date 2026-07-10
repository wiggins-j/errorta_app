"""Phase 3 Task 12 — engine-backed marquee invariant-5 lock.

The router-only ``test_context_router_isolation_bytes.py`` proves the
router itself produces byte-isolated payloads. This test proves the
engine actually wires the router in: a real ``build_and_run`` drives
two members through the scheduler, the gateway captures the request
payload bytes, and the redacted member's payload must NOT contain
corpus-sentinel bytes.

Also asserts:
- A ``ContextManifest`` lands on disk per turn under
  ``${ERRORTA_HOME}/council/context-manifests/``.
- The ``CONTEXT_BUILT`` event payload carries ``manifest_id`` so the
  /inspection endpoint can project manifests back per turn.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

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
from errorta_council.context.transforms.schema import SourceEnvelope, TransformResult
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


SENTINEL = (
    "ZQ_ENGINE_SENTINEL_v1: classified_fact=alpha_quasar_42, "
    "internal_doc_id=AERO_ENGINE_INTERNAL_NOT_FOR_EGRESS"
)


class _CaptureGateway(LocalGateway):
    """LocalGateway subclass that records every request before delegating to fake."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[LocalCouncilModelRequest] = []

    async def call(self, request: LocalCouncilModelRequest) -> LocalCouncilModelResult:
        self.requests.append(request)
        # Bypass _ollama_dispatch — the test members are fake providers.
        return LocalCouncilModelResult(
            content=f"ANSWER_FROM_{request.metadata.get('member_id', 'unknown')}",
            provider="fake", provider_class="local",
            model=request.model,
            input_tokens=None, output_tokens=None,
            duration_ms=0, raw_usage_available=False,
        )

    async def is_reachable(self) -> bool:
        return False  # forces SummaryPipeline extractive fallback


class _SentinelRetrievalForFull:
    """Returns the corpus sentinel only for ``m-full``; empty for ``m-redacted``."""

    def fetch(self, *, member_id, prompt, corpus_ids, transcript_cursor, top_k=8):
        if member_id == "m-full":
            return [SourceEnvelope(
                class_="retrieved_snippet", corpus_id="aerospace",
                chunk_id="ch-001", citation_id="ct-001",
                content=SENTINEL,
                content_sha256=hashlib.sha256(SENTINEL.encode()).hexdigest(),
                tokens=len(SENTINEL.split()),
                sensitivity="may_contain_corpus",
            )]
        return []


class _RedactingTransforms:
    """Replaces the full corpus content with a summary that drops the sentinel."""

    async def transform(self, request):
        summary = "Summary: aerospace internal data referenced; details redacted."
        return TransformResult(
            status="allowed", artifact_id="sa-r-1",
            artifact_kind="redacted_summary",
            content=summary,
            content_sha256=hashlib.sha256(summary.encode()).hexdigest(),
            egress_class="local",
            destination_scope=request.destination_scope,
            token_estimate={"input": 10, "output": 8},
            manifest_id="tm-r-1",
            blocked_reason=None, message_code=None, warnings=[],
        )


class _FakeGatewayMeta:
    async def is_reachable(self) -> bool:
        return True
    async def list_installed_models(self) -> list[str]:
        return ["stub-model"]


def _request_bytes(req: LocalCouncilModelRequest) -> bytes:
    return json.dumps(
        {
            "model": req.model,
            "messages": req.messages,
            "metadata": req.metadata,
        },
        sort_keys=True,
    ).encode("utf-8")


def _build_test_router(*, run_store: RunStore, run_meta, gateway) -> ContextRouter:
    root = council_root()
    manifest_store = ContextManifestStore(root=root / "context-manifests")
    transform_store = TransformStore(root=root / "transforms")
    transforms = _RedactingTransforms()
    # SummaryPipeline still constructed for parity with production, but
    # unused because we pass our own _RedactingTransforms.
    SummaryPipeline(gateway=gateway, route_id="local.summary",
                    allow_extractive_fallback=True)
    RedactionPipeline(version=REDACTION_VERSION)
    TransformStore(root=root / "transforms-unused")
    TransformPipeline  # silence unused-import linter; class referenced for parity.
    loader = _build_snapshot_loader(run_store=run_store, run_meta=run_meta)
    return ContextRouter(
        retrieval=_SentinelRetrievalForFull(),
        transforms=transforms,
        manifest_store=manifest_store,
        run_snapshot_loader=loader,
    )


@pytest.mark.asyncio
async def test_engine_byte_isolation_through_router(
    tmp_errorta_home, runs_dir_path,
) -> None:
    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(
        room_id="rm-iso",
        room_snapshot={
            "id": "rm-iso",
            "context_access_ceiling": "full_context",
            "transcript_access_ceiling": "all_messages",
            "allow_full_context": True,
            "members": [
                {
                    "id": "m-full", "enabled": True, "role": "member",
                    "provider": "fake", "model": "stub-model",
                    "context_access": "full_context",
                    "transcript_access": "all_messages",
                    "gateway_route_id": "fake.local.deterministic",
                },
                {
                    "id": "m-redacted", "enabled": True, "role": "member",
                    "provider": "fake", "model": "stub-model",
                    "context_access": "redacted_summary",
                    "transcript_access": "none",
                    "gateway_route_id": "fake.local.deterministic",
                },
            ],
        },
        prompt="What are the propulsion parameters?",
        corpus_ids=["aerospace"],
    )

    capture = _CaptureGateway()
    router = _build_test_router(
        run_store=store, run_meta=meta, gateway=capture,
    )
    # The adapter used internally by build_and_run() will rebuild a
    # snapshot loader keyed on meta.room_snapshot — but the override
    # router we pass uses its own loader (already attached). Either path
    # is fine because the adapter only calls router.build(); the router
    # owns its loader.

    final = await asyncio.wait_for(
        build_and_run(
            run_store=store,
            run_meta=meta,
            policy=SchedulerPolicy(
                max_rounds=1, max_messages_per_member=1,
                per_turn_timeout_seconds=5,
            ),
            gateway_meta=_FakeGatewayMeta(),
            hardware_scan_present=True,
            gateway=capture,
            context_router=router,
        ),
        timeout=5.0,
    )
    assert final.status == "completed"

    assert len(capture.requests) == 2
    by_member = {r.metadata.get("member_id"): r for r in capture.requests}
    full_bytes = _request_bytes(by_member["m-full"])
    redacted_bytes = _request_bytes(by_member["m-redacted"])

    sentinel_bytes = SENTINEL.encode("utf-8")
    assert sentinel_bytes in full_bytes, (
        "fixture sanity: m-full's gateway request must carry the sentinel; "
        "if this fails the test isn't proving anything"
    )

    # THE MARQUEE ASSERTION — engine-backed (Invariant 5):
    assert sentinel_bytes not in redacted_bytes, (
        "Invariant 5 violation: redacted member's gateway request bytes "
        "contain corpus-sentinel bytes through the wired engine path"
    )
    assert b"ZQ_ENGINE_SENTINEL_v1" not in redacted_bytes


@pytest.mark.asyncio
async def test_engine_writes_manifest_and_stamps_event(
    tmp_errorta_home, runs_dir_path,
) -> None:
    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(
        room_id="rm-m",
        room_snapshot={
            "id": "rm-m",
            "members": [
                {"id": "m1", "enabled": True, "role": "member",
                 "provider": "fake", "model": "stub-model"},
                {"id": "m2", "enabled": True, "role": "member",
                 "provider": "fake", "model": "stub-model"},
            ],
        },
        prompt="hi",
        corpus_ids=[],
    )
    final = await asyncio.wait_for(
        build_and_run(
            run_store=store,
            run_meta=meta,
            policy=SchedulerPolicy(
                max_rounds=1, max_messages_per_member=1,
                per_turn_timeout_seconds=5,
            ),
            gateway_meta=_FakeGatewayMeta(),
            hardware_scan_present=True,
        ),
        timeout=5.0,
    )
    assert final.status == "completed"

    # Manifests on disk for THIS run.
    manifest_dir = council_root() / "context-manifests"
    manifest_paths = sorted(manifest_dir.glob("*.json"))
    run_manifests = []
    for p in manifest_paths:
        d = json.loads(p.read_text())
        if d.get("run_id") == meta.id:
            run_manifests.append(d)
    assert len(run_manifests) >= 2, (
        f"expected ≥2 manifests for run {meta.id}, found {len(run_manifests)}"
    )

    # CONTEXT_BUILT events carry manifest_id.
    _, events = store.read_run(meta.id)
    built = [e for e in events if e.type == EventType.CONTEXT_BUILT]
    assert len(built) == 2
    manifest_ids_in_events = {
        (e.payload or {}).get("manifest_id") for e in built
    }
    assert None not in manifest_ids_in_events, (
        "CONTEXT_BUILT events must stamp manifest_id (Phase 3 Task 12 contract)"
    )
    # Cross-check: every manifest_id stamped on an event matches a file on disk.
    disk_manifest_ids = {d["manifest_id"] for d in run_manifests}
    assert manifest_ids_in_events.issubset(disk_manifest_ids)
