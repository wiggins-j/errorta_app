"""Summary freshness anchors gate reuse (F031-07)."""
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


class _CountingGateway:
    def __init__(self):
        self.call_count = 0

    async def summarize(self, request):
        from errorta_council.gateway_local import SummaryResult
        self.call_count += 1
        return SummaryResult(content=f"SUMMARY-{self.call_count}", duration_ms=1,
                             input_tokens=1, output_tokens=1)

    async def is_reachable(self):
        return True


def _env(content):
    return SourceEnvelope(
        class_="retrieved_snippet", corpus_id="c1", chunk_id="ch1", citation_id="ct1",
        content=content, content_sha256=hashlib.sha256(content.encode()).hexdigest(),
        tokens=1, sensitivity="known_local")


def _request(envs, *, cursor=10):
    return TransformRequest(
        run_id="r-1", turn_id="t-1", member_id="m-a",
        destination_scope="local", requested_context_access="redacted_summary",
        requested_egress_class="local", corpus_ids=["c1"],
        source_envelopes=envs, transcript_cursor=cursor, retrieval_cursor=5,
        max_output_tokens=128,
        policy=TransformPolicy(requested_context_access="redacted_summary",
                               destination_scope="local"),
    )


@pytest.mark.asyncio
async def test_identical_request_reuses_artifact(tmp_path):
    gw = _CountingGateway()
    pipe = TransformPipeline(
        redaction=RedactionPipeline(version=REDACTION_VERSION),
        summary=SummaryPipeline(gateway=gw, route_id="r"),
        store=TransformStore(root=tmp_path),
    )
    req = _request([_env("alpha")])
    r1 = await pipe.transform(req)
    r2 = await pipe.transform(req)
    assert r1.artifact_id == r2.artifact_id
    assert gw.call_count == 1, "fresh anchors must short-circuit second summarize"


@pytest.mark.asyncio
async def test_changed_cursor_invalidates_freshness(tmp_path):
    gw = _CountingGateway()
    pipe = TransformPipeline(
        redaction=RedactionPipeline(version=REDACTION_VERSION),
        summary=SummaryPipeline(gateway=gw, route_id="r"),
        store=TransformStore(root=tmp_path),
    )
    await pipe.transform(_request([_env("alpha")], cursor=10))
    await pipe.transform(_request([_env("alpha")], cursor=11))
    assert gw.call_count == 2
