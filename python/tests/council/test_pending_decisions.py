"""F041 durable pending-decision store tests."""
from __future__ import annotations

import pytest

from errorta_policy import (
    PendingDecisionConflict,
    PendingDecisionNotFound,
    PendingDecisionRequest,
    PendingDecisionStore,
    PolicyPhase,
    PolicyStateWrite,
)


def _request(run_id: str = "run-1") -> PendingDecisionRequest:
    return PendingDecisionRequest(
        run_id=run_id,
        phase=PolicyPhase.TOOL_CALL,
        reason_code="tool_consent_required",
        requester={"member_id": "m-1"},
        safe_request={"tool_id": "web_fetch", "args_sha256": "abc"},
        state_writes_on_approve=(
            PolicyStateWrite(key="tool_consent:web_fetch", value=True),
        ),
    )


def test_pending_decision_create_list_and_restart_round_trip(runs_dir_path) -> None:
    store = PendingDecisionStore(runs_dir=runs_dir_path)
    created = store.create(_request())
    same = store.create(_request())

    assert same.decision_id == created.decision_id
    assert created.state == "pending"
    assert created.safe_request["args_sha256"] == "abc"

    restarted = PendingDecisionStore(runs_dir=runs_dir_path)
    listed = restarted.list("run-1")
    assert [r.decision_id for r in listed] == [created.decision_id]
    assert restarted.get("run-1", created.decision_id).reason_code == (
        "tool_consent_required"
    )


def test_approve_applies_state_writes_once(runs_dir_path) -> None:
    store = PendingDecisionStore(runs_dir=runs_dir_path)
    created = store.create(_request())

    approved = store.approve(
        "run-1", created.decision_id, resolved_by="user:alice"
    )
    approved_again = store.approve("run-1", created.decision_id)

    assert approved.state == "approved"
    assert approved.resolved_by == "user:alice"
    assert [w.key for w in approved.applied_state_writes] == [
        "tool_consent:web_fetch"
    ]
    assert approved_again.applied_state_writes == approved.applied_state_writes


def test_reject_conflicts_after_approve(runs_dir_path) -> None:
    store = PendingDecisionStore(runs_dir=runs_dir_path)
    created = store.create(_request())
    store.approve("run-1", created.decision_id)

    with pytest.raises(PendingDecisionConflict):
        store.reject("run-1", created.decision_id)


def test_path_traversal_ids_are_rejected(runs_dir_path) -> None:
    store = PendingDecisionStore(runs_dir=runs_dir_path)
    with pytest.raises(ValueError):
        store.get("../run", "pd-1")
    with pytest.raises(PendingDecisionNotFound):
        store.get("run-1", "pd-missing")
