"""MARQUEE Phase 3 test (Invariant 5).

Two members in the same turn:
- member_full gets full_context (raw retrieved snippet visible)
- member_redacted gets redacted_summary

Serialize BOTH ContextPayload objects to bytes (json.dumps → UTF-8 encode).
Assert that the redacted member's payload BYTES contain ZERO byte-substring
overlap with the corpus-derived snippet text the full-context member's
payload carries.

Bytes, NOT policy objects. A policy-object assertion would pass while
bytes still leaked.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict

import pytest

from errorta_council.context.manifest_store import ContextManifestStore
from errorta_council.context.router import ContextBuildRequest, ContextRouter
from errorta_council.context.transforms.schema import SourceEnvelope, TransformResult

CORPUS_SENTINEL_TEXT = (
    "ZQ_CORPUS_SENTINEL_PROPULSION_DATA_v1: thrust=29400 kN, isp=363s, "
    "engine=RS-25D, classification=ITAR_RESTRICTED_FAKE, "
    "internal_doc_id=AERO_2026_0617_INTERNAL_NOT_FOR_EGRESS"
)


class _AlwaysSentinelRetrieval:
    """Returns the corpus sentinel for BOTH members.

    The router must call ``self._transforms.transform(...)`` for any
    member whose effective_context_access ∈ {summary_only,
    redacted_summary, redacted_snippets}; that transform is what
    actually scrubs the sentinel. Returning the sentinel for both
    members forces the transform path to be exercised — if the router
    silently skips the transform call (as it did in the F031-3 Phase 3
    review finding), this test fails because the redacted member's
    bytes will contain the sentinel.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def fetch(self, *, member_id, prompt, corpus_ids, transcript_cursor, top_k=8):
        self.calls.append(str(member_id))
        return [SourceEnvelope(
            class_="retrieved_snippet", corpus_id="aerospace",
            chunk_id="ch-001", citation_id="ct-001",
            content=CORPUS_SENTINEL_TEXT,
            content_sha256=hashlib.sha256(CORPUS_SENTINEL_TEXT.encode()).hexdigest(),
            tokens=len(CORPUS_SENTINEL_TEXT.split()),
            sensitivity="may_contain_corpus",
        )]


class _RedactingTransforms:
    """Transform pipeline fake — replaces input envelopes with a redacted summary.

    The test asserts (a) this is actually called for the redacted
    member, and (b) the returned content does not carry sentinel bytes.
    """

    def __init__(self) -> None:
        self.calls: list = []

    async def transform(self, request):
        self.calls.append(request)
        redacted = "Summary: aerospace propulsion data referenced; details redacted."
        return TransformResult(
            status="allowed", artifact_id="sa-r-1", artifact_kind="redacted_summary",
            content=redacted,
            content_sha256=hashlib.sha256(redacted.encode()).hexdigest(),
            egress_class="local", destination_scope=request.destination_scope,
            token_estimate={"input": 10, "output": 8},
            manifest_id="tm-r-1", blocked_reason=None, message_code=None, warnings=[])


def _loader(run_id):
    return {
        "run_id": run_id, "events": [],
        "members": [
            {"member_id": "member_full", "role": "council", "provider_class": "local"},
            {"member_id": "member_redacted", "role": "council", "provider_class": "local"},
        ],
        "room": {"context_access_ceiling": "full_context",
                 "transcript_access_ceiling": "all_messages",
                 "allow_full_context": True},
        "topology": {"context_access_ceiling": "full_context",
                     "transcript_access_ceiling": "all_messages"},
        "residency": {"destination_scope": "local"},
        "corpus_policy": {"max_egress_class": "remote_eligible"},
    }


def _req(member_id, access):
    return ContextBuildRequest(
        run_id="r-1", turn_id=f"t-{member_id}", room_id="room-1",
        member_id=member_id, round=1, sequence=1,
        prompt={"display_text": "Q: report propulsion params",
                "normalized_text": "report propulsion params",
                "signature": "sig-1"},
        corpus_ids=["aerospace"],
        requested_context_access=access,
        requested_transcript_access="none",
        destination_scope="local", max_input_tokens=8192,
        transcript_cursor=0, summary_cursor=0,
        gateway_route_id="local/ollama/x", metadata={})


