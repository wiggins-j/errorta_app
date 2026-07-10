"""Tests for errorta_briefs.lifecycle — BriefState FSM."""
from __future__ import annotations

import pytest

from errorta_briefs.lifecycle import (
    LIFECYCLE_TRANSITIONS,
    BriefState,
    InvalidTransitionError,
    assert_transition,
    can_transition,
)


def test_brief_state_has_seven_members() -> None:
    members = {s.name for s in BriefState}
    assert members == {
        "DRAFT",
        "VALIDATING",
        "RUNNING",
        "PAUSED",
        "COMPLETED",
        "FAILED",
        "ARCHIVED",
    }


def test_transitions_table_matches_spec_exactly() -> None:
    """Spec §5 diagram, encoded literally."""
    expected = {
        BriefState.DRAFT: {BriefState.VALIDATING},
        BriefState.VALIDATING: {BriefState.DRAFT, BriefState.RUNNING, BriefState.FAILED},
        BriefState.RUNNING: {BriefState.PAUSED, BriefState.COMPLETED, BriefState.FAILED},
        BriefState.PAUSED: {BriefState.RUNNING, BriefState.ARCHIVED, BriefState.FAILED},
        BriefState.COMPLETED: {BriefState.RUNNING, BriefState.ARCHIVED},
        BriefState.FAILED: {BriefState.DRAFT, BriefState.ARCHIVED},
        BriefState.ARCHIVED: set(),
    }
    assert LIFECYCLE_TRANSITIONS == expected


@pytest.mark.parametrize(
    "current,target",
    [
        (current, target)
        for current, targets in {
            BriefState.DRAFT: {BriefState.VALIDATING},
            BriefState.VALIDATING: {BriefState.DRAFT, BriefState.RUNNING, BriefState.FAILED},
            BriefState.RUNNING: {BriefState.PAUSED, BriefState.COMPLETED, BriefState.FAILED},
            BriefState.PAUSED: {BriefState.RUNNING, BriefState.ARCHIVED, BriefState.FAILED},
            BriefState.COMPLETED: {BriefState.RUNNING, BriefState.ARCHIVED},
            BriefState.FAILED: {BriefState.DRAFT, BriefState.ARCHIVED},
        }.items()
        for target in targets
    ],
)
def test_every_allowed_transition_passes(current: BriefState, target: BriefState) -> None:
    assert can_transition(current, target)
    assert_transition(current, target)  # must not raise


def test_every_disallowed_transition_raises() -> None:
    """For every (current, target) pair NOT in the table, assert_transition raises."""
    for current in BriefState:
        allowed = LIFECYCLE_TRANSITIONS[current]
        for target in BriefState:
            if target in allowed:
                continue
            assert not can_transition(current, target)
            with pytest.raises(InvalidTransitionError) as excinfo:
                assert_transition(current, target)
            assert excinfo.value.current == current
            assert excinfo.value.target == target


def test_archived_is_terminal() -> None:
    assert LIFECYCLE_TRANSITIONS[BriefState.ARCHIVED] == set()
    for target in BriefState:
        with pytest.raises(InvalidTransitionError):
            assert_transition(BriefState.ARCHIVED, target)


def test_invalid_transition_error_message_includes_state_names() -> None:
    with pytest.raises(InvalidTransitionError) as excinfo:
        assert_transition(BriefState.DRAFT, BriefState.COMPLETED)
    msg = str(excinfo.value)
    assert "DRAFT" in msg
    assert "COMPLETED" in msg
