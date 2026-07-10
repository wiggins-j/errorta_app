"""F031-02 recovery semantics (invariant 4 — fail closed).

Crash-recovery tests pass when an interrupted run is marked
``interrupted`` and never silently resumed; a mid-file corrupt log is
marked ``corrupted`` and never reconstructed as completed.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from errorta_council import paths as council_paths
from errorta_council.recovery import (
    CorruptedRun,
    recover_run,
    scan_and_recover,
)
from errorta_council.run_store import RunStore
from errorta_council.schema import EventStatus, EventType


def _store() -> RunStore:
    return RunStore(runs_dir=council_paths.runs_dir())


def _seed_running_run(rid: str = "run-1") -> RunStore:
    store = _store()
    store.create_run(
        run_id=rid, room_id="room-1",
        room_snapshot={"name": "x", "topology_kind": "round_robin",
                       "member_count": 1, "room_format_version": 1},
        prompt="p", corpus_ids=[],
    )
    token = store.acquire_writer(rid)
    try:
        store.append_event(rid, type=EventType.RUN_STARTED,
                           status=EventStatus.RUNNING, payload={}, writer=token)
    finally:
        store.release_writer(token)
    return store


def test_missing_meta_rebuilds_from_log(tmp_errorta_home: Path) -> None:
    _seed_running_run("run-a")
    (council_paths.runs_dir() / "run-a.meta.json").unlink()
    meta = recover_run("run-a", runs_dir=council_paths.runs_dir())
    assert meta.id == "run-a"
    assert meta.last_sequence == 1
    assert (council_paths.runs_dir() / "run-a.meta.json").is_file()


def test_trailing_truncated_line_marks_needs_repair(tmp_errorta_home: Path) -> None:
    _seed_running_run("run-b")
    log = council_paths.runs_dir() / "run-b.jsonl"
    # Simulate a torn last write — append a partial JSON line.
    with open(log, "a", encoding="utf-8") as fh:
        fh.write('{"format_version":1,"id":"ev-x","run_id":"run-b","sequen')
    meta = recover_run("run-b", runs_dir=council_paths.runs_dir())
    assert meta.status in {"running", "interrupted"}
    assert meta.resume_policy in {"mark_interrupted", "needs_repair"}
    # The truncated line was ignored — last_sequence still reflects the
    # one good event.
    assert meta.last_sequence == 1


def test_mid_file_invalid_json_marks_corrupted(tmp_errorta_home: Path) -> None:
    _seed_running_run("run-c")
    log = council_paths.runs_dir() / "run-c.jsonl"
    # Append garbage in the middle, then a valid line after.
    with open(log, "a", encoding="utf-8") as fh:
        fh.write("not valid json\n")
        fh.write(json.dumps({
            "format_version": 1, "id": "ev-y", "run_id": "run-c",
            "sequence": 2, "type": "run_completed", "status": "completed",
            "created_at": "2026-06-11T00:00:01Z", "payload": {},
        }) + "\n")
    with pytest.raises(CorruptedRun) as exc:
        recover_run("run-c", runs_dir=council_paths.runs_dir())
    assert "run-c" in str(exc.value)


def test_meta_without_log_is_corrupted(tmp_errorta_home: Path) -> None:
    _store().create_run(
        run_id="run-d", room_id="room-1",
        room_snapshot={"name": "x", "topology_kind": "round_robin",
                       "member_count": 1, "room_format_version": 1},
        prompt="p", corpus_ids=[],
    )
    # Mark meta as "running" but log never appeared.
    meta_path = council_paths.runs_dir() / "run-d.meta.json"
    raw = json.loads(meta_path.read_text())
    raw["status"] = "running"
    raw["last_sequence"] = 5
    meta_path.write_text(json.dumps(raw, indent=2))
    with pytest.raises(CorruptedRun):
        recover_run("run-d", runs_dir=council_paths.runs_dir())


def test_scan_marks_running_runs_interrupted(tmp_errorta_home: Path) -> None:
    _seed_running_run("run-e")
    report = scan_and_recover(runs_dir=council_paths.runs_dir())
    assert "run-e" in report.interrupted
    # Meta updated on disk.
    raw = json.loads((council_paths.runs_dir() / "run-e.meta.json").read_text())
    assert raw["status"] == "interrupted"


def test_cancel_requested_without_terminal_becomes_interrupted(
    tmp_errorta_home: Path,
) -> None:
    store = _seed_running_run("run-f")
    store.cancel_run("run-f", requested_by="user", reason="ui_stop_button")
    # Crash before terminal run_cancelled.
    report = scan_and_recover(runs_dir=council_paths.runs_dir())
    assert "run-f" in report.interrupted
