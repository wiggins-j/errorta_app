"""F049 slice 2 — the router renders a live user interjection as a pinned,
always-visible authoritative block."""
from __future__ import annotations

import pytest

from errorta_council.context.manifest_store import ContextManifestStore
from errorta_council.context.packing import TokenPacker
from errorta_council.context.router import (
    USER_INTERJECTION_PREFIX,
    ContextBuildRequest,
    ContextRouter,
)

INTERJECTION_TEXT = "Focus on the cost trade-off, not the schedule."


class _EmptyRetrieval:
    def fetch(self, *, member_id, prompt, corpus_ids, transcript_cursor, top_k=8):
        return []


class _NoopTransforms:
    async def transform(self, request):  # pragma: no cover - not exercised here
        raise AssertionError("transform should not run for full_context members")


def _loader(run_id):
    return {
        "run_id": run_id,
        "events": [
            {"id": "e1", "type": "member_message", "sequence": 1,
             "member_id": "m1", "round": 1, "payload": {"content": "M1: initial take"}},
            {"id": "e2", "type": "user_interjection", "sequence": 2,
             "member_id": None, "round": 1,
             "payload": {"content": INTERJECTION_TEXT, "author": "user",
                         "requested_by": "user"}},
        ],
        "members": [
            {"member_id": "m1", "role": "council", "provider_class": "local"},
            {"member_id": "m3", "role": "council", "provider_class": "local"},
        ],
        "room": {"context_access_ceiling": "full_context",
                 "transcript_access_ceiling": "all_messages",
                 "allow_full_context": True},
        "topology": {"context_access_ceiling": "full_context",
                     "transcript_access_ceiling": "all_messages"},
        "residency": {"destination_scope": "local"},
        "corpus_policy": {"max_egress_class": "remote_eligible"},
    }


def _req(member_id, transcript_access):
    return ContextBuildRequest(
        run_id="r-1", turn_id=f"t-{member_id}", room_id="room-1",
        member_id=member_id, round=1, sequence=3,
        prompt={"display_text": "Compare the two designs",
                "normalized_text": "compare the two designs", "signature": "sig"},
        corpus_ids=[],
        requested_context_access="full_context",
        requested_transcript_access=transcript_access,
        destination_scope="local", max_input_tokens=8192,
        transcript_cursor=10, summary_cursor=0,
        gateway_route_id="local/ollama/x", metadata={})


def _router(tmp_path):
    return ContextRouter(
        retrieval=_EmptyRetrieval(),
        transforms=_NoopTransforms(),
        manifest_store=ContextManifestStore(root=tmp_path),
        run_snapshot_loader=_loader,
    )


def _all_text(payload) -> str:
    return "\n".join(m["content"] for m in payload.messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("access", ["none", "own_messages", "all_messages",
                                    "previous_speaker", "user_only"])
async def test_interjection_reaches_member_regardless_of_transcript_access(tmp_path, access):
    # The user's live message is authoritative direction — it must reach the
    # next member even when that member's transcript_access would hide peer
    # messages (it is rendered like the prompt, not via visibility).
    payload = await _router(tmp_path).build(_req("m3", access))
    text = _all_text(payload)
    assert INTERJECTION_TEXT in text, f"interjection missing for access={access}"
    assert USER_INTERJECTION_PREFIX.strip().split("\n")[0] in text


@pytest.mark.asyncio
async def test_interjection_marked_authoritative_and_attributed(tmp_path):
    payload = await _router(tmp_path).build(_req("m3", "all_messages"))
    text = _all_text(payload)
    # The prefix names the human operator and its precedence over members.
    assert "authoritative" in text.lower()
    assert "human operator" in text.lower()
    # source_refs record the provenance class.
    classes = [r.class_ for r in payload.source_refs]
    assert "user_interjection" in classes


@pytest.mark.asyncio
async def test_interjection_not_double_rendered_for_all_messages(tmp_path):
    # all_messages would otherwise pull the user_interjection event into the
    # transcript loop too — it must appear exactly once.
    payload = await _router(tmp_path).build(_req("m3", "all_messages"))
    text = _all_text(payload)
    assert text.count(INTERJECTION_TEXT) == 1


def test_interjection_is_pinned_above_transcript_under_budget():
    # Under a tight budget the user's message must survive while member
    # transcript is dropped (pinned just below the prompt).
    packer = TokenPacker(max_input_tokens=8)
    out = packer.pack([
        {"class_": "task_instructions", "content": "I", "tokens": 2,
         "content_sha256": "a" * 64},
        {"class_": "user_prompt", "content": "U", "tokens": 2,
         "content_sha256": "b" * 64},
        {"class_": "user_interjection", "content": "USER MSG", "tokens": 2,
         "content_sha256": "c" * 64},
        {"class_": "transcript_event", "content": "long member chatter",
         "tokens": 8, "content_sha256": "d" * 64},
    ])
    kept = [b["class_"] for b in out.kept]
    assert "user_interjection" in kept
    assert "transcript_event" not in kept
    assert [o["class_"] for o in out.omitted] == ["transcript_event"]