def _payload_bytes(payload) -> bytes:
    return json.dumps({
        "context_id": payload.context_id,
        "messages": payload.messages,
        "classes": payload.classes,
        "egress_class": payload.egress_class,
        "source_refs": [asdict(r) for r in payload.source_refs],
        "metadata": payload.metadata,
    }, sort_keys=True).encode("utf-8")


@pytest.mark.asyncio
async def test_redacted_member_payload_bytes_do_not_contain_full_corpus(tmp_path):
    """The marquee invariant-5 test. Bytes-level isolation guarantee."""
    store = ContextManifestStore(root=tmp_path)
    retrieval = _AlwaysSentinelRetrieval()
    transforms = _RedactingTransforms()
    router = ContextRouter(
        retrieval=retrieval,
        transforms=transforms,
        manifest_store=store,
        run_snapshot_loader=_loader,
    )

    payload_full = await router.build(_req("member_full", "full_context"))
    payload_redacted = await router.build(_req("member_redacted", "redacted_summary"))

    full_bytes = _payload_bytes(payload_full)
    redacted_bytes = _payload_bytes(payload_redacted)

    sentinel_bytes = CORPUS_SENTINEL_TEXT.encode("utf-8")
    assert sentinel_bytes in full_bytes, (
        "full_context member must have retrieved snippet bytes in payload; "
        "if this fails, the test fixture is broken before the real assertion runs"
    )

    # THE MARQUEE ASSERTION (Invariant 5):
    assert sentinel_bytes not in redacted_bytes, (
        "Invariant 5 violation: redacted_summary member's payload bytes contain "
        "corpus-sentinel bytes. Bytes, not policy objects — a policy-object "
        "assertion would pass while bytes still leak."
    )

    distinctive = b"ZQ_CORPUS_SENTINEL_PROPULSION_DATA_v1"
    assert distinctive not in redacted_bytes

    # Lock the F031-07 contract end of this slice: the transform pipeline
    # MUST be invoked for the redacted_summary member. Earlier router
    # versions silently skipped this call; the byte-isolation assertion
    # above passed coincidentally because retrieval returned empty for the
    # redacted member. With the strengthened fixture, retrieval always
    # returns the sentinel, so the only way bytes-isolation can hold is
    # if transforms actually scrubbed it.
    assert len(transforms.calls) >= 1, (
        "router did not call transforms.transform for redacted_summary — "
        "byte-isolation only held because retrieval was empty (P1 finding)"
    )
    redacted_calls = [
        c for c in transforms.calls if c.member_id == "member_redacted"
    ]
    assert len(redacted_calls) == 1, (
        "transforms must be called exactly once for the redacted member"
    )
    # The redacted member's payload manifest must carry the transform's
    # provenance — F031-07 §"Emit a transform manifest …".
    rm_files = sorted(store._root.glob("*.json"))  # noqa: SLF001
    redacted_manifest = None
    for p in rm_files:
        d = json.loads(p.read_text())
        if d.get("member_id") == "member_redacted":
            redacted_manifest = d
            break
    assert redacted_manifest is not None
    assert redacted_manifest["transform_manifest_id"] == "tm-r-1"
    # The redacted preview metadata may exist but MUST NOT carry sentinel bytes.
    assert CORPUS_SENTINEL_TEXT not in (
        redacted_manifest.get("preview_redacted") or ""
    )


@pytest.mark.asyncio
async def test_two_payloads_share_no_mutable_objects(tmp_path):
    """Adjacent invariant-5 guarantee: fresh objects per member.

    Mutating one must not affect the other.
    """
    store = ContextManifestStore(root=tmp_path)
    retrieval = _AlwaysSentinelRetrieval()
    transforms = _RedactingTransforms()
    router = ContextRouter(
        retrieval=retrieval,
        transforms=transforms,
        manifest_store=store,
        run_snapshot_loader=_loader,
    )
    a = await router.build(_req("member_full", "full_context"))
    b = await router.build(_req("member_redacted", "redacted_summary"))
    assert id(a.messages) != id(b.messages)
    assert id(a.source_refs) != id(b.source_refs)
    assert id(a.metadata) != id(b.metadata)
    a.messages.append({"role": "user", "content": "EXTRA"})
    assert {"role": "user", "content": "EXTRA"} not in b.messages
