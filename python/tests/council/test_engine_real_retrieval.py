"""F031-RETRIEVAL engine-backed integration tests.

Mirror of ``test_engine_router_wired.py`` for the real-retrieval path
(F031-RET-4). Tests 1-3 + 5 inject a controllable ``_FakeRetrievalPipeline``
via ``RetrievalSeam(pipeline=...)`` + ``context_router=`` override; test 4
monkeypatches ``errorta_query.default_pipeline`` to exercise the real
``AiarRetrievalAdapter`` against ``StubPipeline``.

The marquee test ``test_engine_byte_isolation_marquee_still_holds_with_real_retrieval``
is the engine-backed invariant-5 lock for the new retrieval path: real
snippet bytes flow through the seam for ``m-full`` and ``m-redacted``,
but the redacted member's gateway request bytes must never carry the
sentinel.

SENTINEL value is deliberately distinct from ``test_engine_router_wired.py``
so cross-test contamination is impossible.
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
from errorta_query.models import QueryResult


# The sentinel used by the byte-isolation marquee is a redaction-eligible
# string (a provider-token-shaped pattern) so the F031-07 redaction
# pipeline reliably rewrites it before it reaches the redacted member's
# gateway payload. Tests 1, 2 and 5 use a different sentinel that exercises
# the verbatim path (no redaction match) since their assertions are about
# manifest shape + the FULL member, not the redacted member's outbound
# payload.
SENTINEL = "sk-F031RETSENTINELv1delta_lambda_99classified"
NON_REDACTED_SENTINEL = "ZQ_F031_RET_SENTINEL_v1: classified=delta_lambda_99"


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class _CaptureGateway(LocalGateway):
    """LocalGateway subclass that records every request before returning a fake result."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[LocalCouncilModelRequest] = []

    async def call(self, request: LocalCouncilModelRequest) -> LocalCouncilModelResult:
        self.requests.append(request)
        return LocalCouncilModelResult(
            content=f"ANSWER_FROM_{request.metadata.get('member_id', 'unknown')}",
            provider="fake",
            provider_class="local",
            model=request.model,
            input_tokens=None,
            output_tokens=None,
            duration_ms=0,
            raw_usage_available=False,
        )

    async def is_reachable(self) -> bool:
        # Forces SummaryPipeline extractive fallback for the redacted path.
        return False


class _FakeGatewayMeta:
    async def is_reachable(self) -> bool:
        return True

    async def list_installed_models(self) -> list[str]:
        return ["stub-model"]


class _FakeRetrievalPipeline:
    """``_QueryPipeline``-shaped fake.

    Returns the configured chunks (default: one carrying SENTINEL, one
    carrying ordinary text) for the configured ``corpus_id``; empty list
    for any other corpus.
    """

    def __init__(
        self,
        *,
        corpus_id: str = "welcome",
        chunks: list[QueryResult] | None = None,
    ) -> None:
        self.corpus_id = corpus_id
        self.calls: list[dict] = []
        if chunks is None:
            chunks = [
                QueryResult(
                    content=NON_REDACTED_SENTINEL,
                    corpus_id=corpus_id,
                    chunk_id="ch-sentinel",
                    citation_id="ct-sentinel",
                    score=0.95,
                    tokens=len(NON_REDACTED_SENTINEL.split()),
                ),
                QueryResult(
                    content="ordinary supporting text",
                    corpus_id=corpus_id,
                    chunk_id="ch-002",
                    citation_id="ct-002",
                    score=0.88,
                    tokens=3,
                ),
            ]
        self._chunks = chunks

    def query(self, *, prompt, corpus_ids, top_k):
        self.calls.append(
            {"prompt": prompt, "corpus_ids": list(corpus_ids), "top_k": top_k}
        )
        if self.corpus_id in corpus_ids:
            return list(self._chunks[:top_k])
        return []


def _request_bytes(req: LocalCouncilModelRequest) -> bytes:
    return json.dumps(
        {
            "model": req.model,
            "messages": req.messages,
            "metadata": req.metadata,
        },
        sort_keys=True,
    ).encode("utf-8")


