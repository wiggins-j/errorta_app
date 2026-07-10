"""QA P2 review-finding lock — summarizer falls back when model not found.

The engine wires ``SummaryPipeline(..., route_id="local.summary")`` —
no such Ollama model exists on a normal machine. When the gateway IS
reachable, the pipeline previously skipped the extractive fallback and
called ``gateway.summarize()`` directly, which would raise FatalError
(model_not_found). That bricks every redacted_summary turn.

This test stubs a gateway that's reachable but raises model_not_found
on summarize(), then asserts SummaryPipeline returns an extractive
artifact when ``allow_extractive_fallback=True``.
"""
from __future__ import annotations

import hashlib

import pytest

from errorta_council.context.transforms.schema import SourceEnvelope
from errorta_council.context.transforms.summarization import (
    SummarizerUnavailable,
    SummaryPipeline,
)
from errorta_council.gateway_local import FatalError


class _ReachableButNoModelGateway:
    """Gateway is up, but the requested route_id has no installed model."""

    async def is_reachable(self) -> bool:
        return True

    async def summarize(self, request):
        raise FatalError(f"model_not_found: {request.route_id.split('/')[-1]}")


class _ReachableButNoModelGatewayStrict:
    """Same but the test forbids fallback — expects SummarizerUnavailable."""

    async def is_reachable(self) -> bool:
        return True

    async def summarize(self, request):
        raise FatalError("model_not_found: x")


def _envs(text: str) -> list[SourceEnvelope]:
    return [SourceEnvelope(
        class_="retrieved_snippet", corpus_id="c", chunk_id="ch",
        citation_id="ct", content=text,
        content_sha256=hashlib.sha256(text.encode()).hexdigest(),
        tokens=len(text.split()), sensitivity="known_local",
    )]


@pytest.mark.asyncio
async def test_falls_back_structural_on_model_not_found() -> None:
    """Updated for QA P1 #1 (2026-06-12): the fallback path now produces
    a structural artifact (not extractive) so no verbatim source bytes
    leak through redacted_summary contexts. The intent of this lock
    test — gateway FatalError doesn't crash the engine when fallback is
    permitted — is preserved.
    """
    src = "first sentence here. second sentence."
    pipe = SummaryPipeline(
        gateway=_ReachableButNoModelGateway(),
        route_id="local.summary",
        allow_extractive_fallback=True,
    )
    artifact = await pipe.summarize(
        _envs(src),
        max_output_tokens=64,
    )
    assert artifact.summary_mode == "structural"
    assert artifact.content  # non-empty (carries class metadata).
    # No source bytes leaked.
    assert "first sentence here" not in artifact.content
    assert "second sentence" not in artifact.content
    # Crucially: did NOT crash on FatalError.


@pytest.mark.asyncio
async def test_raises_summarizer_unavailable_when_fallback_forbidden() -> None:
    pipe = SummaryPipeline(
        gateway=_ReachableButNoModelGatewayStrict(),
        route_id="local.summary",
        allow_extractive_fallback=False,
    )
    with pytest.raises(SummarizerUnavailable):
        await pipe.summarize(_envs("x"), max_output_tokens=32)
