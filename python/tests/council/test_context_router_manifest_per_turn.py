"""Invariant 5 + 11: every completed AND blocked member turn writes exactly one ContextManifest."""
from __future__ import annotations

import json

import pytest

from errorta_council.context.manifest_store import ContextManifestStore
from errorta_council.context.router import ContextBuildRequest, ContextRouter


class _NoopRetrieval:
    def fetch(self, **kw): return []


class _Passthrough:
    def __init__(self) -> None:
        self.calls: list = []

    async def transform(self, request):
        self.calls.append(request)
        from errorta_council.context.transforms.schema import TransformResult
        return TransformResult(
            status="allowed", artifact_id="sa-pt", artifact_kind="summary_only",
            content="passthrough summary",
            content_sha256="b" * 64,
            egress_class="local",
            destination_scope=request.destination_scope,
            token_estimate={"input": 0, "output": 0},
            manifest_id="tm-x", blocked_reason=None, message_code=None, warnings=[])


class _BlockedTransform:
    """Used by the transform-blocked test below."""

    async def transform(self, request):
        from errorta_council.context.transforms.schema import TransformResult
        return TransformResult(
            status="blocked", artifact_id=None, artifact_kind=None,
            content=None, content_sha256=None,
            egress_class="blocked",
            destination_scope=request.destination_scope,
            token_estimate={"input": 0, "output": 0},
            manifest_id="tm-blocked",
            blocked_reason="redaction_unavailable",
            message_code="redaction_unavailable", warnings=[])


def _req(member_id, access="prompt_only"):
    return ContextBuildRequest(
        run_id="r-1", turn_id=f"t-{member_id}", room_id="room-1",
        member_id=member_id, round=1, sequence=1,
        prompt={"display_text": "hi", "normalized_text": "hi", "signature": "s"},
        corpus_ids=[], requested_context_access=access,
        requested_transcript_access="none",
        destination_scope="local", max_input_tokens=8192,
        transcript_cursor=0, summary_cursor=0,
        gateway_route_id="local/ollama/x", metadata={})


def _loader(run_id):
    return {
        "run_id": run_id, "events": [],
        "members": [{"member_id": "m_a", "role": "council", "provider_class": "local"}],
        "room": {"context_access_ceiling": "full_context",
                 "transcript_access_ceiling": "all_messages",
                 "allow_full_context": True},
        "topology": {"context_access_ceiling": "full_context",
                     "transcript_access_ceiling": "all_messages"},
        "residency": {"destination_scope": "local"},
        "corpus_policy": {"max_egress_class": "remote_eligible"},
    }


@pytest.mark.asyncio
async def test_one_manifest_per_completed_turn(tmp_path):
    store = ContextManifestStore(root=tmp_path)
    router = ContextRouter(retrieval=_NoopRetrieval(), transforms=_Passthrough(),
                           manifest_store=store, run_snapshot_loader=_loader)
    await router.build(_req("m_a"))
    files = list((tmp_path).glob("*.json"))
    assert len(files) == 1


@pytest.mark.asyncio
async def test_one_manifest_per_blocked_turn(tmp_path):
    """Block via unknown context_access → still produces exactly one manifest."""
    store = ContextManifestStore(root=tmp_path)
    router = ContextRouter(retrieval=_NoopRetrieval(), transforms=_Passthrough(),
                           manifest_store=store, run_snapshot_loader=_loader)
    result = await router.build(_req("m_a", access="experimental_v3"))
    files = list((tmp_path).glob("*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text())
    assert data["blocked_reason"] == "unknown_context_access"


@pytest.mark.asyncio
async def test_transforms_invoked_for_redacted_summary(tmp_path):
    """F031-05/F031-07: router MUST call transforms.transform for the
    redacted access modes, and stamp transform_manifest_id on the manifest.

    This is the P1 review-finding lock: earlier router versions silently
    skipped this call and tests like test_one_manifest_per_completed_turn
    let it pass because they used prompt_only.
    """
    store = ContextManifestStore(root=tmp_path)
    transforms = _Passthrough()
    router = ContextRouter(
        retrieval=_NoopRetrieval(), transforms=transforms,
        manifest_store=store, run_snapshot_loader=_loader,
    )
    payload = await router.build(_req("m_a", access="redacted_summary"))
    assert len(transforms.calls) == 1, (
        "router must invoke transforms.transform for redacted_summary"
    )
    req = transforms.calls[0]
    assert req.requested_context_access == "redacted_summary"
    assert req.member_id == "m_a"
    # Manifest stamps transform provenance.
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text())
    assert data["transform_manifest_id"] == "tm-x"
    assert data["preview_redacted"]  # F031-05 §"bounded redacted preview metadata"


@pytest.mark.asyncio
async def test_transforms_blocked_blocks_context_build(tmp_path):
    """If transforms returns status=blocked, the router MUST block the
    whole context build with the transform's reason (invariant 4).
    """
    store = ContextManifestStore(root=tmp_path)
    router = ContextRouter(
        retrieval=_NoopRetrieval(), transforms=_BlockedTransform(),
        manifest_store=store, run_snapshot_loader=_loader,
    )
    result = await router.build(_req("m_a", access="redacted_summary"))
    from errorta_council.context.router import BlockedContextResult
    assert isinstance(result, BlockedContextResult)
    assert result.blocked_reason == "redaction_unavailable"
    # Manifest is persisted with transform provenance.
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text())
    assert data["blocked_reason"] == "redaction_unavailable"
    assert data["transform_manifest_id"] == "tm-blocked"