def _build_real_router(
    *,
    run_store: RunStore,
    run_meta,
    gateway,
    retrieval_pipeline,
) -> ContextRouter:
    """Build a ContextRouter with the real F031-07 transform stack + the
    fake retrieval pipeline injected at the seam.

    Mirrors ``engine._build_context_router`` but takes the fake pipeline so
    tests are hermetic.
    """
    root = council_root()
    manifest_store = ContextManifestStore(root=root / "context-manifests")
    transform_store = TransformStore(root=root / "transforms")
    redaction = RedactionPipeline(version=REDACTION_VERSION)
    summary = SummaryPipeline(
        gateway=gateway,
        route_id="local.summary",
        allow_extractive_fallback=True,
    )
    transforms = TransformPipeline(
        redaction=redaction, summary=summary, store=transform_store,
    )
    retrieval = RetrievalSeam(pipeline=retrieval_pipeline)
    loader = _build_snapshot_loader(run_store=run_store, run_meta=run_meta)
    return ContextRouter(
        retrieval=retrieval,
        transforms=transforms,
        manifest_store=manifest_store,
        run_snapshot_loader=loader,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_engine_real_retrieval_populates_manifest_source_refs(
    tmp_errorta_home, runs_dir_path,
) -> None:
    """A corpus-bearing run with retrieval wired ends up with real
    ``source_counts`` + ``source_refs`` on the per-turn manifest on disk.
    """
    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(
        room_id="rm-real-retrieval",
        room_snapshot={
            "id": "rm-real-retrieval",
            "context_access_ceiling": "full_context",
            "transcript_access_ceiling": "all_messages",
            "allow_full_context": True,
            "members": [
                {
                    "id": "m-r", "enabled": True, "role": "member",
                    "provider": "fake", "model": "stub-model",
                    "context_access": "retrieved_snippets",
                    "transcript_access": "all_messages",
                    "gateway_route_id": "fake.local.deterministic",
                },
            ],
        },
        prompt="welcome corpus probe",
        corpus_ids=["welcome"],
    )

    capture = _CaptureGateway()
    pipeline = _FakeRetrievalPipeline()
    router = _build_real_router(
        run_store=store, run_meta=meta, gateway=capture,
        retrieval_pipeline=pipeline,
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
            gateway=capture,
            context_router=router,
        ),
        timeout=5.0,
    )
    assert final.status == "completed"

    manifest_dir = council_root() / "context-manifests"
    manifests = [
        json.loads(p.read_text())
        for p in sorted(manifest_dir.glob("*.json"))
        if json.loads(p.read_text()).get("run_id") == meta.id
    ]
    assert len(manifests) >= 1, "expected at least one manifest for the run"

    manifest = manifests[0]
    assert manifest["source_counts"].get("retrieved_snippet", 0) >= 1, (
        f"manifest source_counts should carry retrieved_snippet entries; got {manifest['source_counts']!r}"
    )
    snippet_refs = [
        r for r in manifest["source_refs"] if r.get("class_") == "retrieved_snippet"
    ]
    assert snippet_refs, "expected at least one retrieved_snippet source_ref"
    for ref in snippet_refs:
        assert ref.get("chunk_id"), f"chunk_id must not be empty: {ref!r}"
        assert ref.get("citation_id"), f"citation_id must not be empty: {ref!r}"
        assert ref.get("content_sha256"), f"content_sha256 must not be empty: {ref!r}"
        assert ref.get("corpus_id"), f"corpus_id must not be empty: {ref!r}"


@pytest.mark.asyncio
async def test_engine_real_retrieval_snippet_bytes_reach_gateway_for_full_context(
    tmp_errorta_home, runs_dir_path,
) -> None:
    """``full_context`` member's gateway request payload bytes carry the
    real retrieved snippet content (the verbatim path)."""
    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(
        room_id="rm-full",
        room_snapshot={
            "id": "rm-full",
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
            ],
        },
        prompt="please retrieve",
        corpus_ids=["welcome"],
    )

    capture = _CaptureGateway()
    pipeline = _FakeRetrievalPipeline()
    router = _build_real_router(
        run_store=store, run_meta=meta, gateway=capture,
        retrieval_pipeline=pipeline,
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
            gateway=capture,
            context_router=router,
        ),
        timeout=5.0,
    )
    assert final.status == "completed"
    assert len(capture.requests) == 1
    full_bytes = _request_bytes(capture.requests[0])
    assert NON_REDACTED_SENTINEL.encode("utf-8") in full_bytes, (
        "full_context member's gateway request payload should carry the "
        "retrieved snippet bytes verbatim"
    )


