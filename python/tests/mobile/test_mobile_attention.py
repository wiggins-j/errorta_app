from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from errorta_app import server as server_mod
from errorta_council import paths as council_paths
from errorta_council.run_store import RunStore
from errorta_council.schema import EventStatus, EventType
from errorta_mobile import config as mobile_config
from errorta_mobile import devices as mobile_devices
from errorta_policy import PendingDecisionRequest, PendingDecisionStore, PolicyPhase


@pytest.fixture(autouse=True)
def _isolated_errorta_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    return tmp_path


def _auth_headers(*, read_runs: bool = True) -> dict[str, str]:
    mobile_config.save({"enabled": True, "bind_mode": "loopback_dev"})
    token = "session-token"
    record = mobile_devices.create(
        display_name="Attention Phone",
        platform="ios",
        public_key="public-key",
        session_token=token,
    )
    if not read_runs:
        mobile_devices.update_capabilities(record["device_id"], {"read_runs": False})
    return {
        "x-errorta-mobile-device-id": record["device_id"],
        "authorization": f"Bearer {token}",
    }


def _create_run(prompt: str = "Needs attention") -> str:
    store = RunStore(runs_dir=council_paths.runs_dir())
    meta = store.create_run(
        room_id="room-attention",
        room_snapshot={"id": "room-attention", "name": "Attention Room"},
        prompt=prompt,
        corpus_ids=[],
    )
    return meta.id


def _add_pending_decision(run_id: str) -> None:
    PendingDecisionStore(runs_dir=council_paths.runs_dir()).create(
        PendingDecisionRequest(
            run_id=run_id,
            phase=PolicyPhase.TOOL_CALL,
            reason_code="tool_consent_required",
            requester={"member_id": "reviewer"},
            safe_request={"tool_id": "local_lookup", "secret": "DO_NOT_LEAK"},
            risk_class="low",
        )
    )


def test_attention_counts_pending_decisions_without_leaking_details() -> None:
    run_id = _create_run("Prompt text should not appear in notification payload")
    _add_pending_decision(run_id)
    client = TestClient(server_mod.app)

    response = client.get("/mobile/v1/attention", headers=_auth_headers())

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["needs_attention"] is True
    assert body["attention_count"] == 1
    assert body["pending_decision_count"] == 1
    assert body["runs"][0]["run_id"] == run_id
    assert body["runs"][0]["attention_reasons"] == ["pending_decision"]
    serialized = json.dumps(body)
    assert "DO_NOT_LEAK" not in serialized
    assert "Prompt text should not appear" not in serialized


def test_attention_includes_failed_runs() -> None:
    run_id = _create_run()
    store = RunStore(runs_dir=council_paths.runs_dir())
    writer = store.acquire_writer(run_id)
    try:
        store.append_event(
            run_id,
            type=EventType.RUN_FAILED,
            status=EventStatus.FAILED,
            payload={"reason": "model_error"},
            writer=writer,
        )
    finally:
        store.release_writer(writer)
    client = TestClient(server_mod.app)

    response = client.get("/mobile/v1/attention", headers=_auth_headers())

    assert response.status_code == 200
    assert response.json()["runs"][0]["attention_reasons"] == ["run_failed"]


def test_attention_requires_read_runs_capability() -> None:
    _create_run()
    client = TestClient(server_mod.app)

    response = client.get("/mobile/v1/attention", headers=_auth_headers(read_runs=False))

    assert response.status_code == 403
    assert response.json()["detail"] == "mobile_capability_forbidden:read_runs"
