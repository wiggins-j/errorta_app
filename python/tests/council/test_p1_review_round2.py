"""Regression tests for the second round of post-review findings.

Locks:
- blocked_by_policy rooms (full_context_not_allowed) are rejected at POST /council/runs (P1).
- Engine-backed fake runs report fake_calls > 0 / local_calls correctly in
  the audit summary (P1).
- Blocked turns are counted as blocked (not skipped) in audit totals (P1).
- /turns/{id}/audit returns 410 on manifest-oriented query params (P2).
"""
from __future__ import annotations

import time
from dataclasses import replace as _replace

import pytest
from fastapi.testclient import TestClient

from errorta_app import server as server_mod
from errorta_council import paths as council_paths
from errorta_council.fake_run import run_fake_council
from errorta_council.inspection_audit import build_run_audit_summary
from errorta_council.room_store import RoomStore
from errorta_council.run_store import RunStore
from errorta_council.schema import (
    BudgetPolicy,
    ContextPolicy,
    CouncilMember,
    CouncilRoom,
    EventStatus,
    EventType,
    FORMAT_VERSION,
    FinalizationPolicy,
    MemberSnapshot,
    TopologyPolicy,
)


@pytest.fixture
def client(tmp_errorta_home):
    return TestClient(server_mod.app)


def _full_context_room() -> CouncilRoom:
    """Two-member room where one requests full_context but allow_full_context=False.

    Two enabled members satisfies the F031-03 MVP constraint; the second
    member having full_context against allow_full_context=False is the
    blocked_by_policy condition we want to lock.
    """
    def _m(idx: int, context_access: str) -> CouncilMember:
        mid = f"m-{idx}"
        return CouncilMember(
            id=mid, name=f"m{idx}", role="answerer", enabled=True,
            gateway_route_id="fake.local.stub-model", provider_kind="local",
            provider_display="Fake", model_display="stub-model",
            catalog_version="2026-06-11",
            context_access=context_access,
            transcript_access="own_messages",
            turn_limits={"max_messages": 1, "max_input_tokens": 1024,
                         "max_output_tokens": 256, "max_context_tokens": 1024},
            generation={"temperature": 0.0, "top_p": None, "seed": None},
            system_prompt="", metadata={},
        )
    return CouncilRoom(
        format_version=FORMAT_VERSION, id="rm-fcblock", name="x", description="",
        members=[_m(1, "prompt_only"), _m(2, "full_context")],
        topology=TopologyPolicy(
            kind="round_robin", max_rounds=1, max_total_turns=2,
            max_messages_per_member=1, speaker_order=["m-1", "m-2"],
        ),
        context_policy=ContextPolicy(
            default_context_access="prompt_only",
            default_transcript_access="own_messages",
            allow_full_context=False,             # ← policy forbids it
            require_confirmation_for_remote_context=True,
            require_confirmation_for_full_context=True,
        ),
        budget_policy=BudgetPolicy(
            max_rounds=1, max_messages_per_member=1, max_total_model_calls=2,
            max_remote_calls_per_run=0, max_remote_calls_per_day=None,
            max_input_tokens_per_turn=1024, max_output_tokens_per_turn=256,
            max_context_tokens_per_member=1024,
            max_estimated_usd_per_run=0.0, max_estimated_usd_per_month=None,
        ),
        finalization_policy=FinalizationPolicy(mode="transcript_only"),
        created_at="2026-06-11T00:00:00Z",
        updated_at="2026-06-11T00:00:00Z",
        revision=1,
    )


def test_full_context_blocked_room_rejected_at_create_run(client: TestClient) -> None:
    """P1: blocked_by_policy / full_context_not_allowed must NOT launch."""
    room = _full_context_room()
    RoomStore(
        rooms_dir=council_paths.rooms_dir(),
        deleted_dir=council_paths.deleted_rooms_dir(),
    ).create(room)
    r = client.post(
        "/council/runs",
        json={"room_id": room.id, "prompt": "hi", "corpus_ids": []},
    )
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "room_not_runnable"
    assert detail["status"] == "blocked_by_policy"
    assert any(e["code"] == "full_context_not_allowed" for e in detail["errors"])


