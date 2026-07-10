"""F008d-lifecycle — BriefState FSM.

Encodes the brief lifecycle from F008 spec §5 as an enum + an allowed-transition
table. Other modules (collectors, the resume loop, the UI route layer) consult
``can_transition`` / ``assert_transition`` to keep state moves legal.

Transition diagram (spec §5):

    DRAFT      -> {VALIDATING}
    VALIDATING -> {DRAFT, RUNNING, FAILED}
    RUNNING    -> {PAUSED, COMPLETED, FAILED}
    PAUSED     -> {RUNNING, ARCHIVED, FAILED}
    COMPLETED  -> {RUNNING, ARCHIVED}
    FAILED     -> {DRAFT, ARCHIVED}
    ARCHIVED   -> {}                    (terminal)
"""
from __future__ import annotations

from enum import Enum


class BriefState(str, Enum):
    """Lifecycle state of a brief-driven collection run.

    Stored as a string in JSON for human-readable on-disk state files; the
    ``str`` base lets ``json.dumps(..., default=str)`` round-trip cleanly.
    """

    DRAFT = "DRAFT"
    VALIDATING = "VALIDATING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    ARCHIVED = "ARCHIVED"


#: Allowed transitions, mirroring spec §5 exactly. Empty set = terminal.
LIFECYCLE_TRANSITIONS: dict[BriefState, set[BriefState]] = {
    BriefState.DRAFT: {BriefState.VALIDATING},
    BriefState.VALIDATING: {BriefState.DRAFT, BriefState.RUNNING, BriefState.FAILED},
    BriefState.RUNNING: {BriefState.PAUSED, BriefState.COMPLETED, BriefState.FAILED},
    BriefState.PAUSED: {BriefState.RUNNING, BriefState.ARCHIVED, BriefState.FAILED},
    BriefState.COMPLETED: {BriefState.RUNNING, BriefState.ARCHIVED},
    BriefState.FAILED: {BriefState.DRAFT, BriefState.ARCHIVED},
    BriefState.ARCHIVED: set(),
}


class InvalidTransitionError(ValueError):
    """Raised when a caller asks for a state transition that is not allowed."""

    def __init__(self, current: BriefState, target: BriefState) -> None:
        self.current = current
        self.target = target
        super().__init__(
            f"invalid brief lifecycle transition: {current.value} -> {target.value}"
        )


def can_transition(current: BriefState, target: BriefState) -> bool:
    """Return True if ``current -> target`` is in the allowed table."""
    return target in LIFECYCLE_TRANSITIONS[current]


def assert_transition(current: BriefState, target: BriefState) -> None:
    """Raise ``InvalidTransitionError`` if the move is not allowed."""
    if not can_transition(current, target):
        raise InvalidTransitionError(current, target)
