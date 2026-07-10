"""P1 / P2 regression tests landed alongside the post-review fixes.

Locks:
- POST /council/runs rejects non-runnable rooms (P1: readiness gate).
- Scheduler-thread crash surfaces as a terminal ``run_failed`` event (P1: no silent strands).
- RunMeta counters + terminal_reason advance after MEMBER_MESSAGE / RUN_COMPLETED (P2).
- RUN_CANCEL_REQUESTED durably sets cancel_requested_at on RunMeta (P2).
- FastAPI lifespan calls Council recovery at startup (P1).
"""
from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from errorta_council.fake_run import run_fake_council
from errorta_council.run_store import RunStore
from errorta_council.schema import EventStatus, EventType
from errorta_app import server as server_mod


def _bare_room_payload(rid: str) -> dict:
    """A room payload that is missing the required policy blocks → invalid."""
    return {
        "format_version": 1, "id": rid, "name": "Bad", "description": "",
        "members": [],
        "topology": {"kind": "round_robin"},
        "context_policy": {},
        "budget_policy": {},
        "finalization_policy": {"mode": "transcript_only"},
        "ui": {}, "created_at": "2026-06-11T00:00:00Z",
        "updated_at": "2026-06-11T00:00:00Z",
        "last_validated_at": None, "revision": 1,
    }


@pytest.fixture
def client(tmp_errorta_home):
    return TestClient(server_mod.app)


def test_create_run_rejects_unknown_room(client: TestClient) -> None:
    r = client.post(
        "/council/runs",
        json={"room_id": "ghost", "prompt": "p", "corpus_ids": []},
    )
    assert r.status_code == 404


def test_create_run_rejects_non_runnable_room(
    client: TestClient, seed_room_full
) -> None:
    """Room with an enabled member whose route is unknown → 422 room_not_runnable."""
    from errorta_council import paths as council_paths
    from errorta_council.room_store import RoomStore
    from errorta_council.schema import (
        BudgetPolicy,
        ContextPolicy,
        CouncilMember,
        CouncilRoom,
        FORMAT_VERSION,
        FinalizationPolicy,
        TopologyPolicy,
    )

    bad_member = CouncilMember(
        id="m-x", name="Bad", role="answerer", enabled=True,
        gateway_route_id="anthropic.claude-sonnet-4-6",  # not local.* / fake.* → unknown
        provider_kind="remote",
        provider_display="Anthropic", model_display="claude",
        catalog_version="2026-06-11",
        context_access="prompt_only", transcript_access="own_messages",
        turn_limits={"max_messages": 1, "max_input_tokens": 1024,
                     "max_output_tokens": 256, "max_context_tokens": 1024},
        generation={"temperature": 0.0, "top_p": None, "seed": None},
        system_prompt="", metadata={},
    )
    room = CouncilRoom(
        format_version=FORMAT_VERSION, id="rm-not-runnable", name="x", description="",
        members=[bad_member],
        topology=TopologyPolicy(kind="round_robin", max_rounds=1, max_total_turns=1,
                                max_messages_per_member=1, speaker_order=["m-x"]),
        context_policy=ContextPolicy(
            default_context_access="prompt_only",
            default_transcript_access="own_messages",
            allow_full_context=False,
            require_confirmation_for_remote_context=True,
            require_confirmation_for_full_context=True,
        ),
        budget_policy=BudgetPolicy(
            max_rounds=1, max_messages_per_member=1, max_total_model_calls=1,
            max_remote_calls_per_run=1, max_remote_calls_per_day=None,
            max_input_tokens_per_turn=1024, max_output_tokens_per_turn=256,
            max_context_tokens_per_member=1024,
            max_estimated_usd_per_run=1.0, max_estimated_usd_per_month=None,
        ),
        finalization_policy=FinalizationPolicy(mode="transcript_only"),
        created_at="2026-06-11T00:00:00Z",
        updated_at="2026-06-11T00:00:00Z",
        revision=1,
    )
    store = RoomStore(
        rooms_dir=council_paths.rooms_dir(),
        deleted_dir=council_paths.deleted_rooms_dir(),
    )
    store.create(room)

    r = client.post(
        "/council/runs",
        json={"room_id": "rm-not-runnable", "prompt": "p", "corpus_ids": []},
    )
    assert r.status_code == 422, r.text
    body = r.json()
    detail = body["detail"]
    assert detail["code"] == "room_not_runnable"
    assert detail["status"] in {"needs_provider", "invalid"}


