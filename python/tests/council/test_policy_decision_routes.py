"""F041 pending-decision route coverage."""
from __future__ import annotations

from fastapi.testclient import TestClient

from errorta_app import server as server_mod
from errorta_council.run_store import RunStore
from errorta_policy import (
    PendingDecisionRequest,
    PendingDecisionStore,
    PolicyPhase,
    PolicyStateWrite,
)


def _seed_pending_decision(runs_dir_path):
    runs = RunStore(runs_dir=runs_dir_path)
    meta = runs.create_run(
        room_id="rm-policy",
        room_snapshot={"members": []},
        prompt="approve tool?",
        corpus_ids=[],
    )
    decision = PendingDecisionStore(runs_dir=runs.runs_dir).create(
        PendingDecisionRequest(
            run_id=meta.id,
            phase=PolicyPhase.TOOL_CALL,
            reason_code="tool_consent_required",
            requester={"member_id": "m-1"},
            safe_request={"tool_id": "web_fetch", "args_sha256": "abc"},
            state_writes_on_approve=(
                PolicyStateWrite(key="tool_consent:web_fetch", value=True),
            ),
        )
    )
    return meta, decision


def test_pending_decision_routes_list_and_approve(
    tmp_errorta_home, runs_dir_path
) -> None:
    meta, decision = _seed_pending_decision(runs_dir_path)
    client = TestClient(server_mod.app)

    listed = client.get(f"/council/runs/{meta.id}/pending-decisions")
    assert listed.status_code == 200, listed.text
    assert listed.json()["decisions"][0]["decision_id"] == decision.decision_id

    denied = client.post(
        f"/council/runs/{meta.id}/pending-decisions/{decision.decision_id}/approve",
        json={},
    )
    assert denied.status_code == 403

    approved = client.post(
        f"/council/runs/{meta.id}/pending-decisions/{decision.decision_id}/approve",
        json={"resolved_by": "user:alice"},
        headers={"X-Errorta-Origin": "tauri-ui"},
    )
    assert approved.status_code == 200, approved.text
    body = approved.json()["decision"]
    assert body["state"] == "approved"
    assert body["resolved_by"] == "user:alice"
    assert body["applied_state_writes"][0]["key"] == "tool_consent:web_fetch"


def test_pending_decision_route_rejects_bad_state_filter(
    tmp_errorta_home, runs_dir_path
) -> None:
    meta, _decision = _seed_pending_decision(runs_dir_path)
    client = TestClient(server_mod.app)

    response = client.get(
        f"/council/runs/{meta.id}/pending-decisions?state=not-a-state"
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "unknown_pending_decision_state"
