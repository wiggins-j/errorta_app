"""F031-02 — append-only run store with central sequence assignment.

Invariant 2: one writer per run; sequence assigned centrally; events are
append-only and ``format_version``-stamped. Invariant 11: unknown fields
tolerated; unsupported format_versions rejected.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from errorta_council import paths as council_paths
from errorta_council.run_store import (
    RunNotFound,
    RunStore,
    TerminalRunRejected,
)
from errorta_council.schema import (
    FORMAT_VERSION,
    EventStatus,
    EventType,
)


def _store() -> RunStore:
    return RunStore(runs_dir=council_paths.runs_dir())


def _create_run(store: RunStore, run_id: str = "run-1") -> str:
    meta = store.create_run(
        run_id=run_id, room_id="room-1",
        room_snapshot={"name": "Phase 0 Room", "topology_kind": "round_robin",
                       "member_count": 1, "room_format_version": 1},
        prompt="hello", corpus_ids=[],
    )
    return meta.id


def _seed_open_run(store: RunStore, run_id: str = "run-1"):
    """Phase 1: create a run and acquire its writer token."""
    rid = _create_run(store, run_id)
    return rid, store.acquire_writer(rid)


def test_create_run_writes_meta_only(tmp_errorta_home: Path) -> None:
    store = _store()
    rid = _create_run(store)
    assert (council_paths.runs_dir() / f"{rid}.meta.json").is_file()
    # No event log yet.
    assert not (council_paths.runs_dir() / f"{rid}.jsonl").exists()


def test_append_event_assigns_sequence_one(tmp_errorta_home: Path) -> None:
    store = _store()
    rid, token = _seed_open_run(store)
    ev = store.append_event(
        rid, type=EventType.RUN_STARTED, status=EventStatus.RUNNING,
        payload={"room_id": "room-1"}, writer=token,
    )
    assert ev.sequence == 1
    assert ev.format_version == FORMAT_VERSION


def test_sequence_increments_strictly(tmp_errorta_home: Path) -> None:
    store = _store()
    rid, token = _seed_open_run(store)
    seqs = [
        store.append_event(rid, type=EventType.RUN_STARTED, status=EventStatus.RUNNING,
                           payload={}, writer=token).sequence,
        store.append_event(rid, type=EventType.MEMBER_QUEUED, status=EventStatus.PENDING,
                           payload={"reason": "round_robin"}, writer=token).sequence,
        store.append_event(rid, type=EventType.MEMBER_MESSAGE, status=EventStatus.COMPLETED,
                           payload={"content": "ok"}, writer=token).sequence,
    ]
    assert seqs == [1, 2, 3]


def test_read_run_returns_ordered_events(tmp_errorta_home: Path) -> None:
    store = _store()
    rid, token = _seed_open_run(store)
    for et in (EventType.RUN_STARTED, EventType.MEMBER_QUEUED, EventType.MEMBER_MESSAGE):
        store.append_event(rid, type=et, status=EventStatus.RUNNING, payload={}, writer=token)
    meta, events = store.read_run(rid)
    assert [e.sequence for e in events] == [1, 2, 3]
    assert meta.last_sequence == 3
    assert meta.event_count == 3


def test_terminal_event_rejects_further_events(tmp_errorta_home: Path) -> None:
    store = _store()
    rid, token = _seed_open_run(store)
    store.append_event(rid, type=EventType.RUN_STARTED, status=EventStatus.RUNNING, payload={}, writer=token)
    store.append_event(
        rid, type=EventType.RUN_COMPLETED, status=EventStatus.COMPLETED,
        payload={"terminal_reason": "topology_exhausted"}, writer=token,
    )
    with pytest.raises(TerminalRunRejected):
        store.append_event(
            rid, type=EventType.MEMBER_MESSAGE, status=EventStatus.COMPLETED, payload={}, writer=token,
        )


def test_unknown_run_raises(tmp_errorta_home: Path) -> None:
    with pytest.raises(RunNotFound):
        _store().read_run("nope")


def test_list_runs_returns_summary(tmp_errorta_home: Path) -> None:
    store = _store()
    _create_run(store, "run-a")
    _create_run(store, "run-b")
    summaries = store.list_runs()
    ids = sorted(s.id for s in summaries)
    assert ids == ["run-a", "run-b"]


def test_two_reloads_byte_identical(tmp_errorta_home: Path) -> None:
    """Exit-gate test: re-serializing a run twice is byte-identical."""
    store = _store()
    rid, token = _seed_open_run(store)
    store.append_event(rid, type=EventType.RUN_STARTED, status=EventStatus.RUNNING, payload={}, writer=token)
    store.append_event(
        rid, type=EventType.MEMBER_MESSAGE, status=EventStatus.COMPLETED,
        payload={"content": "hello"}, writer=token,
    )
    store.append_event(
        rid, type=EventType.RUN_COMPLETED, status=EventStatus.COMPLETED,
        payload={"terminal_reason": "topology_exhausted"}, writer=token,
    )
    log = (council_paths.runs_dir() / f"{rid}.jsonl").read_bytes()
    meta = (council_paths.runs_dir() / f"{rid}.meta.json").read_bytes()
    # Read once …
    _ = store.read_run(rid)
    log2 = (council_paths.runs_dir() / f"{rid}.jsonl").read_bytes()
    meta2 = (council_paths.runs_dir() / f"{rid}.meta.json").read_bytes()
    # … read again …
    _ = store.read_run(rid)
    log3 = (council_paths.runs_dir() / f"{rid}.jsonl").read_bytes()
    meta3 = (council_paths.runs_dir() / f"{rid}.meta.json").read_bytes()
    assert log == log2 == log3
    assert meta == meta2 == meta3
