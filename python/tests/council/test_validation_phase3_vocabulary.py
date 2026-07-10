"""P2 review-finding lock: validation accepts the F031-06 Phase 3 vocabulary.

Earlier ``_ALLOWED_TRANSCRIPT_ACCESS`` carried only the Phase 1 set, so
rooms using the documented Phase 3 values (``own_and_user``,
``previous_speaker``, ``role_scoped``, ``redacted_summary``,
``user_only``) were 422-rejected before reaching the resolver.
"""
from __future__ import annotations

import pytest

from errorta_council.schema import (
    BudgetPolicy,
    ContextPolicy,
    CouncilMember,
    CouncilRoom,
    FORMAT_VERSION,
    FinalizationPolicy,
    TopologyPolicy,
)
from errorta_council.gateway_meta import FakeGatewayMeta
from errorta_council.validation import validate_room


def _gateway_meta() -> FakeGatewayMeta:
    return FakeGatewayMeta(
        known_routes={"fake.local.deterministic": {
            "provider_class": "local", "kind": "fake",
            "provider_display": "Fake", "model_display": "stub-model",
        }},
    )


def _member(mid: str, *, transcript_access: str) -> CouncilMember:
    return CouncilMember(
        id=mid, name=mid, role="answerer", enabled=True,
        gateway_route_id="fake.local.deterministic",
        provider_kind="local", provider_display="Fake",
        model_display="stub-model", catalog_version="2026-06-11",
        context_access="prompt_only",
        transcript_access=transcript_access,
        turn_limits={
            "max_messages": 1, "max_input_tokens": 1024,
            "max_output_tokens": 256, "max_context_tokens": 1024,
        },
        generation={"temperature": 0.0, "top_p": None, "seed": None},
        system_prompt="x", metadata={},
    )


def _room(transcript_access: str) -> CouncilRoom:
    NOW = "2026-06-11T00:00:00Z"
    return CouncilRoom(
        format_version=FORMAT_VERSION,
        id="rm-phase3-vocab", name="Phase 3 Vocabulary", description="",
        members=[
            _member("m-1", transcript_access=transcript_access),
            _member("m-2", transcript_access=transcript_access),
        ],
        topology=TopologyPolicy(
            kind="round_robin", max_rounds=1, max_total_turns=2,
            max_messages_per_member=1, speaker_order=["m-1", "m-2"],
        ),
        context_policy=ContextPolicy(
            default_context_access="prompt_only",
            default_transcript_access=transcript_access,
            allow_full_context=False,
            require_confirmation_for_remote_context=True,
            require_confirmation_for_full_context=True,
        ),
        budget_policy=BudgetPolicy(
            max_rounds=1, max_messages_per_member=1,
            max_total_model_calls=2, max_remote_calls_per_run=0,
            max_remote_calls_per_day=None,
            max_input_tokens_per_turn=1024,
            max_output_tokens_per_turn=256,
            max_context_tokens_per_member=1024,
            max_estimated_usd_per_run=0.0,
            max_estimated_usd_per_month=None,
        ),
        finalization_policy=FinalizationPolicy(mode="transcript_only"),
        created_at=NOW, updated_at=NOW, revision=1,
    )


@pytest.mark.parametrize(
    "transcript_access",
    [
        "own_and_user",
        "previous_speaker",
        "role_scoped",
        "redacted_summary",
        "user_only",
        # Phase 1 values still accepted for backward compat.
        "own_messages",
        "all_messages",
        "summary_only",
        "none",
    ],
)
def test_phase3_transcript_access_validates(transcript_access: str) -> None:
    """Rooms with Phase 3 vocabulary must reach the resolver, not 422."""
    room = _room(transcript_access)
    result = validate_room(room, _gateway_meta())
    transcript_errors = [
        e for e in result.errors if "transcript_access" in e.get("code", "")
    ]
    assert not transcript_errors, transcript_errors


def test_unknown_transcript_access_still_rejected() -> None:
    """Invariant 4 still holds: unknown values get rejected."""
    room = _room("teleporting")
    result = validate_room(room, _gateway_meta())
    assert any(
        "transcript_access" in e.get("code", "") for e in result.errors
    )
