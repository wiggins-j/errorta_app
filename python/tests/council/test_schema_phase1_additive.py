"""Invariant 11: Phase 1 additions are field-additive and version-stable."""
from __future__ import annotations

import pytest

from errorta_council.schema import (
    EventStatus,
    EventType,
    FORMAT_VERSION,
    NON_TERMINAL_RUN_STATUSES,
    RunMeta,
)


def test_format_version_unchanged() -> None:
    assert FORMAT_VERSION == 1  # Phase 1 must NOT bump


def test_runmeta_phase1_fields_default_empty() -> None:
    m = RunMeta(
        format_version=1,
        id="r1",
        room_id="rm",
        room_snapshot={},
        prompt="hi",
        corpus_ids=[],
        status="created",
        created_at="2026-06-11T00:00:00Z",
        started_at=None,
        updated_at="2026-06-11T00:00:00Z",
        finished_at=None,
        last_sequence=0,
        event_count=0,
        terminal_event_id=None,
        resume_policy="mark_interrupted",
        costs={},
        capabilities={},
    )
    assert m.completed_messages_by_member == {}
    assert m.total_messages_completed == 0
    assert m.paused_at is None
    assert m.cancel_requested_at is None
    assert m.terminal_reason is None


def test_runmeta_phase0_json_loads_into_phase1_meta() -> None:
    """A Phase 0 RunMeta JSON dict (no new fields) must still load cleanly."""
    raw = {
        "format_version": 1,
        "id": "r1",
        "room_id": "rm",
        "room_snapshot": {},
        "prompt": "hi",
        "corpus_ids": [],
        "status": "created",
        "created_at": "2026-06-11T00:00:00Z",
        "started_at": None,
        "updated_at": "2026-06-11T00:00:00Z",
        "finished_at": None,
        "last_sequence": 0,
        "event_count": 0,
        "terminal_event_id": None,
        "resume_policy": "mark_interrupted",
        "costs": {},
        "capabilities": {},
    }
    m = RunMeta.from_dict(raw)
    assert m.id == "r1"
    assert m.completed_messages_by_member == {}
    # Round-trips additively.
    again = m.to_dict()
    assert again["format_version"] == 1
    assert "completed_messages_by_member" in again


def test_new_event_types_present() -> None:
    assert EventType.LOCAL_RESOURCE_CHECK_STARTED.value == "local_resource_check_started"
    assert EventType.LOCAL_RESOURCE_RELEASED.value == "local_resource_released"


@pytest.mark.parametrize("value", ["paused", "awaiting_user_decision", "resumed"])
def test_new_event_statuses_round_trip(value: str) -> None:
    """Fix 3: EventStatus vocabulary extended with paused/awaiting_user_decision/resumed."""
    es = EventStatus(value)
    assert es.value == value


@pytest.mark.parametrize("status", ["paused", "awaiting_user_decision"])
def test_runmeta_status_vocabulary_extended(status: str) -> None:
    """Fix 3: RunMeta.status accepts the new non-terminal statuses."""
    assert status in NON_TERMINAL_RUN_STATUSES
    m = RunMeta(
        format_version=1,
        id="r1", room_id="rm", room_snapshot={}, prompt="hi", corpus_ids=[],
        status=status,
        created_at="2026-06-11T00:00:00Z",
        started_at=None,
        updated_at="2026-06-11T00:00:00Z",
        finished_at=None,
        last_sequence=0, event_count=0, terminal_event_id=None,
        resume_policy="mark_interrupted", costs={}, capabilities={},
    )
    raw = m.to_dict()
    again = RunMeta.from_dict(raw)
    assert again.status == status


def test_runstore_write_meta_round_trip(tmp_errorta_home, runs_dir_path) -> None:
    """RunStore.write_meta atomically overwrites the meta JSON without an event."""
    from dataclasses import replace
    from errorta_council.run_store import RunStore
    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(
        room_id="rm", room_snapshot={}, prompt="hi", corpus_ids=[]
    )
    new = replace(meta, paused_at="2026-06-11T00:00:00Z", status="paused")
    store.write_meta(new)
    fresh, _ = store.read_run(meta.id)
    assert fresh.paused_at == "2026-06-11T00:00:00Z"
    assert fresh.status == "paused"
