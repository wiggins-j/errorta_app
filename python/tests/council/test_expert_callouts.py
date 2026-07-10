"""F037 Slices 3-4 — manual expert callout runtime + approval + routes.

Runtime tests drive the scheduler in-process via ``build_and_run`` with the
default ``LocalGateway`` (which dispatches ``fake.*`` routes offline), with the
callout pre-seeded in the queue so the first drain catches it deterministically.

Route tests cover the synchronous shape gates (origin, terminal, disabled,
unknown target, approval state machine).
"""
from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from errorta_app.server import app
from errorta_council import paths as council_paths
from errorta_council.callouts.queue import CalloutQueue, CalloutRecord
from errorta_council.gateway_local import LocalGateway
from errorta_council.limits import SchedulerPolicy
from errorta_council.run_store import RunStore
from errorta_council.schema import (
    EscalationPolicy,
    EscalationRosterEntry,
    EventType,
)

_UI = {"x-errorta-origin": "tauri-ui"}


def _runs() -> RunStore:
    return RunStore(runs_dir=council_paths.runs_dir())


def _enable_escalation(room, *, approval_mode: str = "auto", target=None):
    """Add an expert roster + enabled policy + budget headroom."""
    target = target or EscalationRosterEntry(
        id="deep-reviewer",
        name="Deep Reviewer",
        gateway_route_id="fake.local.stub-model",
        provider_kind="local",
        context_access="prompt_only",
        transcript_access="own_messages",
        system_prompt="Resolve the disagreement.",
        callout={"advisory": True},
    )
    return replace(
        room,
        escalation_policy=EscalationPolicy(
            enabled=True, approval_mode=approval_mode, max_callouts_per_run=2,
        ),
        escalation_roster=[target],
        budget_policy=replace(room.budget_policy, max_total_model_calls=None),
    )


def _seed_run(seed_room_full, *, approval_mode="auto", max_rounds=3, target=None):
    room = _enable_escalation(
        seed_room_full(room_id="rm-callout", member_count=2, provider="fake",
                       model="stub-model", max_rounds=max_rounds,
                       max_messages_per_member=max_rounds),
        approval_mode=approval_mode, target=target,
    )
    # The scheduler + routes read escalation config from the run's
    # room_snapshot, so it is enough to seed the snapshot — no RoomStore.
    # Build it the way the real route does so the resource guard recognizes
    # fake members (provider hint) and escalation config round-trips.
    from errorta_app.routes.council import _room_dict_with_provider_hint
    runs = _runs()
    meta = runs.create_run(
        room_id=room.id, room_snapshot=_room_dict_with_provider_hint(room),
        prompt="hi", corpus_ids=[],
    )
    return runs, meta, room


def _policy(max_rounds=3):
    return SchedulerPolicy(
        max_rounds=max_rounds, max_messages_per_member=max_rounds,
        max_total_member_messages=None, per_turn_timeout_seconds=30,
    )


def _types(events):
    return [e.type for e in events]


# --- runtime --------------------------------------------------------------

def test_auto_callout_executes_and_completes(tmp_errorta_home, seed_room_full):
    runs, meta, _ = _seed_run(seed_room_full, approval_mode="auto")
    CalloutQueue(runs_dir=runs.runs_dir, run_id=meta.id).enqueue(CalloutRecord(
        callout_id="co_test", target_id="deep-reviewer",
        reason_code="user_requested", question="please review",
        requested_by={"type": "user"}, state="requested",
    ))
    from errorta_council.engine import build_and_run
    asyncio.run(build_and_run(
        run_store=runs, run_meta=meta, policy=_policy(),
        gateway_meta=LocalGateway(), hardware_scan_present=False,
    ))
    _, events = runs.read_run(meta.id)
    types = _types(events)
    assert EventType.CALLOUT_REQUESTED in types
    assert EventType.CALLOUT_APPROVED in types
    assert EventType.CALLOUT_STARTED in types
    assert EventType.CALLOUT_COMPLETED in types
    # the expert answer is a normal member_message carrying callout_id
    callout_msgs = [
        e for e in events
        if e.type == EventType.MEMBER_MESSAGE and (e.payload or {}).get("is_callout")
    ]
    assert len(callout_msgs) == 1
    assert callout_msgs[0].payload["callout_id"] == "co_test"
    assert callout_msgs[0].payload["target_id"] == "deep-reviewer"
    # the run still terminates normally
    final, _ = runs.read_run(meta.id)
    assert final.status == "completed"
    # queue record reaches completed
    rec = CalloutQueue(runs_dir=runs.runs_dir, run_id=meta.id).get("co_test")
    assert rec.state == "completed"


