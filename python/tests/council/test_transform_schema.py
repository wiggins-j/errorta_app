"""F031-07 transform schema — format_version pinning + frozen guarantees."""
from __future__ import annotations

import json

import pytest

from errorta_council.context.transforms.schema import (
    SourceEnvelope,
    SummaryFreshnessAnchors,
    TransformManifest,
    TransformRequest,
    TransformResult,
    TRANSFORM_FORMAT_VERSION,
)


def test_format_version_is_one():
    assert TRANSFORM_FORMAT_VERSION == 1


def test_manifest_round_trips_through_json():
    m = TransformManifest(
        format_version=1,
        manifest_id="tm-0001",
        run_id="r-1", turn_id="t-1", member_id="m-a",
        created_at="2026-06-11T00:00:00Z",
        artifact_kind="redacted_summary",
        status="allowed",
        source_refs=[],
        redaction_rule_counts={"home_path": 0, "user_var": 0},
        summarizer_route_id="local/ollama/llama3.2:3b",
        freshness_anchors=SummaryFreshnessAnchors(
            transcript_cursor=10, retrieval_cursor=0, source_hashes=["a"*64],
            corpus_policy_version=1, redaction_version=1, summarizer_version=1,
            created_at="2026-06-11T00:00:00Z"),
        payload_sha256="b"*64,
        blocked_reason=None,
        warnings=[],
    )
    from dataclasses import asdict
    s = json.dumps(asdict(m), sort_keys=True)
    assert "tm-0001" in s
    assert '"format_version": 1' in s


def test_source_envelope_is_frozen():
    e = SourceEnvelope(class_="retrieved_snippet", corpus_id="c1",
                       chunk_id="ch1", citation_id="ct1",
                       content="hello", content_sha256="a"*64,
                       tokens=2, sensitivity="known_local")
    with pytest.raises((AttributeError, Exception)):
        e.content = "MUTATED"  # type: ignore[misc]


def test_transform_result_blocked_carries_reason():
    r = TransformResult(
        status="blocked",
        artifact_id=None, artifact_kind=None,
        content=None, content_sha256=None,
        egress_class="blocked", destination_scope="remote",
        token_estimate={"input": 0, "output": 0},
        manifest_id="tm-x",
        blocked_reason="redaction_unavailable",
        message_code="redaction_unavailable",
        warnings=[],
    )
    assert r.status == "blocked"
    assert r.blocked_reason == "redaction_unavailable"


def test_transform_result_rejects_bad_status():
    with pytest.raises(ValueError):
        TransformResult(
            status="bogus",
            artifact_id=None, artifact_kind=None,
            content=None, content_sha256=None,
            egress_class="local", destination_scope="local",
            token_estimate={"input": 0, "output": 0},
            manifest_id="tm-x",
            blocked_reason=None,
            message_code=None,
            warnings=[],
        )
