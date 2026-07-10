from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from errorta_app import server as server_mod
from errorta_council import paths as council_paths
from errorta_council.room_store import RoomStore
from errorta_council.run_store import RunStore
from errorta_council.schema import (
    FORMAT_VERSION,
    BudgetPolicy,
    ContextPolicy,
    CouncilMember,
    CouncilRoom,
    EventStatus,
    EventType,
    FinalizationPolicy,
    MemberSnapshot,
    TopologyPolicy,
)
from errorta_mobile import config as mobile_config
from errorta_mobile import devices as mobile_devices
from errorta_mobile import inbox as mobile_inbox


@pytest.fixture(autouse=True)
def _isolated_errorta_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    return tmp_path


def _auth_headers(
    *,
    read_runs: bool = True,
    start_runs: bool = True,
    send_messages: bool = True,
    cancel_runs: bool = True,
) -> dict[str, str]:
    mobile_config.save({"enabled": True, "bind_mode": "loopback_dev"})
    token = "session-token"
    record = mobile_devices.create(
        display_name="Test iPhone",
        platform="ios",
        public_key="public-key",
        session_token=token,
    )
    mobile_devices.update_capabilities(
        record["device_id"],
        {
            "read_runs": read_runs,
            "start_runs": start_runs,
            "send_messages": send_messages,
            "cancel_runs": cancel_runs,
        },
    )
    return {
        "x-errorta-mobile-device-id": record["device_id"],
        "authorization": f"Bearer {token}",
    }


def _member_snapshot() -> MemberSnapshot:
    return MemberSnapshot(
        member_id="reviewer",
        name="Reviewer",
        role="reviewer",
        provider_display="Fake",
        model_display="deterministic",
        locality="fake",
        context_access="prompt_only",
        transcript_access="own_messages",
    )


def _seed_run() -> str:
    store = RunStore(runs_dir=council_paths.runs_dir())
    meta = store.create_run(
        room_id="room-mobile",
        room_snapshot={"id": "room-mobile", "name": "Mobile Room"},
        prompt="Check the mobile transcript projection",
        corpus_ids=[],
    )
    token = store.acquire_writer(meta.id)
    try:
        store.append_event(
            meta.id,
            type=EventType.RUN_STARTED,
            status=EventStatus.RUNNING,
            payload={},
            writer=token,
        )
        store.append_event(
            meta.id,
            type=EventType.MEMBER_MESSAGE,
            status=EventStatus.COMPLETED,
            payload={"content": "The visible review note."},
            member_id="reviewer",
            member_snapshot=_member_snapshot(),
            writer=token,
        )
        store.append_event(
            meta.id,
            type=EventType.TOOL_CALL_COMPLETED,
            status=EventStatus.COMPLETED,
            payload={
                "tool_id": "code_exec",
                "summary": "Ran parser tests.",
                "content_sha256": "abc123",
                "raw_tool_result": "SECRET RAW TOOL BYTES",
                "stdout": "SECRET STDOUT",
            },
            member_id="reviewer",
            member_snapshot=_member_snapshot(),
            writer=token,
        )
        store.append_event(
            meta.id,
            type=EventType.POLICY_DECISION_CREATED,
            status=EventStatus.PENDING,
            payload={
                "decision_id": "decision-1",
                "phase": "tool_call",
                "reason_code": "approval_required",
                "safe_request": {"tool_id": "code_exec"},
                "secret_internal_context": "do not send",
            },
            writer=token,
        )
    finally:
        store.release_writer(token)
    return meta.id


def _seed_room(room_id: str = "room-mobile", *, name: str = "Mobile Room") -> str:
    member = CouncilMember(
        id="reviewer",
        name="Reviewer",
        role="answerer",
        enabled=True,
        gateway_route_id="fake.local.deterministic",
        provider_kind="fake",
        provider_display="Fake",
        model_display="deterministic",
        catalog_version="2026-06-14",
        context_access="prompt_only",
        transcript_access="own_messages",
        turn_limits={
            "max_messages": 1,
            "max_input_tokens": 1024,
            "max_output_tokens": 256,
            "max_context_tokens": 1024,
        },
        generation={"temperature": 0.0, "top_p": None, "seed": None},
        system_prompt="Mobile test member.",
        metadata={},
    )
    room = CouncilRoom(
        format_version=FORMAT_VERSION,
        id=room_id,
        name=name,
        description="",
        members=[member],
        topology=TopologyPolicy(
            kind="round_robin",
            max_rounds=1,
            max_total_turns=1,
            max_messages_per_member=1,
            speaker_order=[member.id],
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
            max_remote_calls_per_day=None,
            max_input_tokens_per_turn=1024,
            max_output_tokens_per_turn=256,
            max_context_tokens_per_member=1024,
            max_estimated_usd_per_run=0.0,
            max_estimated_usd_per_month=None,
        ),
        finalization_policy=FinalizationPolicy(mode="transcript_only"),
        created_at="2026-06-14T00:00:00Z",
        updated_at="2026-06-14T00:00:00Z",
        revision=1,
    )
    store = RoomStore(
        rooms_dir=council_paths.rooms_dir(),
        deleted_dir=council_paths.deleted_rooms_dir(),
    )
    store.create(room)
    return room_id


