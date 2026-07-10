"""F031-01/F031-02 — /council/* route layer tests.

Route shapes mirror the briefs router pattern in
``errorta_app.routes.briefs``. Architecture-spec OQ#2 (cancel terminal)
resolution: 409 Conflict at the route layer.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_errorta_home: Path) -> TestClient:
    # Import after monkeypatched HOME so the app picks up the temp root.
    from errorta_app.server import app  # noqa: WPS433
    return TestClient(app)


def _room_payload(rid: str = "room-1") -> dict:
    return {
        "format_version": 1, "id": rid, "name": "Phase 0", "description": "",
        "preset_id": None, "status_hint": "draft",
        "members": [
            {"id": "m-1", "name": "M1", "role": "answerer", "enabled": True,
             "gateway_route_id": "fake.local.deterministic",
             "provider_kind": "local", "provider_display": "Fake",
             "model_display": "deterministic", "catalog_version": "2026-06-11",
             "context_access": "prompt_only", "transcript_access": "own_messages",
             "turn_limits": {"max_messages": 1, "max_input_tokens": 1024,
                             "max_output_tokens": 256, "max_context_tokens": 1024},
             "generation": {"temperature": 0.0, "top_p": None, "seed": None},
             "system_prompt": "", "metadata": {}},
            {"id": "m-2", "name": "M2", "role": "critic", "enabled": True,
             "gateway_route_id": "fake.local.deterministic",
             "provider_kind": "local", "provider_display": "Fake",
             "model_display": "deterministic", "catalog_version": "2026-06-11",
             "context_access": "prompt_only", "transcript_access": "own_messages",
             "turn_limits": {"max_messages": 1, "max_input_tokens": 1024,
                             "max_output_tokens": 256, "max_context_tokens": 1024},
             "generation": {"temperature": 0.0, "top_p": None, "seed": None},
             "system_prompt": "", "metadata": {}},
        ],
        "topology": {"kind": "round_robin", "max_rounds": 1,
                     "max_total_turns": 2, "max_messages_per_member": 1,
                     "speaker_order": ["m-1", "m-2"],
                     "allow_user_interjection": False, "stop_when": {}},
        "context_policy": {
            "default_context_access": "prompt_only",
            "default_transcript_access": "own_messages",
            "allow_full_context": False,
            "require_confirmation_for_remote_context": True,
            "require_confirmation_for_full_context": True,
            "member_overrides": {},
            "redaction_profile_id": None, "summary_profile_id": None,
        },
        "budget_policy": {
            "max_rounds": 1, "max_messages_per_member": 1,
            "max_total_model_calls": 2, "max_remote_calls_per_run": 0,
            "max_remote_calls_per_day": None,
            "max_input_tokens_per_turn": 1024, "max_output_tokens_per_turn": 256,
            "max_context_tokens_per_member": 1024,
            "max_estimated_usd_per_run": 0.0, "max_estimated_usd_per_month": None,
            "warn_at_fraction": [0.5, 0.8], "on_budget_exhausted": "stop",
            "require_confirmation_before_first_remote_call": True,
            "require_confirmation_above_estimated_usd": None,
        },
        "finalization_policy": {
            "mode": "transcript_only", "finalizer_member_id": None,
            "judge_member_ids": [], "require_judge_verdict": False,
            "allow_minority_report": True, "allow_grounding_write": False,
            "grounding_requires_user_accept": True,
        },
        "ui": {}, "created_at": "2026-06-11T00:00:00Z",
        "updated_at": "2026-06-11T00:00:00Z",
        "last_validated_at": None, "revision": 1,
    }


def test_healthz_advertises_council_true(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["council"] is True


def test_create_and_get_room(client: TestClient) -> None:
    r = client.post("/council/rooms", json=_room_payload())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["room"]["id"] == "room-1"
    g = client.get("/council/rooms/room-1")
    assert g.status_code == 200
    assert g.json()["room"]["id"] == "room-1"
    assert g.json()["validation"]["status"] in {"ready", "needs_provider",
                                                 "blocked_by_policy", "draft", "invalid"}


def test_get_missing_room_returns_404(client: TestClient) -> None:
    r = client.get("/council/rooms/ghost")
    assert r.status_code == 404


def test_validate_unsaved_payload(client: TestClient) -> None:
    r = client.post("/council/rooms/validate", json=_room_payload(rid="draft"))
    assert r.status_code == 200
    assert "status" in r.json()


def test_put_room_accepts_minimal_added_member(client: TestClient) -> None:
    """Regression: the room editor adds a member as a MINIMAL dict (no role /
    provider_display / model_display / catalog_version / turn_limits /
    generation). The PUT must succeed (200), not raise a CORS-less 500 that the
    webview surfaces as "sidecar_unreachable". See errorta_council.schema
    CouncilMember defaults + the update_room 422 guard."""
    client.post("/council/rooms", json=_room_payload())
    room = _room_payload()
    # exactly what src/features/council memberToRaw emits for a freshly-added member:
    room["members"].append({
        "id": "m-new", "name": "Beacon", "enabled": True,
        "provider_kind": "local",
        "gateway_route_id": "fake.local.deterministic",
        "context_access": "full_context", "transcript_access": "all_messages",
        "system_prompt": "",
    })
    room["topology"]["speaker_order"] = [m["id"] for m in room["members"]]
    r = client.put("/council/rooms/room-1",
                   json={"expected_revision": 1, "room": room})
    assert r.status_code == 200, r.text
    members = r.json()["room"]["members"]
    assert [m["id"] for m in members] == ["m-1", "m-2", "m-new"]
    added = members[-1]
    # defaults filled in for the omitted fields:
    assert added["role"] == "answerer"
    assert added["turn_limits"] == {}
    assert added["generation"] == {}
    assert added["context_access"] == "full_context"


def test_put_room_malformed_member_returns_422_not_500(client: TestClient) -> None:
    """A genuinely un-deserializable member yields a clean 422 (which carries
    CORS headers), not an unhandled 500 (which does not) — so the UI shows the
    real validation error instead of a misleading 'backend unreachable'."""
    client.post("/council/rooms", json=_room_payload())
    room = _room_payload()
    # a member with no `id` (still required) — CouncilMember(**m) raises TypeError:
    room["members"].append({"name": "NoId", "provider_kind": "local"})
    r = client.put("/council/rooms/room-1",
                   json={"expected_revision": 1, "room": room})
    assert r.status_code == 422, r.text  # clean 422, never an unhandled 500
    assert r.json()["detail"]["code"] == "invalid_room"


def test_clone_room(client: TestClient) -> None:
    client.post("/council/rooms", json=_room_payload())
    r = client.post(
        "/council/rooms/room-1/clone",
        json={"new_id": "room-2", "new_name": "Cloned"},
    )
    assert r.status_code == 200
    assert r.json()["room"]["id"] == "room-2"


def test_create_run_dry_fake(client: TestClient) -> None:
    client.post("/council/rooms", json=_room_payload())
    r = client.post(
        "/council/runs",
        json={"room_id": "room-1", "prompt": "p", "corpus_ids": [],
              "conversation_id": None, "conversation_turn_id": None,
              "dry_fake_members": True},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["run"]["status"] == "completed"
    events = body["events"]
    assert events[0]["type"] == "run_started"
    assert events[-1]["type"] == "run_completed"


def test_create_run_uses_room_corpus_ids_when_omitted(client: TestClient) -> None:
    room = _room_payload()
    room["corpus_ids"] = ["room-default"]
    client.post("/council/rooms", json=room)
    r = client.post(
        "/council/runs",
        json={"room_id": "room-1", "prompt": "p", "dry_fake_members": True},
    )
    assert r.status_code == 200, r.text
    assert r.json()["run"]["corpus_ids"] == ["room-default"]


def test_create_run_explicit_empty_corpus_ids_overrides_room_default(
    client: TestClient,
) -> None:
    room = _room_payload()
    room["corpus_ids"] = ["room-default"]
    client.post("/council/rooms", json=room)
    r = client.post(
        "/council/runs",
        json={
            "room_id": "room-1",
            "prompt": "p",
            "corpus_ids": [],
            "dry_fake_members": True,
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["run"]["corpus_ids"] == []


def test_create_run_per_run_corpus_ids_override_room_default(
    client: TestClient,
) -> None:
    room = _room_payload()
    room["corpus_ids"] = ["room-default"]
    client.post("/council/rooms", json=room)
    r = client.post(
        "/council/runs",
        json={
            "room_id": "room-1",
            "prompt": "p",
            "corpus_ids": ["per-run"],
            "dry_fake_members": True,
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["run"]["corpus_ids"] == ["per-run"]


def test_events_after_sequence(client: TestClient) -> None:
    client.post("/council/rooms", json=_room_payload())
    rr = client.post(
        "/council/runs",
        json={"room_id": "room-1", "prompt": "p", "corpus_ids": [],
              "conversation_id": None, "conversation_turn_id": None,
              "dry_fake_members": True},
    )
    run_id = rr.json()["run"]["id"]
    r = client.get(f"/council/runs/{run_id}/events", params={"after_sequence": 2})
    body = r.json()
    assert body["run_id"] == run_id
    assert all(e["sequence"] > 2 for e in body["events"])
    assert body["terminal"] is True


def test_cancel_completed_run_returns_409(client: TestClient) -> None:
    """Architecture-spec OQ#2: cancelling a terminal run is 409 Conflict."""
    client.post("/council/rooms", json=_room_payload())
    rr = client.post(
        "/council/runs",
        json={"room_id": "room-1", "prompt": "p", "corpus_ids": [],
              "conversation_id": None, "conversation_turn_id": None,
              "dry_fake_members": True},
    )
    run_id = rr.json()["run"]["id"]
    r = client.post(f"/council/runs/{run_id}/cancel", json={"reason": "ui"})
    assert r.status_code == 409


def test_list_runs_filters_by_room(client: TestClient) -> None:
    client.post("/council/rooms", json=_room_payload())
    client.post(
        "/council/runs",
        json={"room_id": "room-1", "prompt": "p", "corpus_ids": [],
              "conversation_id": None, "conversation_turn_id": None,
              "dry_fake_members": True},
    )
    r = client.get("/council/runs", params={"room_id": "room-1"})
    assert r.status_code == 200
    assert len(r.json()["runs"]) >= 1
