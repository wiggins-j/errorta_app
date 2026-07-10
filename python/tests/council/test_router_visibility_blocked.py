"""QA P1 review-finding lock — router honors visibility.blocked_reason.

`TranscriptVisibilityResolver` can return a VisibilityPlan with
``blocked_reason="unknown_sensitivity_remote"`` when a remote-bound
member would see a transcript event tagged with sensitivity=unknown.
Earlier router code dropped that field and built a normal
ContextPayload anyway. This test feeds a fixture that triggers the
visibility-blocked path and asserts the router returns
BlockedContextResult + writes a blocked manifest.
"""
from __future__ import annotations

import pytest

from errorta_council.context.manifest_store import ContextManifestStore
from errorta_council.context.router import (
    BlockedContextResult,
    ContextBuildRequest,
    ContextRouter,
)
from errorta_council.context.visibility import TranscriptVisibilityResolver


class _NoopRetrieval:
    def fetch(self, **kw): return []


class _NoopTransforms:
    async def transform(self, request):
        from errorta_council.context.transforms.schema import TransformResult
        return TransformResult(
            status="allowed", artifact_id=None, artifact_kind=None,
            content=None, content_sha256=None, egress_class="local",
            destination_scope=request.destination_scope,
            token_estimate={"input": 0, "output": 0},
            manifest_id="tm-x",
            blocked_reason=None, message_code=None, warnings=[])


def _loader_with_unknown_event(run_id):
    return {
        "run_id": run_id,
        "events": [
            {
                "sequence": 1, "id": "evt-0001", "type": "member_message",
                "member_id": "m_local",
                "payload": {"text": "WHO KNOWS", "sensitivity": "unknown"},
            },
        ],
        "members": [
            {"member_id": "m_local", "role": "council", "provider_class": "local"},
            {"member_id": "m_remote", "role": "council", "provider_class": "remote"},
        ],
        "room": {
            "context_access_ceiling": "full_context",
            "transcript_access_ceiling": "all_messages",
            "allow_full_context": True,
            "policy": {"allow_unknown_sensitivity_local": False},
        },
        "topology": {
            "context_access_ceiling": "full_context",
            "transcript_access_ceiling": "all_messages",
        },
        "residency": {"destination_scope": "remote"},
        "corpus_policy": {
            "max_egress_class": "remote_eligible",
            # Allow the corpus_egress branch to pass so we isolate the
            # visibility-blocked path.
        },
    }


@pytest.mark.asyncio
async def test_router_blocks_when_visibility_blocks(tmp_path) -> None:
    store = ContextManifestStore(root=tmp_path)
    router = ContextRouter(
        retrieval=_NoopRetrieval(), transforms=_NoopTransforms(),
        manifest_store=store,
        run_snapshot_loader=_loader_with_unknown_event,
        visibility=TranscriptVisibilityResolver(),
    )
    req = ContextBuildRequest(
        run_id="r-1", turn_id="t-1", room_id="room-1",
        member_id="m_remote", round=1, sequence=1,
        prompt={"display_text": "hi", "normalized_text": "hi", "signature": "s"},
        corpus_ids=[],
        requested_context_access="prompt_only",
        requested_transcript_access="all_messages",
        destination_scope="remote", max_input_tokens=8192,
        transcript_cursor=10, summary_cursor=0,
        gateway_route_id="remote/x", metadata={},
    )
    result = await router.build(req)
    assert isinstance(result, BlockedContextResult), (
        f"router must propagate visibility.blocked_reason; got {type(result).__name__}"
    )
    assert result.blocked_reason == "unknown_sensitivity_remote"
    assert result.effective_context_access == "blocked"
    # Manifest is persisted with the blocked reason.
    import json
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text())
    assert data["blocked_reason"] == "unknown_sensitivity_remote"
    assert data["effective_context_access"] == "blocked"
    # Visibility plan id surfaces on the blocked manifest for audit.
    assert data["visibility_plan_id"]