def test_approval_required_then_approved_completes(tmp_errorta_home, seed_room_full):
    runs, meta, _ = _seed_run(seed_room_full, approval_mode="ask_user")
    # Pre-set the approval decision so the scheduler's await resolves at once.
    CalloutQueue(runs_dir=runs.runs_dir, run_id=meta.id).enqueue(CalloutRecord(
        callout_id="co_appr", target_id="deep-reviewer",
        reason_code="user_requested", question="q",
        requested_by={"type": "user"}, state="requested", approval="approved",
    ))
    from errorta_council.engine import build_and_run
    asyncio.run(build_and_run(
        run_store=runs, run_meta=meta, policy=_policy(),
        gateway_meta=LocalGateway(), hardware_scan_present=False,
    ))
    _, events = runs.read_run(meta.id)
    types = _types(events)
    assert EventType.CALLOUT_APPROVAL_REQUIRED in types
    assert EventType.CALLOUT_APPROVED in types
    assert EventType.CALLOUT_COMPLETED in types


def test_approval_rejected_emits_rejected_and_run_continues(tmp_errorta_home, seed_room_full):
    runs, meta, _ = _seed_run(seed_room_full, approval_mode="ask_user")
    CalloutQueue(runs_dir=runs.runs_dir, run_id=meta.id).enqueue(CalloutRecord(
        callout_id="co_rej", target_id="deep-reviewer",
        reason_code="user_requested", question="q",
        requested_by={"type": "user"}, state="requested", approval="rejected",
    ))
    from errorta_council.engine import build_and_run
    asyncio.run(build_and_run(
        run_store=runs, run_meta=meta, policy=_policy(),
        gateway_meta=LocalGateway(), hardware_scan_present=False,
    ))
    _, events = runs.read_run(meta.id)
    types = _types(events)
    assert EventType.CALLOUT_REJECTED in types
    assert EventType.CALLOUT_COMPLETED not in types
    # on_callout_rejected defaults to "continue" → run still finishes
    final, _ = runs.read_run(meta.id)
    assert final.status == "completed"
    # no expert member_message was emitted
    assert not [
        e for e in events
        if e.type == EventType.MEMBER_MESSAGE and (e.payload or {}).get("is_callout")
    ]


def test_callout_does_not_consume_member_turn_budget(tmp_errorta_home, seed_room_full):
    # Tight budget: 1 round x 2 members = exactly 2 ordinary turns. A callout
    # answer is a MEMBER_MESSAGE; if it counted toward the deliberation cap it
    # would steal a slot and end the run after a single ordinary turn.
    runs, meta, _ = _seed_run(seed_room_full, approval_mode="auto", max_rounds=1)
    CalloutQueue(runs_dir=runs.runs_dir, run_id=meta.id).enqueue(CalloutRecord(
        callout_id="co_budget", target_id="deep-reviewer",
        reason_code="user_requested", question="q",
        requested_by={"type": "user"}, state="requested",
    ))
    from errorta_council.engine import build_and_run
    asyncio.run(build_and_run(
        run_store=runs, run_meta=meta, policy=_policy(max_rounds=1),
        gateway_meta=LocalGateway(), hardware_scan_present=False,
    ))
    final, events = runs.read_run(meta.id)
    ordinary = [
        e for e in events
        if e.type == EventType.MEMBER_MESSAGE and not (e.payload or {}).get("is_callout")
    ]
    # Both configured members must still get their turn.
    assert {e.member_id for e in ordinary} == {"m-1", "m-2"}
    # And the callout still executed.
    assert EventType.CALLOUT_COMPLETED in _types(events)
    # The deliberation counter excludes the callout turn.
    assert final.total_messages_completed == 2


