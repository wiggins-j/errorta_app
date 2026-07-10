"""F095 BE-3 — room-level default corpus binding (``CouncilRoom.corpus_ids``).

The room editor / Council run picker writes a default corpus binding; a per-run
override still wins at run start. The field is additive and serialized only when
non-empty so pre-F095 rooms round-trip byte-identically.
"""
from __future__ import annotations

from errorta_council.schema import (
    BudgetPolicy,
    ContextPolicy,
    CouncilRoom,
    FinalizationPolicy,
    TopologyPolicy,
)


def _room_dict(**overrides) -> dict:
    base = {
        "format_version": 1,
        "id": "r1",
        "name": "Room",
        "description": "",
        "members": [],
        "topology": {
            "kind": "round_robin",
            "max_rounds": 1,
            "max_total_turns": 1,
            "max_messages_per_member": 1,
        },
        "context_policy": {
            "default_context_access": "prompt_only",
            "default_transcript_access": "own_messages",
            "allow_full_context": False,
            "require_confirmation_for_remote_context": True,
            "require_confirmation_for_full_context": True,
        },
        "budget_policy": {
            "max_rounds": 1,
            "max_messages_per_member": 1,
            "max_total_model_calls": 1,
            "max_remote_calls_per_run": 0,
            "max_remote_calls_per_day": 0,
            "max_input_tokens_per_turn": None,
            "max_output_tokens_per_turn": None,
            "max_context_tokens_per_member": None,
            "max_estimated_usd_per_run": None,
            "max_estimated_usd_per_month": None,
        },
        "finalization_policy": {"mode": "last_message"},
        "created_at": "t0",
        "updated_at": "t0",
        "revision": 1,
    }
    base.update(overrides)
    return base


def test_corpus_ids_default_empty_and_omitted_from_serialized_json() -> None:
    room = CouncilRoom.from_dict(_room_dict())
    assert room.corpus_ids == []
    assert room.effective_corpus_ids() == []
    # byte-identity: the key is not emitted when empty.
    assert "corpus_ids" not in room.to_dict()


def test_corpus_ids_round_trips_when_bound() -> None:
    room = CouncilRoom.from_dict(_room_dict(corpus_ids=["welcome", "aerospace-mini"]))
    assert room.corpus_ids == ["welcome", "aerospace-mini"]
    out = room.to_dict()
    assert out["corpus_ids"] == ["welcome", "aerospace-mini"]
    # re-read is stable
    assert CouncilRoom.from_dict(out).corpus_ids == ["welcome", "aerospace-mini"]


def test_effective_corpus_ids_tolerates_legacy_extras_home() -> None:
    # Pre-F095 demo rooms stored corpus_ids as an unknown top-level key, which
    # landed in _extras. The typed field is empty but the binding is honored.
    room = CouncilRoom(
        format_version=1,
        id="r1",
        name="Room",
        description="",
        members=[],
        topology=TopologyPolicy(
            kind="round_robin", max_rounds=1, max_total_turns=1, max_messages_per_member=1
        ),
        context_policy=ContextPolicy(
            default_context_access="prompt_only",
            default_transcript_access="own_messages",
            allow_full_context=False,
            require_confirmation_for_remote_context=True,
            require_confirmation_for_full_context=True,
        ),
        budget_policy=BudgetPolicy(
            max_rounds=1,
            max_messages_per_member=1,
            max_total_model_calls=1,
            max_remote_calls_per_run=0,
            max_remote_calls_per_day=0,
            max_input_tokens_per_turn=None,
            max_output_tokens_per_turn=None,
            max_context_tokens_per_member=None,
            max_estimated_usd_per_run=None,
            max_estimated_usd_per_month=None,
        ),
        finalization_policy=FinalizationPolicy(mode="last_message"),
        created_at="t0",
        updated_at="t0",
        revision=1,
        _extras={"corpus_ids": ["legacy-corpus"]},
    )
    assert room.corpus_ids == []
    assert room.effective_corpus_ids() == ["legacy-corpus"]


def test_typed_field_wins_over_extras() -> None:
    room = CouncilRoom.from_dict(_room_dict(corpus_ids=["typed"]))
    object.__setattr__(room, "_extras", {"corpus_ids": ["legacy"], "other": True})
    assert room.effective_corpus_ids() == ["typed"]
    assert room.to_dict()["corpus_ids"] == ["typed"]
    assert room.to_dict()["other"] is True
