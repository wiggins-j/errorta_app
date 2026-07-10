from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from errorta_app import server as server_mod
from errorta_council import paths as council_paths
from errorta_council.run_store import RunStore
from errorta_mobile import config as mobile_config
from errorta_mobile import devices as mobile_devices
from errorta_policy import PendingDecisionRequest, PendingDecisionStore, PolicyPhase


@pytest.fixture(autouse=True)
def _isolated_errorta_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    return tmp_path


def _device_headers(capabilities: dict[str, bool] | None = None) -> dict[str, str]:
    mobile_config.save({"enabled": True, "bind_mode": "loopback_dev"})
    token = "session-token"
    record = mobile_devices.create(
        display_name="Approval Phone",
        platform="ios",
        public_key="public-key",
        session_token=token,
    )
    # F065: approval caps are no longer granted by default — this "approval
    # phone" grants approve_low_risk unless the test specifies its own caps.
    caps = capabilities if capabilities is not None else {"approve_low_risk": True}
    mobile_devices.update_capabilities(record["device_id"], caps)
    return {
        "x-errorta-mobile-device-id": record["device_id"],
        "authorization": f"Bearer {token}",
    }


def _seed_decision(
    *,
    reason_code: str = "tool_consent_required",
    risk_class: str | None = "low",
    safe_request: dict[str, Any] | None = None,
) -> tuple[str, str]:
    runs = RunStore(runs_dir=council_paths.runs_dir())
    meta = runs.create_run(
        room_id="room-policy",
        room_snapshot={"id": "room-policy", "name": "Policy Room"},
        prompt="approve tool?",
        corpus_ids=[],
    )
    decision = PendingDecisionStore(runs_dir=runs.runs_dir).create(
        PendingDecisionRequest(
            run_id=meta.id,
            phase=PolicyPhase.TOOL_CALL,
            reason_code=reason_code,
            requester={"member_id": "reviewer", "name": "Reviewer"},
            safe_request=safe_request
            or {
                "tool_id": "local_lookup",
                "args_sha256": "abc123",
                "secret_token": "DO_NOT_LEAK",
            },
            risk_class=risk_class,
        )
    )
    return meta.id, decision.decision_id


def test_mobile_pending_decision_projection_is_safe() -> None:
    run_id, decision_id = _seed_decision()
    client = TestClient(server_mod.app)

    response = client.get("/mobile/v1/pending-decisions", headers=_device_headers())

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["decisions"][0]["decision_id"] == decision_id
    assert body["decisions"][0]["run_id"] == run_id
    assert body["decisions"][0]["title"] == "Allow local_lookup"
    assert body["decisions"][0]["risk"] == "low"
    assert body["decisions"][0]["actions"]["can_approve"] is True
    assert body["decisions"][0]["actions"]["required_capability"] == "approve_low_risk"
    serialized = json.dumps(body)
    assert "DO_NOT_LEAK" not in serialized
    assert "secret_token" not in serialized


def test_mobile_pending_decision_disables_action_without_capability() -> None:
    _seed_decision(reason_code="code_exec_required", risk_class="high",
                   safe_request={"tool_id": "code_exec", "args_sha256": "abc"})
    client = TestClient(server_mod.app)

    response = client.get("/mobile/v1/pending-decisions", headers=_device_headers())

    assert response.status_code == 200
    decision = response.json()["decisions"][0]
    assert decision["decision_class"] == "code_exec"
    assert decision["actions"]["can_approve"] is False
    assert decision["actions"]["required_capability"] == "approve_code_exec"


def test_mobile_approval_requires_matching_capability() -> None:
    run_id, decision_id = _seed_decision(
        reason_code="code_exec_required",
        risk_class="high",
        safe_request={"tool_id": "code_exec", "args_sha256": "abc"},
    )
    client = TestClient(server_mod.app)

    response = client.post(
        f"/mobile/v1/pending-decisions/{run_id}/{decision_id}/approve",
        json={"client_request_id": "req-1"},
        headers=_device_headers(),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "mobile_capability_forbidden:approve_code_exec"


def test_mobile_approval_is_idempotent_and_records_device_id() -> None:
    run_id, decision_id = _seed_decision()
    headers = _device_headers()
    client = TestClient(server_mod.app)

    approved = client.post(
        f"/mobile/v1/pending-decisions/{run_id}/{decision_id}/approve",
        json={"client_request_id": "req-1", "decision_revision": 1},
        headers=headers,
    )
    repeated = client.post(
        f"/mobile/v1/pending-decisions/{run_id}/{decision_id}/approve",
        json={"client_request_id": "req-1"},
        headers=headers,
    )

    assert approved.status_code == 200, approved.text
    assert repeated.status_code == 200, repeated.text
    decision = repeated.json()["decision"]
    assert decision["state"] == "approved"
    assert decision["resolved_by"].startswith("mobile_device:mob_dev_")
    assert repeated.json()["client_request_id"] == "req-1"


def test_mobile_approval_revision_conflict_returns_latest_decision() -> None:
    run_id, decision_id = _seed_decision()
    client = TestClient(server_mod.app)

    response = client.post(
        f"/mobile/v1/pending-decisions/{run_id}/{decision_id}/approve",
        json={"decision_revision": 99},
        headers=_device_headers(),
    )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "mobile_decision_revision_conflict"
    assert detail["decision"]["revision"] == 1


def test_revoked_device_cannot_list_mobile_pending_decisions() -> None:
    _seed_decision()
    headers = _device_headers()
    mobile_devices.revoke(headers["x-errorta-mobile-device-id"])
    client = TestClient(server_mod.app)

    response = client.get("/mobile/v1/pending-decisions", headers=headers)

    assert response.status_code == 401
    assert response.json()["detail"] == "mobile_device_revoked"
