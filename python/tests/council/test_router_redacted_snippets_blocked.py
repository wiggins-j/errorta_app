"""QA P2 review-finding lock — redacted_snippets fail-closed until impl.

The TransformPipeline currently emits only summary artifacts. Routing
``redacted_snippets`` through it silently downgrades to a summary —
which violates invariant 4 (no silent degradation). The router blocks
the mode explicitly with ``redacted_snippets_not_implemented`` so the
UI/audit surface a clear reason instead of a fake summary.
"""
from __future__ import annotations

import json

import pytest

from errorta_council.context.manifest_store import ContextManifestStore
from errorta_council.context.router import (
    BlockedContextResult,
    ContextBuildRequest,
    ContextRouter,
)


class _NoopRetrieval:
    def fetch(self, **kw): return []


class _TransformWouldBeCalled:
    """Asserts the router does NOT call transforms for redacted_snippets."""

    def __init__(self) -> None:
        self.calls: list = []

    async def transform(self, request):
        self.calls.append(request)
        from errorta_council.context.transforms.schema import TransformResult
        return TransformResult(
            status="allowed", artifact_id="sa-1", artifact_kind="summary_only",
            content="this would be a summary",
            content_sha256="b" * 64,
            egress_class="local",
            destination_scope=request.destination_scope,
            token_estimate={"input": 0, "output": 0},
            manifest_id="tm-x", blocked_reason=None, message_code=None,
            warnings=[],
        )


def _loader(run_id):
    return {
        "run_id": run_id, "events": [],
        "members": [
            {"member_id": "m_a", "role": "council", "provider_class": "local"},
        ],
        "room": {
            "context_access_ceiling": "full_context",
            "transcript_access_ceiling": "all_messages",
            "allow_full_context": True,
        },
        "topology": {
            "context_access_ceiling": "full_context",
            "transcript_access_ceiling": "all_messages",
        },
        "residency": {"destination_scope": "local"},
        "corpus_policy": {"max_egress_class": "remote_eligible"},
    }


@pytest.mark.asyncio
async def test_redacted_snippets_blocks_fail_closed(tmp_path) -> None:
    store = ContextManifestStore(root=tmp_path)
    transforms = _TransformWouldBeCalled()
    router = ContextRouter(
        retrieval=_NoopRetrieval(), transforms=transforms,
        manifest_store=store, run_snapshot_loader=_loader,
    )
    req = ContextBuildRequest(
        run_id="r-1", turn_id="t-1", room_id="room-1",
        member_id="m_a", round=1, sequence=1,
        prompt={"display_text": "q", "normalized_text": "q", "signature": "s"},
        corpus_ids=["aerospace"],
        requested_context_access="redacted_snippets",
        requested_transcript_access="none",
        destination_scope="local", max_input_tokens=8192,
        transcript_cursor=0, summary_cursor=0,
        gateway_route_id="local/ollama/x", metadata={},
    )
    result = await router.build(req)
    assert isinstance(result, BlockedContextResult)
    assert result.blocked_reason == "redacted_snippets_not_implemented"
    # The pipeline must NOT be invoked — otherwise we'd be silently
    # producing a summary while telling the user it was snippets.
    assert transforms.calls == [], (
        "router must not call transforms for redacted_snippets; "
        "block first or upgrade the pipeline to emit per-snippet artifacts"
    )
    # Manifest persisted with the structured reason.
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text())
    assert data["blocked_reason"] == "redacted_snippets_not_implemented"
