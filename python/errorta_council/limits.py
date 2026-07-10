"""Scheduler policy + stable reason-code vocabulary (F031-09).

Invariant 7: caps in a `SchedulerPolicy` are absolute once a run is created;
no code path (decisions, moderator, finalizer) can raise them. Validation
runs once at run-creation; the scheduler reads the frozen policy thereafter.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final


class ReasonCode(str, Enum):
    """Stable reason-code vocabulary surfaced through the F031-02 event log."""

    LIMITS_EXHAUSTED = "limits_exhausted"
    NO_ELIGIBLE_MEMBERS = "no_eligible_members"
    MEMBER_MESSAGE_CAP_REACHED = "member_message_cap_reached"
    LOCAL_TIMEOUT = "local_timeout"
    LOCAL_PROVIDER_UNAVAILABLE = "local_provider_unavailable"
    LOCAL_MODEL_MISSING = "local_model_missing"
    GATEWAY_ERROR = "gateway_error"
    CANCEL_REQUESTED = "cancel_requested"
    PER_TURN_TIMEOUT = "per_turn_timeout"
    CAP_INVARIANT_VIOLATED = "cap_invariant_violated"
    ORIGIN_NOT_AUTHORIZED = "origin_not_authorized"


_STOP_BEHAVIORS: Final[frozenset[str]] = frozenset({"stop", "skip_member", "ask", "continue_local_only"})
_SKIP_SCOPES: Final[frozenset[str]] = frozenset({"current_turn", "current_round", "remainder_of_run"})
_BLOCK_BEHAVIORS: Final[frozenset[str]] = frozenset({"stop", "skip", "ask"})


@dataclass(frozen=True)
class SchedulerPolicy:
    """Frozen-at-run-creation scheduler limits + stop/skip semantics."""

    max_rounds: int | None = None
    max_messages_per_member: int | None = None
    max_total_member_messages: int | None = None
    max_wall_clock_seconds: int | None = None
    per_turn_timeout_seconds: int = 30
    stop_behavior: str = "stop"
    skip_scope: str = "current_turn"
    member_block_behavior: str = "stop"
    allow_pause: bool = True
    allow_resume: bool = True
    allow_cancel: bool = True
    max_parallel_member_calls: int = 1


class PolicyValidationError(ValueError):
    """Raised when a `SchedulerPolicy` is not runnable."""


def validate_runnable(policy: SchedulerPolicy) -> None:
    """Raise PolicyValidationError if `policy` is not a runnable config.

    Rules (F031-09 §Runnable config):
      - both `max_rounds` and `max_messages_per_member` MUST be positive ints
        (tightened post-review — at-least-one-bound let unbounded surfaces
        through; F031-09 acceptance requires both);
      - `max_total_member_messages` is optional;
      - `per_turn_timeout_seconds` must be > 0;
      - `stop_behavior` ∈ {stop, skip_member, ask, continue_local_only};
      - `skip_scope` ∈ {current_turn, current_round, remainder_of_run};
      - `member_block_behavior` ∈ {stop, skip, ask};
      - Phase 1 hard-pins `max_parallel_member_calls == 1`.
    """
    if policy.max_rounds is None or policy.max_messages_per_member is None:
        raise PolicyValidationError(
            "missing_required_caps: max_rounds and max_messages_per_member "
            "must both be positive ints (F031-09 §Runnable config)"
        )
    for name, value in (
        ("max_rounds", policy.max_rounds),
        ("max_messages_per_member", policy.max_messages_per_member),
        ("max_total_member_messages", policy.max_total_member_messages),
        ("max_wall_clock_seconds", policy.max_wall_clock_seconds),
    ):
        if value is not None and value <= 0:
            raise PolicyValidationError(f"{name}_must_be_positive: got {value}")
    if policy.per_turn_timeout_seconds <= 0:
        raise PolicyValidationError("per_turn_timeout_seconds_must_be_positive")
    if policy.stop_behavior not in _STOP_BEHAVIORS:
        raise PolicyValidationError(f"unknown_stop_behavior: {policy.stop_behavior}")
    if policy.skip_scope not in _SKIP_SCOPES:
        raise PolicyValidationError(f"unknown_skip_scope: {policy.skip_scope}")
    if policy.member_block_behavior not in _BLOCK_BEHAVIORS:
        raise PolicyValidationError(f"unknown_member_block_behavior: {policy.member_block_behavior}")
    if policy.max_parallel_member_calls != 1:
        raise PolicyValidationError(
            "phase1_pins_max_parallel_member_calls_to_1"
        )