def test_dry_fake_run_reports_fake_calls(
    client: TestClient, seed_room_full
) -> None:
    """P1: a fake run must report fake_calls > 0 / local_calls = 0.

    The Phase 0 dry_fake path attaches MemberSnapshot(locality="fake"), so
    the audit aggregator classifies the call as fake.
    """
    room = seed_room_full(member_count=2, provider="fake",
                         model="stub-model", max_rounds=1)
    r = client.post(
        "/council/runs",
        json={"room_id": room.id, "prompt": "hi", "corpus_ids": [],
              "dry_fake_members": True},
    )
    assert r.status_code == 200, r.text
    run_id = r.json()["run"]["id"]
    summary = client.get(f"/council/runs/{run_id}/audit-summary").json()
    assert summary["totals"]["fake_calls"] == 2
    assert summary["totals"]["local_calls"] == 0


def test_engine_backed_fake_run_reports_fake_calls(
    client: TestClient, seed_room_full
) -> None:
    """P1: a non-dry_fake fake-provider run still reports fake_calls > 0.

    The scheduler attaches member_snapshot.locality="fake" inferred from
    the room snapshot's gateway_route_id / provider, so the audit
    aggregator no longer misclassifies fake providers as local.
    """
    room = seed_room_full(member_count=2, provider="fake",
                         model="stub-model", max_rounds=1)
    r = client.post(
        "/council/runs",
        json={"room_id": room.id, "prompt": "hi", "corpus_ids": []},
    )
    assert r.status_code in (200, 201), r.text
    run_id = r.json()["run"]["id"]
    # Poll for terminal.
    for _ in range(60):
        meta = client.get(f"/council/runs/{run_id}").json()["run"]
        if meta["status"] in ("completed", "failed", "cancelled"):
            break
        time.sleep(0.05)
    summary = client.get(f"/council/runs/{run_id}/audit-summary").json()
    assert summary["totals"]["fake_calls"] == 2
    assert summary["totals"]["local_calls"] == 0
    assert summary["totals"]["completed"] == 2


def test_blocked_turn_counted_as_blocked(
    tmp_errorta_home, runs_dir_path
) -> None:
    """P1: MEMBER_SKIPPED with status=BLOCKED increments blocked, not skipped."""
    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(
        room_id="rm", room_snapshot={"topology_kind": "round_robin", "members": []},
        prompt="hi", corpus_ids=[],
    )
    snap = MemberSnapshot(
        member_id="m-1", name="M1", role="answerer",
        provider_display="Ollama", model_display="x",
        locality="local", context_access="prompt_only",
        transcript_access="own_messages", catalog_version=None,
    )
    token = store.acquire_writer(meta.id)
    try:
        store.append_event(
            meta.id, type=EventType.RUN_STARTED,
            status=EventStatus.RUNNING, payload={}, writer=token,
        )
        store.append_event(
            meta.id, type=EventType.MEMBER_SKIPPED,
            status=EventStatus.BLOCKED,
            payload={"reason": "local_model_missing"},
            member_id="m-1", member_snapshot=snap, writer=token,
        )
    finally:
        store.release_writer(token)
    summary = build_run_audit_summary(store, meta.id)
    assert summary.totals.blocked == 1
    assert summary.totals.skipped == 0
    assert summary.totals.turns == 1


def test_turn_audit_rejects_manifest_query_params(
    client: TestClient, seed_room_full
) -> None:
    """P2: ?include_manifest=1 (and friends) → 410 inspection_phase_3_only."""
    room = seed_room_full(member_count=2, provider="fake",
                         model="stub-model", max_rounds=1)
    r = client.post(
        "/council/runs",
        json={"room_id": room.id, "prompt": "hi", "corpus_ids": [],
              "dry_fake_members": True},
    )
    assert r.status_code == 200, r.text
    run_id = r.json()["run"]["id"]
    _, events = RunStore(runs_dir=council_paths.runs_dir()).read_run(run_id)
    msg = next(e for e in events if e.type == EventType.MEMBER_MESSAGE)
    # Without forbidden params: 200.
    ok = client.get(f"/council/runs/{run_id}/turns/{msg.id}/audit")
    assert ok.status_code == 200
    # With forbidden param: 410.
    blocked = client.get(
        f"/council/runs/{run_id}/turns/{msg.id}/audit?include_manifest=1"
    )
    assert blocked.status_code == 410
    assert blocked.json()["detail"] == "inspection_phase_3_only"