@pytest.mark.asyncio
async def test_engine_byte_isolation_marquee_still_holds_with_real_retrieval(
    tmp_errorta_home, runs_dir_path,
) -> None:
    """Invariant 5 marquee with retrieval ON.

    Two members querying the SAME corpus_id: ``m-full`` (full_context) sees
    the sentinel bytes in its outbound payload; ``m-redacted``
    (redacted_summary) must not.
    """
    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(
        room_id="rm-iso-real",
        room_snapshot={
            "id": "rm-iso-real",
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
        prompt="cross-member retrieval test",
        corpus_ids=["welcome"],
    )

    capture = _CaptureGateway()
    # Use a redaction-eligible sentinel (provider-token-shaped) so the
    # F031-07 redaction pipeline reliably rewrites it before it reaches the
    # redacted member's payload — this is what production redaction is for.
    marquee_chunks = [
        QueryResult(
            content=f"Internal note: {SENTINEL} please reference this token.",
            corpus_id="welcome",
            chunk_id="ch-marquee",
            citation_id="ct-marquee",
            score=0.99,
            tokens=10,
        ),
    ]
    pipeline = _FakeRetrievalPipeline(chunks=marquee_chunks)
    router = _build_real_router(
        run_store=store, run_meta=meta, gateway=capture,
        retrieval_pipeline=pipeline,
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
            gateway=capture,
            context_router=router,
        ),
        timeout=5.0,
    )
    assert final.status == "completed"
    assert len(capture.requests) == 2
    by_member = {r.metadata.get("member_id"): r for r in capture.requests}
    sentinel_bytes = SENTINEL.encode("utf-8")

    full_bytes = _request_bytes(by_member["m-full"])
    assert sentinel_bytes in full_bytes, (
        "fixture sanity: m-full's gateway request must carry the sentinel "
        "for the marquee to prove anything"
    )

    redacted_bytes = _request_bytes(by_member["m-redacted"])
    # MARQUEE — Invariant 5 with real retrieval:
    assert sentinel_bytes not in redacted_bytes, (
        "Invariant 5 violation: redacted member's gateway request bytes "
        "contain corpus-sentinel bytes through the real-retrieval engine path"
    )


@pytest.mark.asyncio
async def test_engine_byte_isolation_holds_for_arbitrary_corpus_content(
    tmp_errorta_home, runs_dir_path,
) -> None:
    """F031-07 hardening lock (QA-elevated 2026-06-12).

    Invariant 5 must hold for ARBITRARY corpus content — not just for
    redaction-pattern-matched sentinels. Before the F031-07 substring-leak
    gate landed in `SummaryPipeline`, the extractive fallback would echo
    a no-period chunk verbatim into the redacted_summary member's
    payload (RedactionPipeline doesn't grip arbitrary domain text). This
    test uses real engine + real transforms + arbitrary aerospace-style
    text and asserts the redacted member's gateway bytes never carry it.
    """
    # No periods, no provider tokens, no env vars, no IPs — none of the
    # RedactionPipeline rules grip. Pre-fix, the extractive fallback would
    # echo this verbatim into the redacted member's payload.
    arbitrary_text = (
        "Propulsion controller delivers thrust telemetry at fifty "
        "hertz via internal bus across hardened paths"
    )
    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(
        room_id="rm-iso-arbitrary",
        room_snapshot={
            "id": "rm-iso-arbitrary",
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
        prompt="arbitrary corpus content marquee",
        corpus_ids=["welcome"],
    )
    capture = _CaptureGateway()
    chunks = [
        QueryResult(
            content=arbitrary_text,
            corpus_id="welcome",
            chunk_id="ch-arbitrary",
            citation_id="ct-arbitrary",
            score=0.9,
            tokens=len(arbitrary_text.split()),
        ),
    ]
    pipeline = _FakeRetrievalPipeline(chunks=chunks)
    router = _build_real_router(
        run_store=store, run_meta=meta, gateway=capture,
        retrieval_pipeline=pipeline,
    )
    final = await asyncio.wait_for(
        build_and_run(
            run_store=store, run_meta=meta,
            policy=SchedulerPolicy(
                max_rounds=1, max_messages_per_member=1,
                per_turn_timeout_seconds=5,
            ),
            gateway_meta=_FakeGatewayMeta(), hardware_scan_present=True,
            gateway=capture, context_router=router,
        ),
        timeout=5.0,
    )
    assert final.status == "completed"
    assert len(capture.requests) == 2
    by_member = {r.metadata.get("member_id"): r for r in capture.requests}
    text_bytes = arbitrary_text.encode("utf-8")

    # Sanity check: m-full DOES carry the bytes (fixture is meaningful).
    full_bytes = _request_bytes(by_member["m-full"])
    assert text_bytes in full_bytes, (
        "fixture sanity: m-full's payload must carry the arbitrary corpus "
        "text for the assertion below to mean anything"
    )

    # F031-07 marquee assertion:
    redacted_bytes = _request_bytes(by_member["m-redacted"])
    assert text_bytes not in redacted_bytes, (
        "F031-07 violation: redacted member's gateway request bytes "
        "contain arbitrary corpus content. The substring-leak gate in "
        "SummaryPipeline failed to substitute the structural fallback."
    )
    # Distinctive 40-char window also absent (defensive).
    assert arbitrary_text[:40].encode("utf-8") not in redacted_bytes


@pytest.mark.asyncio
async def test_engine_real_retrieval_no_aiar_returns_empty_source_refs(
    monkeypatch, tmp_errorta_home, runs_dir_path,
) -> None:
    """With AIAR pinned to StubPipeline (no real retrieval), a corpus-bearing
    run still completes normally with empty ``source_refs`` — no skipped
    members, no blocked manifests."""
    from errorta_query.pipeline import StubPipeline

    # The adapter resolves ``default_pipeline`` lazily via
    # ``errorta_query.default_pipeline()`` (see
    # errorta_council/context/aiar_retrieval_adapter.py:_resolve_default_pipeline),
    # so this single monkeypatch is sufficient.
    monkeypatch.setattr(
        "errorta_query.default_pipeline", lambda: StubPipeline(),
    )

    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(
        room_id="rm-no-aiar",
        room_snapshot={
            "id": "rm-no-aiar",
            "context_access_ceiling": "full_context",
            "transcript_access_ceiling": "all_messages",
            "allow_full_context": True,
            "members": [
                {
                    "id": "m-stub", "enabled": True, "role": "member",
                    "provider": "fake", "model": "stub-model",
                    "context_access": "retrieved_snippets",
                    "transcript_access": "all_messages",
                    "gateway_route_id": "fake.local.deterministic",
                },
            ],
        },
        prompt="empty AIAR probe",
        corpus_ids=["welcome"],
    )

    capture = _CaptureGateway()
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
        ),
        timeout=5.0,
    )
    assert final.status == "completed"

    manifest_dir = council_root() / "context-manifests"
    manifests = [
        json.loads(p.read_text())
        for p in sorted(manifest_dir.glob("*.json"))
        if json.loads(p.read_text()).get("run_id") == meta.id
    ]
    assert len(manifests) >= 1
    manifest = manifests[0]
    assert manifest["source_counts"].get("retrieved_snippet", 0) == 0, (
        f"source_counts should have no retrieved_snippet entries with stub "
        f"pipeline; got {manifest['source_counts']!r}"
    )
    snippet_refs = [
        r for r in manifest["source_refs"] if r.get("class_") == "retrieved_snippet"
    ]
    assert snippet_refs == []

    _, events = store.read_run(meta.id)
    skipped = [e for e in events if e.type == EventType.MEMBER_SKIPPED]
    assert skipped == [], f"expected no MEMBER_SKIPPED events; got {skipped!r}"


