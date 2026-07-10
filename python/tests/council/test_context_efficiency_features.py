from __future__ import annotations

import json

import pytest

from errorta_council.context.compaction import compact_transcript_blocks
from errorta_council.context.dialect.parser import parse_digest_v1
from errorta_council.context.dialect.render import render_digest_v1
from errorta_council.context.efficiency import TranscriptCompactionConfig
from errorta_council.context.manifest_store import ContextManifestStore
from errorta_council.context.router import ContextBuildRequest, ContextRouter


class _NoopRetrieval:
    def fetch(self, **kw):
        return []


class _NoopTransforms:
    async def transform(self, request):  # pragma: no cover - not used here
        raise AssertionError("transform not expected")


def _request(member_id: str = "m-1") -> ContextBuildRequest:
    return ContextBuildRequest(
        run_id="run-eff",
        turn_id=f"{member_id}-r1",
        room_id="room-eff",
        member_id=member_id,
        round=1,
        sequence=1,
        prompt={
            "display_text": "Decide whether to enable context efficiency.",
            "normalized_text": "Decide whether to enable context efficiency.",
            "signature": "sig",
        },
        corpus_ids=[],
        requested_context_access="prompt_only",
        requested_transcript_access="none",
        destination_scope="local",
        max_input_tokens=8192,
        transcript_cursor=0,
        summary_cursor=0,
        gateway_route_id="fake.local.deterministic",
        metadata={},
    )


def _loader(config: dict, *, finalizer_id: str | None = None):
    def load(run_id: str):
        return {
            "run_id": run_id,
            "events": [],
            "members": [
                {"member_id": "m-1", "id": "m-1", "role": "council", "provider_class": "fake"},
                {"member_id": "m-final", "id": "m-final", "role": "finalizer", "provider_class": "fake"},
            ],
            "room": {
                "context_access_ceiling": "full_context",
                "transcript_access_ceiling": "all_messages",
                "allow_full_context": True,
                "context_efficiency": config,
                "finalization_policy": {"finalizer_member_id": finalizer_id},
            },
            "topology": {
                "context_access_ceiling": "full_context",
                "transcript_access_ceiling": "all_messages",
            },
            "residency": {"destination_scope": "local"},
            "corpus_policy": {"max_egress_class": "remote_eligible"},
        }
    return load


@pytest.mark.asyncio
async def test_style_and_dialect_blocks_are_config_gated(tmp_path, tmp_errorta_home):
    store = ContextManifestStore(root=tmp_path)
    router = ContextRouter(
        retrieval=_NoopRetrieval(),
        transforms=_NoopTransforms(),
        manifest_store=store,
        run_snapshot_loader=_loader({
            "deliberation_style": "telegraphic",
            "deliberation_dialect": "digest_v1",
            "citation_references": True,
            "prompt_cache_hints": True,
        }),
    )
    payload = await router.build(_request("m-1"))
    roles = [m["role"] for m in payload.messages]
    assert payload.cache_hints
    manifest = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert manifest["source_counts"]["style_instructions"] == 1
    assert manifest["source_counts"]["dialect_instructions"] == 1
    assert manifest["packing_order_variant"] == "cache_hints_only"
    assert manifest["cache_hints"]
    assert roles == ["system", "user", "user", "user"]


@pytest.mark.asyncio
async def test_finalizer_excludes_style_and_dialect_blocks(tmp_path, tmp_errorta_home):
    store = ContextManifestStore(root=tmp_path)
    router = ContextRouter(
        retrieval=_NoopRetrieval(),
        transforms=_NoopTransforms(),
        manifest_store=store,
        run_snapshot_loader=_loader({
            "deliberation_style": "telegraphic",
            "deliberation_dialect": "digest_v1",
        }, finalizer_id="m-final"),
    )
    await router.build(_request("m-final"))
    manifest = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert "style_instructions" not in manifest["source_counts"]
    assert "dialect_instructions" not in manifest["source_counts"]


def test_transcript_compaction_structural_segment_records_omissions():
    blocks = [
        {
            "class_": "transcript_event",
            "content": "early",
            "content_sha256": "a" * 64,
            "round": 1,
            "member_id": "m-1",
            "transcript_event_id": "ev1",
        },
        {
            "class_": "transcript_event",
            "content": "fresh",
            "content_sha256": "b" * 64,
            "round": 4,
            "member_id": "m-2",
            "transcript_event_id": "ev2",
        },
    ]
    result = compact_transcript_blocks(
        blocks,
        current_round=4,
        config=TranscriptCompactionConfig(enabled=True, full_rounds_window=2),
    )
    assert result.blocks[0]["class_"] == "transcript_summary"
    assert result.blocks[1]["content"] == "fresh"
    assert result.segments[0]["event_ids"] == ["ev1"]
    assert result.omitted[0]["reason"] == "compacted_to_summary"


def test_digest_parser_drops_unknown_cites_and_renderer_is_deterministic():
    raw = """
    preface
    {"v":"digest_v1","position":"Enable it.","claims":[{"id":"k1","text":"Saves tokens.","cites":["c1","c9"],"confidence":"high"}],"agree":[],"dispute":[],"delta":null,"open":[]}
    """
    parsed = parse_digest_v1(raw, known_citations={"c1"})
    assert parsed.ok
    assert parsed.digest is not None
    assert parsed.digest["claims"][0]["cites"] == ["c1"]
    assert "unknown_cite:c9" in parsed.warnings
    assert render_digest_v1(parsed.digest, member_id="m-1") == render_digest_v1(
        parsed.digest, member_id="m-1"
    )
