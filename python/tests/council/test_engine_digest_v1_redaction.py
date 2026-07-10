"""WS4 sentinel-redaction lock.

Asserts that the position / claims text from a ``digest_v1`` member's output
does NOT reach the gateway payload of a ``redacted_summary`` member in the
same run.

Why this test exists: a digest_v1 response is structured JSON embedded in
prose.  An incorrectly-wired router might include the raw member_message
content (which contains the digest JSON with position= / claims= fields) in
the context of a redacted_summary peer.  This test drives ``build_and_run``
end-to-end, plants a sentinel inside the digest position field, and asserts
the sentinel never appears in the redacted member's gateway request bytes.
"""
from __future__ import annotations

import asyncio
import hashlib
import json

import pytest

from errorta_council.context.manifest_store import ContextManifestStore
from errorta_council.context.retrieval import RetrievalSeam
from errorta_council.context.router import ContextRouter
from errorta_council.context.transforms.schema import SourceEnvelope, TransformResult
from errorta_council.engine import _build_snapshot_loader, build_and_run
from errorta_council.gateway_local import (
    LocalCouncilModelRequest,
    LocalCouncilModelResult,
    LocalGateway,
)
from errorta_council.limits import SchedulerPolicy
from errorta_council.paths import council_root
from errorta_council.run_store import RunStore


# Sentinel embedded in the digest_v1 position field.
DIGEST_POSITION_SENTINEL = (
    "DIGEST_POSITION_SENTINEL_WS4_DO_NOT_FORWARD_XQ99 "
    "Adopt proposal A with sub-clause 7 verbatim."
)

# What the digest_v1 member will emit.
DIGEST_V1_OUTPUT = json.dumps({
    "v": "digest_v1",
    "position": DIGEST_POSITION_SENTINEL,
    "claims": [
        {
            "id": "k1",
            "text": "Sub-clause 7 is unambiguous DIGEST_POSITION_SENTINEL_WS4_DO_NOT_FORWARD_XQ99",
            "cites": [],
            "confidence": "high",
        }
    ],
    "agree": [],
    "dispute": [],
    "delta": "revised",
    "open": [],
})


class _DigestGateway(LocalGateway):
    """Returns a digest_v1 response for the first member, a plain answer for the rest."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[LocalCouncilModelRequest] = []
        self._call_count = 0

    async def call(self, request: LocalCouncilModelRequest) -> LocalCouncilModelResult:
        self.requests.append(request)
        self._call_count += 1
        mid = request.metadata.get("member_id", "")
        if mid == "m-digest":
            content = DIGEST_V1_OUTPUT
        else:
            content = "Acknowledged."
        return LocalCouncilModelResult(
            content=content,
            provider="fake", provider_class="local",
            model=request.model,
            input_tokens=None, output_tokens=None,
            duration_ms=0, raw_usage_available=False,
        )

    async def is_reachable(self) -> bool:
        return False

    async def list_installed_models(self) -> list[str]:
        return ["stub-model"]


class _RedactingTransforms:
    """Drops content for redacted_summary members."""

    async def transform(self, request):
        summary = "Policy decision pending. [redacted]"
        return TransformResult(
            status="allowed", artifact_id="sa-digest-1",
            artifact_kind="redacted_summary",
            content=summary,
            content_sha256=hashlib.sha256(summary.encode()).hexdigest(),
            egress_class="local",
            destination_scope=request.destination_scope,
            token_estimate={"input": 5, "output": 4},
            manifest_id="tm-digest-1",
            blocked_reason=None, message_code=None, warnings=[],
        )


class _FakeGatewayMeta:
    async def is_reachable(self) -> bool:
        return True

    async def list_installed_models(self) -> list[str]:
        return ["stub-model"]


def _request_bytes(req: LocalCouncilModelRequest) -> bytes:
    return json.dumps(
        {"model": req.model, "messages": req.messages, "metadata": req.metadata},
        sort_keys=True,
    ).encode("utf-8")


@pytest.mark.asyncio
async def test_digest_v1_position_not_forwarded_to_redacted_member(
    tmp_errorta_home, runs_dir_path
) -> None:
    """Invariant 5 extended: digest_v1 position/claims do not reach redacted peers."""
    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(
        room_id="rm-digest-test",
        room_snapshot={
            "id": "rm-digest-test",
            "context_access_ceiling": "full_context",
            "transcript_access_ceiling": "all_messages",
            "allow_full_context": True,
            "members": [
                {
                    "id": "m-digest", "enabled": True, "role": "member",
                    "provider": "fake", "model": "stub-model",
                    "context_access": "full_context",
                    "transcript_access": "all_messages",
                    "gateway_route_id": "fake.local.deterministic",
                },
                {
                    "id": "m-redacted", "enabled": True, "role": "member",
                    "provider": "fake", "model": "stub-model",
                    "context_access": "redacted_summary",
                    "transcript_access": "none",
                    "gateway_route_id": "fake.local.deterministic",
                },
            ],
        },
        prompt="Should we adopt sub-clause 7?",
        corpus_ids=[],
    )

    gateway = _DigestGateway()
    root = council_root()
    manifest_store = ContextManifestStore(root=root / "context-manifests")
    loader = _build_snapshot_loader(run_store=store, run_meta=meta)
    router = ContextRouter(
        retrieval=RetrievalSeam(pipeline=None),
        transforms=_RedactingTransforms(),
        manifest_store=manifest_store,
        run_snapshot_loader=loader,
    )

    final = await asyncio.wait_for(
        build_and_run(
            run_store=store,
            run_meta=meta,
            policy=SchedulerPolicy(
                max_rounds=2,
                max_messages_per_member=2,
                per_turn_timeout_seconds=5,
            ),
            gateway_meta=_FakeGatewayMeta(),
            hardware_scan_present=True,
            gateway=gateway,
            context_router=router,
        ),
        timeout=10.0,
    )
    assert final.status == "completed"
    assert len(gateway.requests) >= 2, "expected both members to have been called"

    # Find the round-2 (or later) request for m-redacted so the digest output
    # from m-digest has had a chance to appear in the transcript.
    redacted_reqs = [
        r for r in gateway.requests
        if r.metadata.get("member_id") == "m-redacted"
    ]
    assert redacted_reqs, "m-redacted was never called"

    sentinel_bytes = DIGEST_POSITION_SENTINEL.encode("utf-8")
    for req in redacted_reqs:
        rb = _request_bytes(req)
        assert sentinel_bytes not in rb, (
            "Invariant 5 violation (WS4): digest_v1 position sentinel leaked into "
            "the redacted member's gateway request bytes.\n"
            f"Sentinel: {DIGEST_POSITION_SENTINEL!r}\n"
            f"Request bytes (truncated): {rb[:500]!r}"
        )