def test_run_meta_counters_advance_after_fake_run(
    tmp_errorta_home, runs_dir_path
) -> None:
    """RUN_MEMBER_MESSAGE events project into RunMeta counters (P2)."""
    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(
        room_id="rm",
        room_snapshot={"topology_kind": "round_robin", "members": []},
        prompt="hi",
        corpus_ids=[],
    )
    run_fake_council(store, meta.id, member_ids=["m-1", "m-2"])
    fresh, _ = store.read_run(meta.id)
    assert fresh.total_messages_completed == 2
    assert fresh.completed_messages_by_member == {"m-1": 1, "m-2": 1}
    assert fresh.terminal_reason == "topology_exhausted"
    assert fresh.status == "completed"


def test_cancel_requested_at_durably_projected(
    tmp_errorta_home, runs_dir_path
) -> None:
    """RUN_CANCEL_REQUESTED writes cancel_requested_at into RunMeta (P2)."""
    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(
        room_id="rm",
        room_snapshot={"topology_kind": "round_robin", "members": []},
        prompt="hi",
        corpus_ids=[],
    )
    token = store.acquire_writer(meta.id)
    try:
        store.append_event(
            meta.id, type=EventType.RUN_STARTED,
            status=EventStatus.RUNNING, payload={}, writer=token,
        )
        store.append_event(
            meta.id, type=EventType.RUN_CANCEL_REQUESTED,
            status=EventStatus.CANCEL_REQUESTED,
            payload={"requested_by": "user", "reason": "ui_stop_button"},
            writer=token,
        )
    finally:
        store.release_writer(token)
    fresh, _ = store.read_run(meta.id)
    assert fresh.cancel_requested_at is not None
    # Run is still non-terminal until RUN_CANCELLED is written.
    assert fresh.status == "running"


def test_scheduler_thread_crash_emits_run_failed_terminal(
    client: TestClient, monkeypatch, seed_room_full
) -> None:
    """If the scheduler thread raises, a terminal RUN_FAILED is emitted (P1)."""
    from errorta_app.routes import council as council_routes

    async def _boom(**_kw):
        raise RuntimeError("simulated_scheduler_crash")

    # Patch the symbol the route binds at import time.
    monkeypatch.setattr(council_routes, "build_and_run", _boom)

    room = seed_room_full(
        room_id="rm-crash", member_count=2, provider="fake",
        model="stub-model", max_rounds=1, max_messages_per_member=1,
    )
    r = client.post(
        "/council/runs",
        json={"room_id": room.id, "prompt": "p", "corpus_ids": []},
    )
    assert r.status_code in (200, 201), r.text
    run_id = r.json()["run"]["id"]

    # Poll until terminal (the daemon thread emits RUN_FAILED).
    for _ in range(60):
        meta = client.get(f"/council/runs/{run_id}").json()["run"]
        if meta["status"] in ("failed", "cancelled", "completed"):
            break
        time.sleep(0.05)

    final = client.get(f"/council/runs/{run_id}").json()
    assert final["run"]["status"] == "failed"
    assert final["run"]["terminal_reason"] == "gateway_error"
    types = [e["type"] for e in final["events"]]
    assert types[-1] == "run_failed"


def test_lifespan_runs_council_recovery_at_startup(tmp_errorta_home) -> None:
    """FastAPI lifespan invokes scan_and_recover() (P1)."""
    # Seed an orphan running run.
    from errorta_council import paths as council_paths
    store = RunStore(runs_dir=council_paths.runs_dir())
    meta = store.create_run(
        room_id="rm",
        room_snapshot={"topology_kind": "round_robin", "members": []},
        prompt="p",
        corpus_ids=[],
    )
    token = store.acquire_writer(meta.id)
    try:
        store.append_event(
            meta.id, type=EventType.RUN_STARTED,
            status=EventStatus.RUNNING, payload={}, writer=token,
        )
    finally:
        store.release_writer(token)

    # Mounting TestClient as a context manager triggers lifespan startup.
    with TestClient(server_mod.app):
        pass

    # After startup, the orphan should be marked interrupted.
    fresh, _ = store.read_run(meta.id)
    assert fresh.status == "interrupted"
