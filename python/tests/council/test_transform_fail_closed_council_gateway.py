"""QA P2 #5 (2026-06-12): TransformPipeline must fail closed on council
gateway errors AND on ``SummarizerUnavailable``.

Background. ``errorta_briefs.connector.{FatalError,RetryableError}`` and
``errorta_council.gateway_local.{FatalError,RetryableError}`` are two
DISTINCT exception class hierarchies. Pre-fix, ``TransformPipeline.transform``
caught only the briefs ones, so a misbehaving ``LocalGateway.summarize()``
would escape the pipeline and crash the engine. ``SummarizerUnavailable``
similarly slipped past.

These lock tests prove the broadened ``except`` clauses route every
summarizer failure into a blocked manifest with the correct reason —
invariant 4 (fail closed) end-to-end.
"""
from __future__ import annotations

import hashlib

import pytest

from errorta_council.context.transforms.pipeline import TransformPipeline
from errorta_council.context.transforms.redaction import REDACTION_VERSION, RedactionPipeline
from errorta_council.context.transforms.schema import (
    SourceEnvelope,
    TransformPolicy,
    TransformRequest,
)
from errorta_council.context.transforms.store import TransformStore
from errorta_council.context.transforms.summarization import SummaryPipeline


def _env(c: str) -> SourceEnvelope:
    return SourceEnvelope(
        class_="retrieved_snippet", corpus_id="c1", chunk_id="ch1",
        citation_id="ct1", content=c,
        content_sha256=hashlib.sha256(c.encode()).hexdigest(),
        tokens=1, sensitivity="known_local",
    )


def _req(*, content: str = "alpha") -> TransformRequest:
    return TransformRequest(
        run_id="r-1", turn_id="t-1", member_id="m-a",
        destination_scope="local",
        requested_context_access="redacted_summary",
        requested_egress_class="local",
        corpus_ids=["c1"], source_envelopes=[_env(content)],
        transcript_cursor=10, retrieval_cursor=5, max_output_tokens=128,
        policy=TransformPolicy(requested_context_access="redacted_summary",
                               destination_scope="local"),
    )


def _build_pipeline(gateway, tmp_path) -> TransformPipeline:
    return TransformPipeline(
        redaction=RedactionPipeline(version=REDACTION_VERSION),
        summary=SummaryPipeline(gateway=gateway, route_id="local/ollama/x"),
        store=TransformStore(root=tmp_path),
    )


class _CouncilFatalGateway:
    async def is_reachable(self):
        return True

    async def summarize(self, request):
        from errorta_council.gateway_local import FatalError
        raise FatalError("model_not_found: local/ollama/x")


class _CouncilRetryableGateway:
    async def is_reachable(self):
        return True

    async def summarize(self, request):
        from errorta_council.gateway_local import RetryableError
        raise RetryableError("local_timeout")


class _UnreachableNoFallbackGateway:
    async def is_reachable(self):
        return False

    async def summarize(self, request):
        raise AssertionError("must not be called when unreachable")


@pytest.mark.asyncio
async def test_council_gateway_fatal_falls_back_to_structural_when_allowed(tmp_path):
    """``errorta_council.gateway_local.FatalError`` from
    ``LocalGateway.summarize()`` (typically ``model_not_found``) is
    caught INSIDE ``SummaryPipeline`` when ``allow_extractive_fallback``
    is on, and degrades to a structural artifact. The transform
    finishes as ``allowed`` with structural content — not blocked.

    This is the documented degraded path: the demo's hardcoded
    ``local/ollama/llama3.2:3b`` route_id rarely matches an installed
    Ollama model on a dev machine, but the engine still proceeds with
    a structural summary rather than blocking the member.

    The QA P2 #5 broadened catch in pipeline.py only fires for the
    fallback-forbidden case (next test) or for Retryable errors that
    SummaryPipeline does NOT catch.
    """
    pipe = _build_pipeline(_CouncilFatalGateway(), tmp_path)
    result = await pipe.transform(_req())
    assert result.status == "allowed"
    assert result.artifact_kind == "redacted_summary"
    # The artifact came from the structural fallback path, so no raw
    # source bytes appear in the content.
    assert "alpha" not in (result.content or "")


@pytest.mark.asyncio
async def test_council_gateway_fatal_no_fallback_routes_to_blocked_manifest(tmp_path):
    """When ``allow_extractive_fallback=False``, ``SummaryPipeline``
    re-raises as ``SummarizerUnavailable`` on FatalError. The pipeline
    must catch that and emit a blocked manifest with
    ``blocked_reason="summarizer_failed_fatal"`` — previously escaped
    because the catch only caught briefs.FatalError.
    """
    pipe = TransformPipeline(
        redaction=RedactionPipeline(version=REDACTION_VERSION),
        summary=SummaryPipeline(
            gateway=_CouncilFatalGateway(),
            route_id="local/ollama/x",
            allow_extractive_fallback=False,
        ),
        store=TransformStore(root=tmp_path),
    )
    result = await pipe.transform(_req())
    assert result.status == "blocked"
    assert result.blocked_reason == "summarizer_failed_fatal"
    assert result.content is None
    assert result.egress_class == "blocked"


@pytest.mark.asyncio
async def test_council_gateway_retryable_routes_to_blocked_manifest(tmp_path):
    """``errorta_council.gateway_local.RetryableError`` must produce a
    blocked transform manifest with ``blocked_reason="summarizer_failed_retryable"``.
    """
    pipe = _build_pipeline(_CouncilRetryableGateway(), tmp_path)
    result = await pipe.transform(_req())
    assert result.status == "blocked"
    assert result.blocked_reason == "summarizer_failed_retryable"
    assert result.content is None


@pytest.mark.asyncio
async def test_summarizer_unavailable_routes_to_blocked_manifest(tmp_path):
    """When the gateway is unreachable AND policy forbids the fallback
    path, ``SummaryPipeline`` raises ``SummarizerUnavailable``. The
    pipeline must catch it and emit a blocked manifest rather than let it
    crash the engine.
    """
    pipe = TransformPipeline(
        redaction=RedactionPipeline(version=REDACTION_VERSION),
        summary=SummaryPipeline(
            gateway=_UnreachableNoFallbackGateway(),
            route_id="local/ollama/x",
            allow_extractive_fallback=False,
        ),
        store=TransformStore(root=tmp_path),
    )
    result = await pipe.transform(_req())
    assert result.status == "blocked"
    assert result.blocked_reason == "summarizer_failed_fatal"
    assert result.content is None
