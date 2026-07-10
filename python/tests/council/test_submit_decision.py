"""F031-09 submit_decision durable projection.

Locks the post-review P2 hardening: a decision is persisted into RunMeta
(so recovery and the audit drawer can observe it), and a choice="stop"
durably triggers cancellation.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from errorta_app import server as server_mod
from errorta_council.control import RunControl
from errorta_council.run_store import RunStore
from errorta_council.schema import EventStatus, EventType


@pytest.fixture
def seeded_run(tmp_errorta_home, runs_dir_path):
    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(
        room_id="rm",
        room_snapshot={"topology_kind": "round_robin", "members": []},
        prompt="hi", corpus_ids=[],
    )
    # F031-09: decisions only resolve an ask-pause. The route layer enforces
    # this; tests must seed the same state. Park the run in
    # ``awaiting_user_decision`` so submit_decision is applicable.
    store.merge_meta_fields(meta.id, status="awaiting_user_decision")
    return store, store.read_run(meta.id)[0]


@pytest.mark.asyncio
async def test_submit_decision_persists_last_decision_into_meta(seeded_run) -> None:
    store, meta = seeded_run
    ctl = RunControl(run_store=store, run_id=meta.id)
    _new_meta, ev = await ctl.submit_decision(
        choice="skip_member", scope="current_round", requested_by="user:1"
    )
    fresh, _ = store.read_run(meta.id)
    assert fresh.last_decision is not None
    assert fresh.last_decision["choice"] == "skip_member"
    assert fresh.last_decision["scope"] == "current_round"
    assert fresh.last_decision["requested_by"] == "user:1"
    assert fresh.last_decision["at"]  # timestamp present
    # The event was also recorded.
    _, events = store.read_run(meta.id)
    rsc = [e for e in events if e.type == EventType.RUN_STATUS_CHANGED]
    assert any(
        e.payload.get("decision", {}).get("choice") == "skip_member" for e in rsc
    )


@pytest.mark.asyncio
async def test_submit_decision_stop_durably_triggers_cancel(seeded_run) -> None:
    store, meta = seeded_run
    ctl = RunControl(run_store=store, run_id=meta.id)
    new_meta, _ev = await ctl.submit_decision(
        choice="stop", scope="remainder_of_run", requested_by="user:1"
    )
    assert new_meta.cancel_requested_at is not None
    fresh, _ = store.read_run(meta.id)
    assert fresh.cancel_requested_at is not None
    assert fresh.last_decision["choice"] == "stop"


@pytest.mark.asyncio
async def test_submit_decision_continue_local_only_records_decision(seeded_run) -> None:
    store, meta = seeded_run
    ctl = RunControl(run_store=store, run_id=meta.id)
    await ctl.submit_decision(
        choice="continue_local_only",
        scope="current_turn",
        requested_by="user:1",
    )
    fresh, _ = store.read_run(meta.id)
    assert fresh.last_decision["choice"] == "continue_local_only"
    assert fresh.last_decision["scope"] == "current_turn"
    # Non-terminal: no cancel triggered.
    assert fresh.cancel_requested_at is None


@pytest.mark.asyncio
async def test_submit_decision_survives_restart_via_meta_round_trip(seeded_run) -> None:
    """The decision lives in meta JSON — read by a freshly-constructed store."""
    store, meta = seeded_run
    ctl = RunControl(run_store=store, run_id=meta.id)
    await ctl.submit_decision(
        choice="skip_member", scope="current_round", requested_by="user:1"
    )
    # Build a new RunStore (simulating sidecar restart) reading the same dir.
    new_store = RunStore(runs_dir=store._runs_dir)  # noqa: SLF001 — test only
    fresh, _ = new_store.read_run(meta.id)
    assert fresh.last_decision["choice"] == "skip_member"
    assert fresh.last_decision["scope"] == "current_round"


def test_decision_route_409s_when_run_not_awaiting(
    tmp_errorta_home, seed_room_full,
) -> None:
    """F031-09 P2 lock: decisions are only valid when the run is in
    ``awaiting_user_decision`` — POSTing outside that state returns 409.
    The proper happy-path is covered by test_ask_decision_e2e.py which
    drives the scheduler into the ask-pause state via stop_behavior=ask.
    """
    client = TestClient(server_mod.app)
    room = seed_room_full(member_count=2, provider="fake", model="stub-model",
                         max_rounds=1)
    r = client.post("/council/runs", json={"room_id": room.id, "prompt": "p",
                                            "corpus_ids": []})
    assert r.status_code in (200, 201), r.text
    run_id = r.json()["run"]["id"]
    # Drain so the run reaches terminal state.
    from errorta_app.routes.council import drain_scheduler_threads
    drain_scheduler_threads(timeout=5.0)
    r2 = client.post(
        f"/council/runs/{run_id}/decision",
        json={"choice": "skip_member", "scope": "current_round"},
        headers={"X-Errorta-Origin": "tauri-ui"},
    )
    # Either 409 (decision_not_applicable) or terminal — both fail-closed.
    # Which one fires depends on a race between the scheduler's terminal
    # write and the decision POST; both are correct fail-closed responses
    # outside ``awaiting_user_decision`` and lock the same review finding.
    assert r2.status_code in (409, 410), r2.text
    detail = r2.json()["detail"]
    assert detail in ("decision_not_applicable", "terminal_run"), detail


@pytest.mark.asyncio
async def test_resume_during_awaiting_user_decision_continues_run(
    tmp_errorta_home, runs_dir_path,
) -> None:
    """F031-09 P2 lock: /resume must work from ``awaiting_user_decision``
    (treated as ``continue_local_only``). Earlier ``request_resume``
    only handled ``paused`` — calls from ``awaiting_user_decision`` were
    silently no-ops.
    """
    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(
        room_id="rm",
        room_snapshot={"topology_kind": "round_robin", "members": []},
        prompt="hi", corpus_ids=[],
    )
    store.merge_meta_fields(meta.id, status="awaiting_user_decision")
    ctl = RunControl(run_store=store, run_id=meta.id)
    new_meta = await ctl.request_resume(requested_by="user:1")
    assert new_meta.status == "running"
    # The state-change event records the transition for audit.
    _, events = store.read_run(meta.id)
    rsc = [
        e for e in events
        if e.type == EventType.RUN_STATUS_CHANGED
        and e.payload.get("from_status") == "awaiting_user_decision"
    ]
    assert rsc, "expected RUN_STATUS_CHANGED event recording the resume"
