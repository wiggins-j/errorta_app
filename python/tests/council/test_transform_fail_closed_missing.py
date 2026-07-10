"""Deliberate bug: redacted_summary requested + redaction raises FatalError.

Asserts the turn blocks with reason 'redaction_unavailable' and NO provider
call is attempted. Invariant 4 (Fail closed, marquee) + invariant 12.
"""
from __future__ import annotations

import hashlib

import pytest

from errorta_council.context.transforms.pipeline import TransformPipeline
from errorta_council.context.transforms.schema import (
    SourceEnvelope,
    TransformPolicy,
    TransformRequest,
)
from errorta_council.context.transforms.store import TransformStore
from errorta_council.context.transforms.summarization import SummaryPipeline


class _RaisingRedaction:
    version = 1

    def exclude_disallowed_classes(self, envs, *, destination_scope):
        return list(envs), []

    def redact_envelopes(self, envs, *, destination_scope, _enforce_scan=True):
        from errorta_briefs.connector import FatalError
        raise FatalError("redaction_unavailable")


class _ShouldNotBeCalledGateway:
    def __init__(self):
        self.called = False

    async def summarize(self, request):
        self.called = True
        raise AssertionError("provider must not be called when redaction blocks")

    async def is_reachable(self):
        return True


def _env(content):
    return SourceEnvelope(
        class_="retrieved_snippet", corpus_id="c1", chunk_id="ch1", citation_id="ct1",
        content=content, content_sha256=hashlib.sha256(content.encode()).hexdigest(),
        tokens=1, sensitivity="known_local")


@pytest.mark.asyncio
async def test_redaction_unavailable_blocks_and_skips_provider(tmp_path):
    gw = _ShouldNotBeCalledGateway()
    pipe = TransformPipeline(
        redaction=_RaisingRedaction(),
        summary=SummaryPipeline(gateway=gw, route_id="r"),
        store=TransformStore(root=tmp_path),
    )
    req = TransformRequest(
        run_id="r-1", turn_id="t-1", member_id="m-a",
        destination_scope="local",
        requested_context_access="redacted_summary",
        requested_egress_class="local", corpus_ids=["c1"],
        source_envelopes=[_env("alpha")],
        transcript_cursor=10, retrieval_cursor=5, max_output_tokens=128,
        policy=TransformPolicy(requested_context_access="redacted_summary",
                               destination_scope="local"),
    )
    result = await pipe.transform(req)
    assert result.status == "blocked"
    assert result.blocked_reason == "redaction_unavailable"
    assert result.message_code == "redaction_unavailable"
    assert gw.called is False
