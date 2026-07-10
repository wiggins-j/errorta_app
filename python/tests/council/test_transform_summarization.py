"""SummaryPipeline calls LocalGateway-shaped dependency (invariant 3)."""
from __future__ import annotations

import hashlib

import pytest

from errorta_council.context.transforms.schema import SourceEnvelope
from errorta_council.context.transforms.summarization import (
    SUMMARIZER_VERSION,
    SummarizerUnavailable,
    SummaryPipeline,
)


class _FakeGateway:
    def __init__(self, response_text="SUMMARY: alpha + beta", reachable=True):
        self.calls: list = []
        self._response = response_text
        self._reachable = reachable

    async def summarize(self, request):
        self.calls.append(request)
        from errorta_council.gateway_local import SummaryResult
        return SummaryResult(content=self._response, duration_ms=42,
                             input_tokens=20, output_tokens=8)

    async def is_reachable(self) -> bool:
        return self._reachable


def _env(content, class_="retrieved_snippet"):
    return SourceEnvelope(
        class_=class_, corpus_id="c1", chunk_id="ch1", citation_id="ct1",
        content=content, content_sha256=hashlib.sha256(content.encode()).hexdigest(),
        tokens=len(content.split()), sensitivity="known_local")


@pytest.mark.asyncio
async def test_summarize_invokes_local_gateway():
    gw = _FakeGateway()
    pipe = SummaryPipeline(gateway=gw, route_id="local/ollama/llama3.2:3b")
    envs = [_env("alpha text"), _env("beta text")]
    artifact = await pipe.summarize(envs, max_output_tokens=128)
    assert artifact.content == "SUMMARY: alpha + beta"
    assert artifact.summarizer_version == SUMMARIZER_VERSION
    assert len(gw.calls) == 1
    assert gw.calls[0].role == "summarizer"
    assert gw.calls[0].route_id == "local/ollama/llama3.2:3b"


@pytest.mark.asyncio
async def test_unreachable_gateway_falls_back_to_structural_no_source_bytes():
    """QA P1 #1 (2026-06-12): when the local gateway is unreachable AND
    fallback is permitted, the SummaryPipeline must emit structural
    metadata — NOT extractive sentences from the source envelopes. The
    extractive path by construction echoes source bytes (first sentence
    per chunk) shorter than the 40-char substring-leak gate, which would
    leak short corpus facts (e.g., "AIAR is published under Apache-2.0.")
    into redacted_summary contexts.
    """
    gw = _FakeGateway(reachable=False)
    pipe = SummaryPipeline(gateway=gw, route_id="local/ollama/llama3.2:3b",
                           allow_extractive_fallback=True)
    envs = [_env("first envelope text"), _env("second envelope text")]
    artifact = await pipe.summarize(envs, max_output_tokens=128)
    assert artifact.summary_mode == "structural"
    # No verbatim source bytes (not even short under-threshold windows).
    assert "first envelope text" not in artifact.content
    assert "second envelope text" not in artifact.content
    # Structural metadata is present.
    assert "Summary unavailable" in artifact.content
    assert "retrieved_snippet" in artifact.content


@pytest.mark.asyncio
async def test_unreachable_gateway_short_phrase_does_not_leak():
    """Regression for QA P1 #1: a single short phrase (well under the
    40-char substring-leak threshold) must NOT appear in the produced
    content when the gateway is unreachable. This is the failure mode
    QA reproduced with welcome-corpus facts.
    """
    gw = _FakeGateway(reachable=False)
    pipe = SummaryPipeline(gateway=gw, route_id="x")
    # 39 chars, deliberately under the substring-leak threshold so the
    # old extractive path would have passed the gate and leaked it.
    short_fact = "AIAR is published under Apache-2.0."
    assert len(short_fact) < 40
    artifact = await pipe.summarize([_env(short_fact)], max_output_tokens=64)
    assert artifact.summary_mode == "structural"
    assert short_fact not in artifact.content
    # Even single-word substrings from the source should not appear in
    # the structural fallback (it carries class names, not source text).
    assert "Apache-2.0" not in artifact.content


@pytest.mark.asyncio
async def test_summarizer_unavailable_raises_when_fallback_forbidden():
    gw = _FakeGateway(reachable=False)
    pipe = SummaryPipeline(gateway=gw, route_id="x", allow_extractive_fallback=False)
    with pytest.raises(SummarizerUnavailable):
        await pipe.summarize([_env("x")], max_output_tokens=64)