def test_mobile_rooms_lists_desktop_built_rooms_for_new_prompt() -> None:
    _seed_room("room-a", name="Alpha Room")
    _seed_room("room-b", name="Beta Room")
    client = TestClient(server_mod.app)

    response = client.get("/mobile/v1/rooms", headers=_auth_headers())

    assert response.status_code == 200, response.text
    rooms = response.json()["rooms"]
    assert [r["room_id"] for r in rooms] == ["room-a", "room-b"]
    assert [r["name"] for r in rooms] == ["Alpha Room", "Beta Room"]
    assert rooms[0]["status_hint"] == "draft"
    assert rooms[0]["revision"] == 1


def test_mobile_rooms_requires_start_capability() -> None:
    _seed_room()
    client = TestClient(server_mod.app)

    response = client.get("/mobile/v1/rooms", headers=_auth_headers(start_runs=False))

    assert response.status_code == 403
    assert response.json()["detail"] == "mobile_capability_forbidden:start_runs"


def test_mobile_create_run_from_inbox_archives_handoff_item() -> None:
    room_id = _seed_room()
    headers = _auth_headers()
    item = mobile_inbox.create(
        device_id=headers["x-errorta-mobile-device-id"],
        kind="text",
        text="Use the mobile handoff as the prompt.",
    )
    client = TestClient(server_mod.app)

    response = client.post(
        "/mobile/v1/runs",
        headers=headers,
        json={
            "room_id": room_id,
            "source_inbox_item_id": item["inbox_item_id"],
            "dry_fake_members": True,
            "client_request_id": "client-1",
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["client_request_id"] == "client-1"
    assert body["source_inbox_item_id"] == item["inbox_item_id"]
    assert body["run"]["prompt"] == "Use the mobile handoff as the prompt."
    assert body["run"]["status"] == "completed"
    assert mobile_inbox.list_items(
        device_id=headers["x-errorta-mobile-device-id"],
        status="pending",
    ) == []
    archived = mobile_inbox.list_items(
        device_id=headers["x-errorta-mobile-device-id"],
        status="archived",
    )
    assert archived[0]["inbox_item_id"] == item["inbox_item_id"]


def test_mobile_create_run_requires_start_capability() -> None:
    room_id = _seed_room()
    client = TestClient(server_mod.app)

    response = client.post(
        "/mobile/v1/runs",
        headers=_auth_headers(start_runs=False),
        json={"room_id": room_id, "prompt": "Denied", "dry_fake_members": True},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "mobile_capability_forbidden:start_runs"


def test_mobile_create_run_returns_stable_error_for_unknown_room() -> None:
    client = TestClient(server_mod.app)

    response = client.post(
        "/mobile/v1/runs",
        headers=_auth_headers(),
        json={"room_id": "missing-room", "prompt": "Denied"},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "mobile_room_not_found"


def test_mobile_follow_up_routes_through_f049_interjection_and_archives_inbox() -> None:
    # A mobile follow-up MUST become a USER_INTERJECTION (the F049 mechanism the
    # context router actually delivers to the next member) — not a bespoke
    # MOBILE_MESSAGE event that the router silently ignores.
    run_id = _seed_run()
    headers = _auth_headers()
    item = mobile_inbox.create(
        device_id=headers["x-errorta-mobile-device-id"],
        kind="text",
        text="Follow up from the phone.",
    )
    client = TestClient(server_mod.app)

    response = client.post(
        f"/mobile/v1/runs/{run_id}/messages",
        headers=headers,
        json={
            "source_inbox_item_id": item["inbox_item_id"],
            "client_request_id": "follow-1",
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["accepted"] is True
    # The durable event is a USER_INTERJECTION the context router will fold in.
    store = RunStore(runs_dir=council_paths.runs_dir())
    _meta, events = store.read_run(run_id)
    interjections = [e for e in events if e.type == EventType.USER_INTERJECTION]
    assert len(interjections) == 1
    assert interjections[0].payload["content"] == "Follow up from the phone."
    assert interjections[0].payload["requested_by"].startswith("mobile_device:")
    # The phone still sees its own message in the transcript projection.
    projected = client.get(
        f"/mobile/v1/runs/{run_id}/events?after_sequence=4",
        headers=headers,
    ).json()["events"][0]
    assert projected["actor"]["kind"] == "mobile_device"
    assert projected["body"]["text"] == "Follow up from the phone."
    assert mobile_inbox.list_items(
        device_id=headers["x-errorta-mobile-device-id"],
        status="pending",
    ) == []


def test_mobile_follow_up_rejects_terminal_run() -> None:
    store = RunStore(runs_dir=council_paths.runs_dir())
    run_id = _seed_run()
    token = store.acquire_writer(run_id)
    try:
        store.append_event(
            run_id,
            type=EventType.RUN_COMPLETED,
            status=EventStatus.COMPLETED,
            payload={"reason": "done"},
            writer=token,
        )
    finally:
        store.release_writer(token)
    client = TestClient(server_mod.app)

    response = client.post(
        f"/mobile/v1/runs/{run_id}/messages",
        headers=_auth_headers(),
        json={"message": "Too late"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "mobile_run_terminal"


def test_mobile_cancel_run_is_gated_and_audited() -> None:
    run_id = _seed_run()
    client = TestClient(server_mod.app)

    denied = client.post(
        f"/mobile/v1/runs/{run_id}/cancel",
        headers=_auth_headers(cancel_runs=False),
        json={"reason": "No capability"},
    )

    assert denied.status_code == 403
    assert denied.json()["detail"] == "mobile_capability_forbidden:cancel_runs"

    allowed = client.post(
        f"/mobile/v1/runs/{run_id}/cancel",
        headers=_auth_headers(),
        json={"reason": "Stop from phone", "client_request_id": "cancel-1"},
    )

    assert allowed.status_code == 200, allowed.text
    body = allowed.json()
    assert body["client_request_id"] == "cancel-1"
    assert body["event"]["type"] == EventType.RUN_CANCEL_REQUESTED.value
    assert body["event"]["payload"]["reason"] == "Stop from phone"


def test_mobile_run_events_stream_yields_json_backlog() -> None:
    run_id = _seed_run()
    client = TestClient(server_mod.app)

    with client.stream(
        "GET",
        f"/mobile/v1/runs/{run_id}/events/stream?after_sequence=1",
        headers=_auth_headers(),
    ) as response:
        assert response.status_code == 200
        first = next(response.iter_text()).split("\n\n", 1)[0]

    assert first.startswith("data: ")
    payload = json.loads(first.removeprefix("data: ").strip())
    assert payload["type"] == "events"
    assert [event["sequence"] for event in payload["events"]] == [2, 3, 4]


def test_mobile_run_events_are_resumable_and_safe() -> None:
    run_id = _seed_run()
    client = TestClient(server_mod.app)

    response = client.get(
        f"/mobile/v1/runs/{run_id}/events?after_sequence=1",
        headers=_auth_headers(),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["run"]["run_id"] == run_id
    assert body["last_sequence"] == 4
    assert [event["sequence"] for event in body["events"]] == [2, 3, 4]
    assert body["events"][0]["actor"]["name"] == "Reviewer"
    assert body["events"][0]["body"]["text"] == "The visible review note."
    assert body["events"][1]["body"] == {
        "type": "tool_call",
        "tool_id": "code_exec",
        "status": "completed",
        "summary": "Ran parser tests.",
        "content_sha256": "abc123",
        "artifact_count": 0,
        "decision_id": None,
    }
    assert body["events"][2]["body"]["type"] == "pending_decision"

    serialized = json.dumps(body)
    assert "SECRET RAW TOOL BYTES" not in serialized
    assert "SECRET STDOUT" not in serialized
    assert "secret_internal_context" not in serialized
    assert "safe_request" not in serialized


def test_mobile_run_events_require_read_capability() -> None:
    run_id = _seed_run()
    client = TestClient(server_mod.app)

    response = client.get(
        f"/mobile/v1/runs/{run_id}/events",
        headers=_auth_headers(read_runs=False),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "mobile_capability_forbidden:read_runs"


def test_mobile_run_events_return_404_for_unknown_run() -> None:
    client = TestClient(server_mod.app)

    response = client.get(
        "/mobile/v1/runs/missing/events",
        headers=_auth_headers(),
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "mobile_run_not_found"


def test_mobile_run_list_rejects_unknown_status_filter() -> None:
    client = TestClient(server_mod.app)

    response = client.get("/mobile/v1/runs?status=everything", headers=_auth_headers())

    assert response.status_code == 422
