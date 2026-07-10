"""Invariant 5: fresh ContextPayload per member per turn.

Object-identity test: two member payloads in the same turn never share
the same messages list, source_refs list, or metadata dict.
"""
from __future__ import annotations

import pytest

from errorta_council.context.manifest_store import ContextManifestStore
from errorta_council.context.router import ContextBuildRequest, ContextRouter


class _NoopRetrieval:
    def fetch(self, **kw): return []


class _PassthroughTransform:
    async def transform(self, request):
        from errorta_council.context.transforms.schema import TransformResult
        return TransformResult(
            status="allowed", artifact_id="sa-1", artifact_kind="redacted_summary",
            content="redacted summary content", content_sha256="x" * 64,
            egress_class="local", destination_scope=request.destination_scope,
            token_estimate={"input": 1, "output": 1}, manifest_id="tm-1",
            blocked_reason=None, message_code=None, warnings=[])


def _request(member_id, *, access="prompt_only"):
    return ContextBuildRequest(
        run_id="r-1", turn_id=f"t-{member_id}", room_id="room-1",
        member_id=member_id, round=1, sequence=1,
        prompt={"display_text": "hello", "normalized_text": "hello",
                "signature": "sig-1"},
        corpus_ids=[],
        requested_context_access=access,
        requested_transcript_access="none",
        destination_scope="local",
        max_input_tokens=8192,
        transcript_cursor=0, summary_cursor=0,
        gateway_route_id="local/ollama/x",
        metadata={},
    )


@pytest.mark.asyncio
async def test_two_members_get_fresh_payload_objects(tmp_path):
    store = ContextManifestStore(root=tmp_path)
    router = ContextRouter(
        retrieval=_NoopRetrieval(),
        transforms=_PassthroughTransform(),
        manifest_store=store,
        run_snapshot_loader=lambda run_id: {
            "run_id": run_id, "events": [], "members": [
                {"member_id": "m_a", "role": "council", "provider_class": "local"},
                {"member_id": "m_b", "role": "council", "provider_class": "local"},
            ],
            "room": {"context_access_ceiling": "full_context",
                     "transcript_access_ceiling": "all_messages",
                     "allow_full_context": True},
            "topology": {"context_access_ceiling": "full_context",
                         "transcript_access_ceiling": "all_messages"},
            "residency": {"destination_scope": "local"},
            "corpus_policy": {"max_egress_class": "remote_eligible"},
        },
    )
    payload_a = await router.build(_request("m_a"))
    payload_b = await router.build(_request("m_b"))
    assert id(payload_a) != id(payload_b)
    assert id(payload_a.messages) != id(payload_b.messages)
    assert id(payload_a.source_refs) != id(payload_b.source_refs)
    assert id(payload_a.metadata) != id(payload_b.metadata)