def test_remote_target_forces_approval_even_in_auto_mode(tmp_errorta_home, seed_room_full):
    # A remote target (non-fake route) must require approval for the first
    # remote callout even under approval_mode=auto. Also exercises the
    # fail-safe route-kind detection (provider_kind left default 'unknown').
    remote_target = EscalationRosterEntry(
        id="opus-expert", name="Opus", gateway_route_id="anthropic.claude-opus",
        context_access="prompt_only", transcript_access="own_messages",
    )
    runs, meta, _ = _seed_run(seed_room_full, approval_mode="auto", target=remote_target)
    # Pre-reject so the run never attempts a (network) remote gateway call.
    CalloutQueue(runs_dir=runs.runs_dir, run_id=meta.id).enqueue(CalloutRecord(
        callout_id="co_remote", target_id="opus-expert",
        reason_code="user_requested", question="q",
        requested_by={"type": "user"}, state="requested", approval="rejected",
    ))
    from errorta_council.engine import build_and_run
    asyncio.run(build_and_run(
        run_store=runs, run_meta=meta, policy=_policy(),
        gateway_meta=LocalGateway(), hardware_scan_present=False,
    ))
    _, events = runs.read_run(meta.id)
    types = _types(events)
    assert EventType.CALLOUT_APPROVAL_REQUIRED in types  # auto did NOT auto-admit
    assert EventType.CALLOUT_REJECTED in types
    # never reached a (failed) remote gateway call
    assert EventType.CALLOUT_STARTED not in types


# --- routes ---------------------------------------------------------------

def test_route_requires_ui_origin(tmp_errorta_home, seed_room_full):
    runs, meta, _ = _seed_run(seed_room_full)
    client = TestClient(app)
    r = client.post(f"/council/runs/{meta.id}/callouts",
                    json={"target_id": "deep-reviewer"})
    assert r.status_code == 403


def test_route_unknown_target_404(tmp_errorta_home, seed_room_full):
    runs, meta, _ = _seed_run(seed_room_full)
    client = TestClient(app)
    r = client.post(f"/council/runs/{meta.id}/callouts",
                    json={"target_id": "ghost"}, headers=_UI)
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "unknown_callout_target"


def test_route_disabled_escalation_422(tmp_errorta_home, seed_room_full):
    room = seed_room_full(room_id="rm-off", member_count=2, provider="fake",
                          model="stub-model", max_rounds=1)
    runs = _runs()
    meta = runs.create_run(room_id=room.id, room_snapshot=room.to_dict(),
                           prompt="hi", corpus_ids=[])
    client = TestClient(app)
    r = client.post(f"/council/runs/{meta.id}/callouts",
                    json={"target_id": "deep-reviewer"}, headers=_UI)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "escalation_disabled"


def test_route_enqueues_and_lists(tmp_errorta_home, seed_room_full):
    runs, meta, _ = _seed_run(seed_room_full)
    client = TestClient(app)
    r = client.post(f"/council/runs/{meta.id}/callouts",
                    json={"target_id": "deep-reviewer", "question": "q"},
                    headers=_UI)
    assert r.status_code == 200
    callout_id = r.json()["callout_id"]
    listed = client.get(f"/council/runs/{meta.id}/callouts").json()["callouts"]
    assert any(c["callout_id"] == callout_id for c in listed)


def test_route_approve_requires_awaiting_state_409(tmp_errorta_home, seed_room_full):
    runs, meta, _ = _seed_run(seed_room_full)
    CalloutQueue(runs_dir=runs.runs_dir, run_id=meta.id).enqueue(CalloutRecord(
        callout_id="co_x", target_id="deep-reviewer", reason_code="user_requested",
        question="q", requested_by={"type": "user"}, state="requested",
    ))
    client = TestClient(app)
    r = client.post(f"/council/runs/{meta.id}/callouts/co_x/approve", headers=_UI)
    assert r.status_code == 409