# ---------------------------------------------------------------------------
# F031-07 hardening — substring-leak gate (QA-elevated, 2026-06-12).
#
# Closes the byte-isolation gap discovered during F031-RETRIEVAL: when the
# extractive fallback's "first sentence" return path echoes a whole chunk
# (because the content has no periods), and the RedactionPipeline's pattern
# set doesn't match the content (arbitrary domain text), the redacted_summary
# member receives raw corpus bytes despite policy. The gate substitutes a
# structural-metadata fallback when the produced summary contains a >=N-char
# contiguous run from any source envelope.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_long_no_period_chunk_also_falls_back_structural():
    """Regression for the original F031-07 case: a no-period chunk
    (which the old extractive path would have echoed verbatim and the
    substring-leak gate would have caught) now goes structural directly
    via the unreachable-gateway path. End state is identical to QA P1
    #1's stricter contract.
    """
    gw = _FakeGateway(reachable=False)
    pipe = SummaryPipeline(
        gateway=gw, route_id="x", allow_extractive_fallback=True,
    )
    # 80 chars, no period, no patterns the redaction layer can grip.
    leak_text = (
        "The RS-25D propulsion controller delivers thrust telemetry at 50Hz "
        "via internal bus"
    )
    artifact = await pipe.summarize([_env(leak_text)], max_output_tokens=64)
    assert artifact.summary_mode == "structural"
    assert leak_text not in artifact.content
    assert leak_text[:40] not in artifact.content
    assert "Summary unavailable" in artifact.content
    assert "retrieved_snippet" in artifact.content


@pytest.mark.asyncio
async def test_abstractive_summary_swap_when_gateway_echoes_input():
    """If a misbehaving abstractive summarizer returns input verbatim, the
    gate must catch it just like the extractive case.
    """
    leak_text = (
        "Internal classification: ITAR_RESTRICTED_PROPULSION_DATA_v2_2026"
    )

    class _EchoingGateway:
        async def is_reachable(self):
            return True

        async def summarize(self, request):
            from errorta_council.gateway_local import SummaryResult
            # Echo the user payload back verbatim — adversarial summarizer.
            return SummaryResult(
                content=leak_text, duration_ms=1,
                input_tokens=10, output_tokens=10,
            )

    pipe = SummaryPipeline(gateway=_EchoingGateway(), route_id="x")
    artifact = await pipe.summarize([_env(leak_text)], max_output_tokens=64)
    assert artifact.summary_mode == "structural"
    assert leak_text not in artifact.content


@pytest.mark.asyncio
async def test_genuine_paraphrase_passes_through():
    """A real summarizer that paraphrases the content (no 40-char verbatim
    window) must pass the gate and surface as ``abstractive`` mode.
    """
    source = (
        "The RS-25D engine produces approximately 29,400 kN of vacuum thrust."
    )
    paraphrase = "Engine thrust output is documented in vacuum conditions."

    class _ParaphrasingGateway:
        async def is_reachable(self): return True
        async def summarize(self, request):
            from errorta_council.gateway_local import SummaryResult
            return SummaryResult(
                content=paraphrase, duration_ms=1,
                input_tokens=20, output_tokens=5,
            )

    pipe = SummaryPipeline(gateway=_ParaphrasingGateway(), route_id="x")
    artifact = await pipe.summarize([_env(source)], max_output_tokens=64)
    assert artifact.summary_mode == "abstractive"
    assert artifact.content == paraphrase


@pytest.mark.asyncio
async def test_threshold_zero_still_protects_unreachable_gateway_path():
    """Even with the substring-leak gate disabled (threshold=0), the
    unreachable-gateway path always emits structural metadata — the gate
    is the *abstractive* echo defense, not the extractive defense.
    """
    gw = _FakeGateway(reachable=False)
    pipe = SummaryPipeline(
        gateway=gw, route_id="x",
        summary_substring_leak_threshold=0,
    )
    leak_text = "The same long string with no periods and no patterns to grip"
    artifact = await pipe.summarize(
        [_env(leak_text)], max_output_tokens=128,
    )
    assert artifact.summary_mode == "structural"
    assert leak_text not in artifact.content
