"""INTERNAL CONTRACT TEST ONLY. Council event envelope may evolve; this
file is the canary that locks the current internal shape.

Marker: ``internal_contract`` (already registered in
``python/pyproject.toml`` under F-INFRA-03; same disclaimer as the
verdict-contract canary).

Locks:
- envelope required fields (invariant 11 — additive evolution);
- ``format_version == 1``;
- sequence assignment rules (invariant 2);
- base ``type`` and ``status`` vocabulary;
- sanitized error shape (invariant 12).

If any of these change in a non-additive way, this canary fails and
the change must be planned through a migration spec, not a silent
schema bump.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from errorta_council import paths as council_paths
from errorta_council.fake_run import run_fake_council
from errorta_council.run_store import RunStore
from errorta_council.schema import (
    FORMAT_VERSION,
    CouncilEventError,
    EventStatus,
    EventType,
)

pytestmark = pytest.mark.internal_contract


REQUIRED_ENVELOPE_FIELDS = {
    "format_version", "id", "run_id", "sequence",
    "type", "status", "created_at", "payload",
}


# Locked base type vocabulary (Phase 0). Reserved future types are
# declared in EventType but explicitly not in this lock; emission is
# the lock, not enum membership.
LOCKED_BASE_TYPES = {
    "run_started", "run_status_changed",
    "member_queued",
    "context_build_started", "context_built",
    "budget_check_started", "budget_blocked",
    "member_call_started", "member_message", "member_skipped",
    "member_failed", "member_cancelled",
    "finalization_started", "final_answer",
    "verdict_recorded", "grounding_recorded",
    "run_cancel_requested", "run_cancelled",
    "run_failed", "run_completed",
    "diagnostic_note",
}

LOCKED_STATUSES = {
    "pending", "running", "completed", "skipped", "blocked",
    "failed", "cancel_requested", "cancelled", "interrupted",
    # Phase 1 additions (F031-1a Fix 3 vocabulary):
    "paused", "resumed", "awaiting_user_decision",
}


def test_locked_base_types_match_enum() -> None:
    enum_values = {e.value for e in EventType}
    # Locked subset is a strict subset of the enum (enum may declare
    # reserved future types).
    assert LOCKED_BASE_TYPES.issubset(enum_values)


def test_locked_statuses_match_enum() -> None:
    assert LOCKED_STATUSES == {e.value for e in EventStatus}


def test_format_version_locked_at_one() -> None:
    assert FORMAT_VERSION == 1


def test_fake_run_envelope_locks(tmp_errorta_home: Path) -> None:
    store = RunStore(runs_dir=council_paths.runs_dir())
    meta = store.create_run(
        run_id="canary-run", room_id="canary-room",
        room_snapshot={"name": "C", "topology_kind": "round_robin",
                       "member_count": 1, "room_format_version": 1},
        prompt="p", corpus_ids=[],
    )
    run_fake_council(store, meta.id, member_ids=["m-1"])
    log = council_paths.runs_dir() / f"{meta.id}.jsonl"
    for line in log.read_text().splitlines():
        if not line.strip():
            continue
        raw = json.loads(line)
        missing = REQUIRED_ENVELOPE_FIELDS - raw.keys()
        assert not missing, f"envelope missing required fields: {missing}"
        assert raw["format_version"] == 1
        assert raw["type"] in LOCKED_BASE_TYPES
        assert raw["status"] in LOCKED_STATUSES


def test_sequence_starts_at_one_and_increments(tmp_errorta_home: Path) -> None:
    store = RunStore(runs_dir=council_paths.runs_dir())
    meta = store.create_run(
        run_id="canary-seq", room_id="r",
        room_snapshot={}, prompt="p", corpus_ids=[],
    )
    run_fake_council(store, meta.id, member_ids=["m-1", "m-2"])
    log = council_paths.runs_dir() / f"{meta.id}.jsonl"
    seqs = [json.loads(line)["sequence"]
            for line in log.read_text().splitlines() if line.strip()]
    assert seqs[0] == 1
    assert seqs == list(range(1, len(seqs) + 1))


# ---- Phase 1 additions ------------------------------------------------------


@pytest.mark.internal_contract
def test_phase1_emits_local_resource_check_event_types() -> None:
    """Invariant 11: Phase 1 EXTENDS the emitted-event vocabulary additively."""
    from errorta_council.schema import EventType
    assert "local_resource_check_started" in {e.value for e in EventType}
    assert "local_resource_released" in {e.value for e in EventType}


@pytest.mark.internal_contract
def test_phase1_event_status_includes_paused_resumed_awaiting() -> None:
    """Fix 3: EventStatus vocabulary extended with paused/resumed/awaiting_user_decision."""
    from errorta_council.schema import EventStatus
    statuses = {s.value for s in EventStatus}
    assert "paused" in statuses
    assert "resumed" in statuses
    assert "awaiting_user_decision" in statuses


@pytest.mark.internal_contract
def test_phase1_runmeta_non_terminal_statuses_extended() -> None:
    """Fix 3: RunMeta status vocabulary accepts paused / awaiting_user_decision."""
    from errorta_council.schema import NON_TERMINAL_RUN_STATUSES
    assert "paused" in NON_TERMINAL_RUN_STATUSES
    assert "awaiting_user_decision" in NON_TERMINAL_RUN_STATUSES


@pytest.mark.internal_contract
def test_phase1_run_status_changed_payload_carries_decision() -> None:
    """RUN_STATUS_CHANGED payload from a decision must carry the choice/scope shape."""
    from errorta_council.schema import CouncilEvent, EventStatus, EventType
    sample = {"decision": {"choice": "skip_member", "scope": "current_round"},
              "requested_by": "user"}
    ev = CouncilEvent(
        format_version=1, id="e1", run_id="r1", sequence=1,
        type=EventType.RUN_STATUS_CHANGED, status=EventStatus.RUNNING,
        created_at="2026-06-11T00:00:00Z", payload=sample,
    )
    assert ev.payload["decision"]["choice"] == "skip_member"
    assert ev.payload["decision"]["scope"] == "current_round"


@pytest.mark.internal_contract
def test_phase1_run_status_changed_paused_status_round_trips() -> None:
    """Fix 3: RUN_STATUS_CHANGED + EventStatus.PAUSED round-trips through to_dict/from_dict."""
    from errorta_council.schema import CouncilEvent, EventStatus, EventType
    ev = CouncilEvent(
        format_version=1, id="e2", run_id="r1", sequence=2,
        type=EventType.RUN_STATUS_CHANGED, status=EventStatus.PAUSED,
        created_at="2026-06-11T00:00:00Z",
        payload={"status_change": "paused", "requested_by": "user"},
    )
    raw = ev.to_dict()
    back = CouncilEvent.from_dict(raw)
    assert back.status is EventStatus.PAUSED


@pytest.mark.internal_contract
def test_phase1_terminal_reason_codes_are_stable() -> None:
    """Stable reason-code vocabulary surfaced in terminal payloads."""
    from errorta_council.limits import ReasonCode
    must_exist = {
        "limits_exhausted", "no_eligible_members", "member_message_cap_reached",
        "local_timeout", "local_provider_unavailable", "local_model_missing",
        "gateway_error", "cancel_requested", "per_turn_timeout",
        "cap_invariant_violated", "origin_not_authorized",
    }
    actual = {r.value for r in ReasonCode}
    missing = must_exist - actual
    assert not missing, f"missing stable reason codes: {missing}"


def test_error_payload_has_sanitized_shape() -> None:
    err = CouncilEventError(code="provider_timeout",
                            message="Provider timed out.",
                            retryable=True,
                            details={"phase": "call"})
    raw = err.__dict__
    # Permit _extras pass-through field (invariant 11 additive evolution).
    raw_no_extras = {k: v for k, v in raw.items() if k != "_extras"}
    assert set(raw_no_extras.keys()) == {"code", "message", "retryable", "details"}
    assert isinstance(raw_no_extras["details"], dict)
