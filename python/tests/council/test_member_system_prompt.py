"""The member system prompt: use the configured one, else a neutral default.

Regression for the "Gemini roleplays" report: the old task instruction was
``"You are <member_id> in a Council run."`` — ID-as-identity framing that made
some models adopt a persona. The router now uses the member's configured
``system_prompt`` when set, and otherwise a neutral default that explicitly
says not to role-play. Either way the old framing must be gone.
"""
from __future__ import annotations

import pytest

from errorta_council.context.manifest_store import ContextManifestStore
from errorta_council.context.router import (
    DEFAULT_MEMBER_SYSTEM_PROMPT,
    ContextBuildRequest,
    ContextRouter,
)


class _NoopRetrieval:
    def fetch(self, **kw):
        return []


class _NoopTransforms:
    async def transform(self, request):  # pragma: no cover - not used here
        raise AssertionError("transform not expected")


def _request(member_id: str = "m-1") -> ContextBuildRequest:
    return ContextBuildRequest(
        run_id="run-sp",
        turn_id=f"{member_id}-r1",
        room_id="room-sp",
        member_id=member_id,
        round=1,
        sequence=1,
        prompt={
            "display_text": "What is the capital of France?",
            "normalized_text": "what is the capital of france?",
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


def _loader(*, system_prompt: str):
    def load(run_id: str):
        return {
            "run_id": run_id,
            "events": [],
            "members": [
                {
                    "member_id": "m-1", "id": "m-1", "role": "member",
                    "provider_class": "fake", "system_prompt": system_prompt,
                },
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
    return load


def _system_text(payload) -> str:
    systems = [m["content"] for m in payload.messages if m["role"] == "system"]
    assert systems, "expected at least one system message"
    return systems[0]


@pytest.mark.asyncio
async def test_configured_system_prompt_is_used(tmp_path, tmp_errorta_home):
    router = ContextRouter(
        retrieval=_NoopRetrieval(),
        transforms=_NoopTransforms(),
        manifest_store=ContextManifestStore(root=tmp_path),
        run_snapshot_loader=_loader(
            system_prompt="You are a terse mathematician. Answer in one line.",
        ),
    )
    payload = await router.build(_request())
    text = _system_text(payload)
    assert text == "You are a terse mathematician. Answer in one line."
    assert "in a Council run" not in text


@pytest.mark.asyncio
async def test_empty_system_prompt_falls_back_to_neutral_default(
    tmp_path, tmp_errorta_home
):
    router = ContextRouter(
        retrieval=_NoopRetrieval(),
        transforms=_NoopTransforms(),
        manifest_store=ContextManifestStore(root=tmp_path),
        run_snapshot_loader=_loader(system_prompt=""),
    )
    payload = await router.build(_request())
    text = _system_text(payload)
    assert text == DEFAULT_MEMBER_SYSTEM_PROMPT
    # The roleplay-inducing framing must be gone, and the default tells the
    # model not to role-play.
    assert "in a Council run" not in text
    assert "role-play" in text.lower()
