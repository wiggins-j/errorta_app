from __future__ import annotations

import pytest

from errorta_council.recovery import scan_and_recover, validate_decision_event
from errorta_council.run_store import RunStore
from errorta_council.schema import EventStatus, EventType


def test_mid_flight_cancel_emits_missing_terminal(tmp_errorta_home, runs_dir_path) -> None:
    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(
        room_id="rm", room_snapshot={"id": "rm", "members": []}, prompt="hi", corpus_ids=[]
    )
    # Simulate: a cancel-requested event was written, but the process died before
    # the terminal RUN_CANCELLED could be appended.
    token = store.acquire_writer(meta.id)
    try:
        store.append_event(
            meta.id,
            type=EventType.RUN_CANCEL_REQUESTED,
            status=EventStatus.CANCEL_REQUESTED,
            payload={"requested_by": "user", "reason": "user_action"},
            writer=token,
        )
    finally:
        store.release_writer(token)
    summary = scan_and_recover(store)
    assert meta.id in summary.interrupted_runs
    _, events = store.read_run(meta.id)
    assert events[-1].type == EventType.RUN_CANCELLED
    fresh, _ = store.read_run(meta.id)
    assert fresh.status == "cancelled"


def test_running_on_boot_without_progress_is_interrupted(tmp_errorta_home, runs_dir_path) -> None:
    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(
        room_id="rm", room_snapshot={"id": "rm", "members": []}, prompt="hi", corpus_ids=[]
    )
    token = store.acquire_writer(meta.id)
    try:
        store.append_event(
            meta.id,
            type=EventType.RUN_STARTED,
            status=EventStatus.RUNNING,
            payload={},
            writer=token,
        )
    finally:
        store.release_writer(token)
    summary = scan_and_recover(store)
    assert meta.id in summary.interrupted_runs
    fresh, _ = store.read_run(meta.id)
    assert fresh.status == "interrupted"


def test_deliberate_bug_decision_raising_cap_rejected(tmp_errorta_home, runs_dir_path) -> None:
    """Invariant 7: a decision event whose payload tries to raise max_rounds is rejected."""
    bad_event_payload = {
        "decision": {
            "choice": "continue_local_only",
            "scope": "remainder_of_run",
            "override_max_rounds": 10,
        }
    }
    with pytest.raises(ValueError) as exc:
        validate_decision_event(bad_event_payload, current_max_rounds=2)
    assert "cap_invariant_violated" in str(exc.value)
