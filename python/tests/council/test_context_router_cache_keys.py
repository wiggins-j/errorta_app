"""Invariant 5: cache keys + log lines derive from payload_sha256/context_id ONLY.

Asserts no raw text in manifest filenames or log lines.
"""
from __future__ import annotations

import logging

import pytest

from errorta_council.context.manifest_store import ContextManifestStore
from errorta_council.context.router import ContextBuildRequest, ContextRouter

SENTINEL = "SENTINEL_RAW_TEXT_DO_NOT_KEY_BY_ME_alpha_beta"


class _NoopRetrieval:
    def fetch(self, **kw): return []


class _Passthrough:
    async def transform(self, request):
        from errorta_council.context.transforms.schema import TransformResult
        return TransformResult(
            status="allowed", artifact_id="sa-1", artifact_kind=None,
            content=None, content_sha256=None, egress_class="local",
            destination_scope=request.destination_scope,
            token_estimate={"input": 0, "output": 0},
            manifest_id="tm-1", blocked_reason=None, message_code=None, warnings=[])


@pytest.mark.asyncio
async def test_cache_keys_contain_only_hashes(tmp_path, caplog):
    captured_keys: list[str] = []
    store = ContextManifestStore(root=tmp_path)
    orig = store.write

    def _track(manifest):
        # The store keys files by manifest_id; assert no raw text in name.
        captured_keys.append(manifest.manifest_id)
        return orig(manifest)

    store.write = _track  # type: ignore[assignment]

    router = ContextRouter(
        retrieval=_NoopRetrieval(),
        transforms=_Passthrough(),
        manifest_store=store,
        run_snapshot_loader=lambda run_id: {
            "run_id": run_id, "events": [], "members": [
                {"member_id": "m_a", "role": "council", "provider_class": "local"},
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
    caplog.set_level(logging.DEBUG, logger="errorta_council")
    req = ContextBuildRequest(
        run_id="r-1", turn_id="t-1", room_id="room-1", member_id="m_a",
        round=1, sequence=1,
        prompt={"display_text": SENTINEL, "normalized_text": SENTINEL,
                "signature": "sig-1"},
        corpus_ids=[],
        requested_context_access="prompt_only",
        requested_transcript_access="none",
        destination_scope="local", max_input_tokens=8192,
        transcript_cursor=0, summary_cursor=0,
        gateway_route_id="local/ollama/x", metadata={},
    )
    payload = await router.build(req)
    for key in captured_keys:
        assert SENTINEL not in key, f"manifest key leaked raw text: {key}"
    for rec in caplog.records:
        assert SENTINEL not in rec.getMessage(), \
            f"log line leaked raw text: {rec.getMessage()}"
    assert payload.context_id
