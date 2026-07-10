"""Deliberate bug: unknown destination_scope → blocked, never approximated.

Invariant 4 (Fail closed, marquee).
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


class _ShouldNotBeCalledGateway:
    def __init__(self):
        self.called = False

    async def summarize(self, request):
        self.called = True
        raise AssertionError("must not be called on unknown destination")

    async def is_reachable(self):
        return True


def _env(c):
    return SourceEnvelope(
        class_="retrieved_snippet", corpus_id="c1", chunk_id="ch1", citation_id="ct1",
        content=c, content_sha256=hashlib.sha256(c.encode()).hexdigest(),
        tokens=1, sensitivity="known_local")


@pytest.mark.asyncio
async def test_unknown_destination_scope_blocks(tmp_path):
    gw = _ShouldNotBeCalledGateway()
    pipe = TransformPipeline(
        redaction=RedactionPipeline(version=REDACTION_VERSION),
        summary=SummaryPipeline(gateway=gw, route_id="r"),
        store=TransformStore(root=tmp_path),
    )
    req = TransformRequest(
        run_id="r-1", turn_id="t-1", member_id="m-a",
        destination_scope="experimental_v2",
        requested_context_access="redacted_summary",
        requested_egress_class="local",
        corpus_ids=["c1"], source_envelopes=[_env("alpha")],
        transcript_cursor=10, retrieval_cursor=5, max_output_tokens=128,
        policy=TransformPolicy(requested_context_access="redacted_summary",
                               destination_scope="experimental_v2"),
    )
    result = await pipe.transform(req)
    assert result.status == "blocked"
    assert result.blocked_reason == "unknown_destination"
    assert gw.called is False
