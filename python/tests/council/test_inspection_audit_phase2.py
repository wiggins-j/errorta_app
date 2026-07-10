"""F031-08 Phase 2 audit-subset tests (read-only, Phase 0/1 data only)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from errorta_app.server import app
from errorta_council.fake_run import run_fake_council
from errorta_council.inspection_audit import (
    build_run_audit_summary,
    build_turn_audit,
)
from errorta_council.run_store import RunStore
from errorta_council.schema import EventType


@pytest.fixture
def client(tmp_errorta_home) -> TestClient:
    return TestClient(app)


def test_audit_summary_aggregates_phase0_event_data(tmp_errorta_home, runs_dir_path) -> None:
    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(
        room_id="rm", room_snapshot={"topology_kind": "round_robin", "members": []},
        prompt="hi", corpus_ids=[],
    )
    run_fake_council(store, meta.id, member_ids=["m1", "m2"])
    summary = build_run_audit_summary(store, meta.id)
    assert summary.totals.turns == 2
    assert summary.totals.completed == 2
    assert summary.totals.fake_calls == 2
    assert summary.totals.remote_calls == 0
    # All turns end up as destination_scope=fake.
    assert all(t.destination_scope == "fake" for t in summary.turns)


def test_turn_audit_returns_overview_and_after(tmp_errorta_home, runs_dir_path) -> None:
    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(
        room_id="rm", room_snapshot={"topology_kind": "round_robin", "members": []},
        prompt="hi", corpus_ids=[],
    )
    run_fake_council(store, meta.id, member_ids=["m1"])
    _, events = store.read_run(meta.id)
    msg = next(e for e in events if e.type == EventType.MEMBER_MESSAGE)
    overview, after = build_turn_audit(store, meta.id, msg.id)
    assert overview.run_id == meta.id
    assert overview.destination_scope == "fake"
    assert after.output_appended is True


def test_route_audit_summary_endpoint(client: TestClient) -> None:
    # Seed a room via the public route + run a dry-fake to populate events.
    payload = {
        "format_version": 1, "id": "rm-a", "name": "A", "description": "",
        "preset_id": None, "status_hint": "draft",
        "members": [
            {"id": "m-1", "name": "M1", "role": "answerer", "enabled": True,
             "gateway_route_id": "fake.local.deterministic", "provider_kind": "local",
             "provider_display": "Fake", "model_display": "deterministic",
             "catalog_version": "2026-06-11",
             "context_access": "prompt_only", "transcript_access": "own_messages",
             "turn_limits": {"max_messages": 1, "max_input_tokens": 1024,
                             "max_output_tokens": 256, "max_context_tokens": 1024},
             "generation": {"temperature": 0.0, "top_p": None, "seed": None},
             "system_prompt": "", "metadata": {}},
            {"id": "m-2", "name": "M2", "role": "critic", "enabled": True,
             "gateway_route_id": "fake.local.deterministic", "provider_kind": "local",
             "provider_display": "Fake", "model_display": "deterministic",
             "catalog_version": "2026-06-11",
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
        "context_policy": {"default_context_access": "prompt_only",
                           "default_transcript_access": "own_messages",
                           "allow_full_context": False,
                           "require_confirmation_for_remote_context": True,
                           "require_confirmation_for_full_context": True,
                           "member_overrides": {},
                           "redaction_profile_id": None, "summary_profile_id": None},
        "budget_policy": {"max_rounds": 1, "max_messages_per_member": 1,
                          "max_total_model_calls": 1, "max_remote_calls_per_run": 0,
                          "max_remote_calls_per_day": None,
                          "max_input_tokens_per_turn": 1024,
                          "max_output_tokens_per_turn": 256,
                          "max_context_tokens_per_member": 1024,
                          "max_estimated_usd_per_run": 0.0,
                          "max_estimated_usd_per_month": None,
                          "warn_at_fraction": [], "on_budget_exhausted": "stop",
                          "require_confirmation_before_first_remote_call": True,
                          "require_confirmation_above_estimated_usd": None},
        "finalization_policy": {"mode": "transcript_only",
                                 "finalizer_member_id": None,
                                 "judge_member_ids": [],
                                 "require_judge_verdict": False,
                                 "allow_minority_report": True,
                                 "allow_grounding_write": False,
                                 "grounding_requires_user_accept": True},
        "ui": {}, "created_at": "2026-06-11T00:00:00Z",
        "updated_at": "2026-06-11T00:00:00Z",
        "last_validated_at": None, "revision": 1,
    }
    client.post("/council/rooms", json=payload)
    r = client.post("/council/runs", json={"room_id": "rm-a", "prompt": "hi", "corpus_ids": [],
                                            "dry_fake_members": True})
    run_id = r.json()["run"]["id"]
    s = client.get(f"/council/runs/{run_id}/audit-summary")
    assert s.status_code == 200
    body = s.json()
    assert body["run_id"] == run_id
    assert body["totals"]["completed"] >= 1


def test_inspection_route_404_on_unknown_run(client: TestClient) -> None:
    """Phase 3 Task 12b: unknown run → 404 run_not_found."""
    r = client.get("/council/runs/does_not_exist/turns/anything/inspection")
    assert r.status_code == 404
    assert r.json()["detail"] == "run_not_found"


def test_inspection_audit_include_manifest_still_410(client: TestClient) -> None:
    """Phase 3 Task 12b boundary: the audit endpoint's manifest query-params
    stay 410 until the per-include projection layer ships in its own slice."""
    payload = {
        "format_version": 1, "id": "rm-i", "name": "I", "description": "",
        "preset_id": None, "status_hint": "draft",
        "members": [
            {"id": "m-1", "name": "M1", "role": "answerer", "enabled": True,
             "gateway_route_id": "fake.local.deterministic", "provider_kind": "local",
             "provider_display": "Fake", "model_display": "deterministic",
             "catalog_version": "2026-06-11", "context_access": "prompt_only",
             "transcript_access": "own_messages",
             "turn_limits": {"max_messages": 1, "max_input_tokens": 1024,
                             "max_output_tokens": 256, "max_context_tokens": 1024},
             "generation": {"temperature": 0.0, "top_p": None, "seed": None},
             "system_prompt": "x", "metadata": {}},
            {"id": "m-2", "name": "M2", "role": "answerer", "enabled": True,
             "gateway_route_id": "fake.local.deterministic", "provider_kind": "local",
             "provider_display": "Fake", "model_display": "deterministic",
             "catalog_version": "2026-06-11", "context_access": "prompt_only",
             "transcript_access": "own_messages",
             "turn_limits": {"max_messages": 1, "max_input_tokens": 1024,
                             "max_output_tokens": 256, "max_context_tokens": 1024},
             "generation": {"temperature": 0.0, "top_p": None, "seed": None},
             "system_prompt": "x", "metadata": {}},
        ],
        "topology": {"kind": "round_robin", "max_rounds": 1,
                     "max_messages_per_member": 1, "max_total_turns": 2,
                     "speaker_order": ["m-1", "m-2"], "stop_condition": None},
        "context_policy": {"default_context_access": "prompt_only",
                            "default_transcript_access": "own_messages",
                            "allow_full_context": False,
                            "require_confirmation_for_remote_context": True,
                            "require_confirmation_for_full_context": True},
        "budget_policy": {"max_rounds": 1, "max_messages_per_member": 1,
                          "max_total_model_calls": 2, "max_remote_calls_per_run": 0,
                          "max_remote_calls_per_day": None,
                          "max_input_tokens_per_turn": 1024,
                          "max_output_tokens_per_turn": 256,
                          "max_context_tokens_per_member": 1024,
                          "max_estimated_usd_per_run": 0.0,
                          "max_estimated_usd_per_month": None},
        "finalization_policy": {"mode": "transcript_only",
                                 "finalizer_member_id": None,
                                 "judge_member_ids": [],
                                 "require_judge_verdict": False,
                                 "allow_minority_report": True,
                                 "allow_grounding_write": False,
                                 "grounding_requires_user_accept": True},
        "ui": {}, "created_at": "2026-06-11T00:00:00Z",
        "updated_at": "2026-06-11T00:00:00Z",
        "last_validated_at": None, "revision": 1,
    }
    client.post("/council/rooms", json=payload)
    r = client.post(
        "/council/runs",
        json={"room_id": "rm-i", "prompt": "hi", "corpus_ids": [],
              "dry_fake_members": True},
    )
    run_id = r.json()["run"]["id"]
    # The Phase 2 audit endpoint still 410s on manifest-oriented query params.
    a = client.get(
        f"/council/runs/{run_id}/turns/anyturn/audit?include_manifest=1"
    )
    assert a.status_code == 410
    assert a.json()["detail"] == "inspection_phase_3_only"