@pytest.mark.asyncio
async def test_engine_real_retrieval_unknown_corpus_id_does_not_block_turn(
    tmp_errorta_home, runs_dir_path,
) -> None:
    """If retrieval returns [] for an unknown corpus_id, the turn still
    completes normally with empty source_refs — NOT MEMBER_SKIPPED.
    """
    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(
        room_id="rm-unknown-corpus",
        room_snapshot={
            "id": "rm-unknown-corpus",
            "context_access_ceiling": "full_context",
            "transcript_access_ceiling": "all_messages",
            "allow_full_context": True,
            "members": [
                {
                    "id": "m-u", "enabled": True, "role": "member",
                    "provider": "fake", "model": "stub-model",
                    "context_access": "retrieved_snippets",
                    "transcript_access": "all_messages",
                    "gateway_route_id": "fake.local.deterministic",
                },
            ],
        },
        prompt="unknown corpus probe",
        corpus_ids=["does-not-exist"],
    )

    # Fake pipeline only knows "welcome"; the run requests "does-not-exist".
    capture = _CaptureGateway()
    pipeline = _FakeRetrievalPipeline(corpus_id="welcome")
    router = _build_real_router(
        run_store=store, run_meta=meta, gateway=capture,
        retrieval_pipeline=pipeline,
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
            gateway=capture,
            context_router=router,
        ),
        timeout=5.0,
    )
    assert final.status == "completed"

    _, events = store.read_run(meta.id)
    skipped = [e for e in events if e.type == EventType.MEMBER_SKIPPED]
    assert skipped == [], (
        f"unknown corpus_id should not cause MEMBER_SKIPPED; got {skipped!r}"
    )

    manifest_dir = council_root() / "context-manifests"
    manifests = [
        json.loads(p.read_text())
        for p in sorted(manifest_dir.glob("*.json"))
        if json.loads(p.read_text()).get("run_id") == meta.id
    ]
    assert len(manifests) >= 1
    snippet_refs = [
        r
        for m in manifests
        for r in m["source_refs"]
        if r.get("class_") == "retrieved_snippet"
    ]
    assert snippet_refs == [], (
        f"unknown corpus should yield zero retrieved_snippet refs; got {snippet_refs!r}"
    )
