from __future__ import annotations

import pytest

from errorta_council.local_context import LocalContextBuilder
from errorta_council.schema import CouncilEvent, EventStatus, EventType, RunMeta


def _msg_event(seq: int, member_id: str, content: str, round_: int = 1) -> CouncilEvent:
    return CouncilEvent(
        format_version=1,
        id=f"e{seq}",
        run_id="r1",
        sequence=seq,
        type=EventType.MEMBER_MESSAGE,
        status=EventStatus.COMPLETED,
        created_at="2026-06-11T00:00:00Z",
        payload={"content": content},
        member_id=member_id,
        round=round_,
    )


def _run_meta() -> RunMeta:
    return RunMeta(
        format_version=1,
        id="r1", room_id="rm", room_snapshot={}, prompt="What is the capital of France?",
        corpus_ids=[], status="running",
        created_at="2026-06-11T00:00:00Z", started_at=None,
        updated_at="2026-06-11T00:00:00Z", finished_at=None,
        last_sequence=0, event_count=0, terminal_event_id=None,
        resume_policy="mark_interrupted", costs={}, capabilities={},
    )


@pytest.mark.asyncio
async def test_includes_system_prompt_and_user_prompt() -> None:
    builder = LocalContextBuilder(max_input_chars=10_000)
    out = await builder.build(
        run_meta=_run_meta(),
        member={"id": "m1", "role": "scholar"},
        transcript=[],
    )
    assert out["context_id"].startswith("ctx-r1-m1-")
    roles = [m["role"] for m in out["messages"]]
    assert roles == ["system", "user"]
    assert "scholar" in out["messages"][0]["content"].lower()
    assert out["messages"][1]["content"] == "What is the capital of France?"


@pytest.mark.asyncio
async def test_includes_prior_local_member_messages() -> None:
    builder = LocalContextBuilder(max_input_chars=10_000)
    transcript = [
        _msg_event(1, "m1", "Paris."),
        _msg_event(2, "m2", "Paris, France."),
    ]
    out = await builder.build(
        run_meta=_run_meta(),
        member={"id": "m3", "role": "skeptic"},
        transcript=transcript,
    )
    contents = [m["content"] for m in out["messages"]]
    assert any("Paris." in c for c in contents)
    assert any("Paris, France." in c for c in contents)


@pytest.mark.asyncio
async def test_excludes_non_member_message_events() -> None:
    builder = LocalContextBuilder(max_input_chars=10_000)
    transcript = [
        _msg_event(1, "m1", "Paris."),
        CouncilEvent(
            format_version=1, id="e2", run_id="r1", sequence=2,
            type=EventType.CONTEXT_BUILT, status=EventStatus.COMPLETED,
            created_at="2026-06-11T00:00:00Z", payload={"context_id": "c"},
            member_id="m2", round=1,
        ),
    ]
    out = await builder.build(
        run_meta=_run_meta(),
        member={"id": "m3", "role": "scholar"},
        transcript=transcript,
    )
    assert all("context_id" not in m["content"] for m in out["messages"])


@pytest.mark.asyncio
async def test_bounded_by_char_budget_drops_oldest_first() -> None:
    builder = LocalContextBuilder(max_input_chars=200)
    big = "X" * 150
    transcript = [
        _msg_event(1, "m1", big),
        _msg_event(2, "m2", "recent-msg"),
    ]
    out = await builder.build(
        run_meta=_run_meta(),
        member={"id": "m3", "role": "scholar"},
        transcript=transcript,
    )
    contents = [m["content"] for m in out["messages"]]
    assert any("recent-msg" in c for c in contents)
    assert not any(big in c for c in contents)


@pytest.mark.asyncio
async def test_context_id_is_deterministic_per_member_per_turn() -> None:
    builder = LocalContextBuilder(max_input_chars=10_000)
    out1 = await builder.build(run_meta=_run_meta(), member={"id": "m1", "role": "x"}, transcript=[])
    out2 = await builder.build(run_meta=_run_meta(), member={"id": "m1", "role": "x"}, transcript=[])
    assert out1["context_id"] == out2["context_id"]
