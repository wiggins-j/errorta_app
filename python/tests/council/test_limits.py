from __future__ import annotations

import pytest

from errorta_council.limits import (
    SchedulerPolicy,
    PolicyValidationError,
    ReasonCode,
    validate_runnable,
)


def test_reason_codes_stable_vocabulary() -> None:
    """Locks the F031-09 reason-code seam — names must not drift."""
    assert ReasonCode.LIMITS_EXHAUSTED.value == "limits_exhausted"
    assert ReasonCode.NO_ELIGIBLE_MEMBERS.value == "no_eligible_members"
    assert ReasonCode.MEMBER_MESSAGE_CAP_REACHED.value == "member_message_cap_reached"
    assert ReasonCode.LOCAL_TIMEOUT.value == "local_timeout"
    assert ReasonCode.LOCAL_PROVIDER_UNAVAILABLE.value == "local_provider_unavailable"
    assert ReasonCode.LOCAL_MODEL_MISSING.value == "local_model_missing"
    assert ReasonCode.GATEWAY_ERROR.value == "gateway_error"
    assert ReasonCode.CANCEL_REQUESTED.value == "cancel_requested"
    assert ReasonCode.PER_TURN_TIMEOUT.value == "per_turn_timeout"
    assert ReasonCode.CAP_INVARIANT_VIOLATED.value == "cap_invariant_violated"
    assert ReasonCode.ORIGIN_NOT_AUTHORIZED.value == "origin_not_authorized"


def test_scheduler_policy_defaults() -> None:
    p = SchedulerPolicy(
        max_rounds=2,
        max_messages_per_member=4,
        max_total_member_messages=8,
        per_turn_timeout_seconds=30,
    )
    assert p.stop_behavior == "stop"
    assert p.skip_scope == "current_turn"
    assert p.member_block_behavior == "stop"
    assert p.allow_pause is True
    assert p.allow_resume is True
    assert p.allow_cancel is True
    assert p.max_wall_clock_seconds is None
    assert p.max_parallel_member_calls == 1


def test_validate_runnable_rejects_all_unbounded() -> None:
    with pytest.raises(PolicyValidationError) as exc:
        validate_runnable(
            SchedulerPolicy(
                max_rounds=None,
                max_messages_per_member=None,
                max_total_member_messages=None,
                per_turn_timeout_seconds=30,
            )
        )
    assert "missing_required_caps" in str(exc.value)


def test_validate_runnable_rejects_missing_max_messages_per_member() -> None:
    """F031-09 acceptance — both caps required."""
    with pytest.raises(PolicyValidationError) as exc:
        validate_runnable(
            SchedulerPolicy(max_rounds=1, per_turn_timeout_seconds=30)
        )
    assert "missing_required_caps" in str(exc.value)


def test_validate_runnable_rejects_zero_rounds() -> None:
    with pytest.raises(PolicyValidationError):
        validate_runnable(SchedulerPolicy(
            max_rounds=0, max_messages_per_member=1, per_turn_timeout_seconds=30,
        ))


def test_validate_runnable_rejects_unknown_stop_behavior() -> None:
    with pytest.raises(PolicyValidationError) as exc:
        validate_runnable(
            SchedulerPolicy(
                max_rounds=1, max_messages_per_member=1,
                per_turn_timeout_seconds=30, stop_behavior="explode",
            )
        )
    assert "unknown_stop_behavior" in str(exc.value)


def test_validate_runnable_rejects_parallel_member_calls_above_one() -> None:
    """Phase 1 ships fixed max_parallel_member_calls=1 (invariant scope)."""
    with pytest.raises(PolicyValidationError):
        validate_runnable(
            SchedulerPolicy(
                max_rounds=1, max_messages_per_member=1,
                per_turn_timeout_seconds=30,
                max_parallel_member_calls=2,
            )
        )


def test_validate_runnable_accepts_both_caps_bounded() -> None:
    validate_runnable(SchedulerPolicy(
        max_rounds=1, max_messages_per_member=1, per_turn_timeout_seconds=30,
    ))
