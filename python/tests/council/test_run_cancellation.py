"""F031-02 cancellation semantics + deterministic fake run.

Architecture-spec OQ#2 resolution: cancelling a terminal run returns
**409 Conflict** at the route layer. At the store layer, append-after-
terminal raises ``TerminalRunRejected``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from errorta_council import paths as council_paths
from errorta_council.fake_run import run_fake_council
from errorta_council.run_store import RunStore, TerminalRunRejected
from errorta_council.schema import EventStatus, EventType


def _store() -> RunStore:
    return RunStore(runs_dir=council_paths.runs_dir())


def test_fake_run_emits_ordered_transcript(tmp_errorta_home: Path) -> None:
    store = _store()
    meta = store.create_run(
        run_id="run-fake", room_id="room-1",
        room_snapshot={"name": "x", "topology_kind": "round_robin",
                       "member_count": 2, "room_format_version": 1},
        prompt="hello", corpus_ids=[],
    )
    run_fake_council(store, meta.id, member_ids=["m-1", "m-2"])
    _, events = store.read_run(meta.id)
    types = [e.type for e in events]
    assert types == [
        EventType.RUN_STARTED,
        EventType.MEMBER_MESSAGE,
        EventType.MEMBER_MESSAGE,
        EventType.RUN_COMPLETED,
    ]
    assert [e.sequence for e in events] == [1, 2, 3, 4]


def test_fake_run_failure_variant_writes_run_failed(tmp_errorta_home: Path) -> None:
    store = _store()
    meta = store.create_run(
        run_id="run-fakefail", room_id="room-1",
        room_snapshot={"name": "x", "topology_kind": "round_robin",
                       "member_count": 2, "room_format_version": 1},
        prompt="hello", corpus_ids=[],
    )
    run_fake_council(store, meta.id, member_ids=["m-1", "m-2"], fail=True)
    _, events = store.read_run(meta.id)
    assert events[-1].type == EventType.RUN_FAILED
    assert events[-1].status == EventStatus.FAILED


def _start_run(store: RunStore, run_id: str):
    meta = store.create_run(
        run_id=run_id, room_id="room-1",
        room_snapshot={}, prompt="p", corpus_ids=[],
    )
    token = store.acquire_writer(meta.id)
    try:
        store.append_event(meta.id, type=EventType.RUN_STARTED,
                           status=EventStatus.RUNNING, payload={}, writer=token)
    finally:
        store.release_writer(token)
    return meta


def test_cancel_writes_request_event_first(tmp_errorta_home: Path) -> None:
    store = _store()
    meta = _start_run(store, "run-cancel")
    new_meta, ev = store.cancel_run(meta.id, requested_by="user", reason="ui")
    assert ev.type == EventType.RUN_CANCEL_REQUESTED
    assert ev.status == EventStatus.CANCEL_REQUESTED
    # Run is not terminal yet — terminal arrives when cleanup writes run_cancelled.
    assert new_meta.status == "running"


def test_double_cancel_is_idempotent_at_store(tmp_errorta_home: Path) -> None:
    store = _store()
    meta = _start_run(store, "run-twice")
    store.cancel_run(meta.id, requested_by="user", reason="ui")
    # A second cancel appends another request event; idempotency is a
    # route-level concern (see test_council_routes.py).
    store.cancel_run(meta.id, requested_by="user", reason="ui")
    _, events = store.read_run(meta.id)
    cancels = [e for e in events if e.type == EventType.RUN_CANCEL_REQUESTED]
    assert len(cancels) == 2


def test_cancel_terminal_run_raises_terminal_rejected(
    tmp_errorta_home: Path,
) -> None:
    """Spec OQ#2 (store layer): append after terminal is hard-rejected."""
    store = _store()
    meta = store.create_run(
        run_id="run-done", room_id="room-1",
        room_snapshot={}, prompt="p", corpus_ids=[],
    )
    token = store.acquire_writer(meta.id)
    try:
        store.append_event(meta.id, type=EventType.RUN_STARTED,
                           status=EventStatus.RUNNING, payload={}, writer=token)
        store.append_event(meta.id, type=EventType.RUN_COMPLETED,
                           status=EventStatus.COMPLETED,
                           payload={"terminal_reason": "topology_exhausted"},
                           writer=token)
    finally:
        store.release_writer(token)
    with pytest.raises(TerminalRunRejected):
        store.cancel_run(meta.id, requested_by="user", reason="ui")
