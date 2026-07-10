"""TransformPipeline orchestrates redaction + summarization + store."""
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


class _FakeGateway:
    async def summarize(self, request):
        from errorta_council.gateway_local import SummaryResult
        text = "SUMMARY of: " + "; ".join(
            m["content"][:20] for m in request.messages if m.get("role") == "user"
        )
        return SummaryResult(content=text, duration_ms=10,
                             input_tokens=10, output_tokens=5)

    async def is_reachable(self):
        return True


def _env(content, class_="retrieved_snippet", sensitivity="known_local"):
    return SourceEnvelope(
        class_=class_, corpus_id="c1", chunk_id="ch1", citation_id="ct1",
        content=content, content_sha256=hashlib.sha256(content.encode()).hexdigest(),
        tokens=len(content.split()), sensitivity=sensitivity)


def _request(envs, *, scope="local", access="redacted_summary"):
    return TransformRequest(
        run_id="r-1", turn_id="t-1", member_id="m-a",
        destination_scope=scope, requested_context_access=access,
        requested_egress_class="local",
        corpus_ids=["c1"],
        source_envelopes=envs,
        transcript_cursor=10, retrieval_cursor=5,
        max_output_tokens=128,
        policy=TransformPolicy(requested_context_access=access, destination_scope=scope),
    )


@pytest.mark.asyncio
async def test_pipeline_redacted_summary_happy_path(tmp_path):
    store = TransformStore(root=tmp_path)
    redaction = RedactionPipeline(version=REDACTION_VERSION)
    summary = SummaryPipeline(gateway=_FakeGateway(), route_id="local/ollama/x")
    pipe = TransformPipeline(redaction=redaction, summary=summary, store=store)
    envs = [_env("see /Users/example/file.pdf"), _env("alpha beta gamma")]
    result = await pipe.transform(_request(envs))
    assert result.status == "allowed"
    assert result.artifact_kind == "redacted_summary"
    assert result.blocked_reason is None
    assert result.manifest_id
    assert "/Users/example" not in (result.content or "")


@pytest.mark.asyncio
async def test_pipeline_persists_manifest_only_not_raw(tmp_path):
    store = TransformStore(root=tmp_path)
    redaction = RedactionPipeline(version=REDACTION_VERSION)
    summary = SummaryPipeline(gateway=_FakeGateway(), route_id="local/ollama/x")
    pipe = TransformPipeline(redaction=redaction, summary=summary, store=store)
    envs = [_env("SENTINEL_RAW_TEXT_NEVER_PERSIST alpha")]
    result = await pipe.transform(_request(envs))
    on_disk = (tmp_path / "manifests" / f"{result.manifest_id}.json").read_text()
    assert "SENTINEL_RAW_TEXT_NEVER_PERSIST" not in on_disk
    assert "redaction_rule_counts" in on_disk
