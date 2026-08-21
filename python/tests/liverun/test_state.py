# python/tests/liverun/test_state.py
from __future__ import annotations

import json
from pathlib import Path

import pytest

from errorta_liverun.profile import Caps
from errorta_liverun.state import LaunchLedger, RunState, RunStore


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    return tmp_path


def _state(store: RunStore, phase: str = "launching") -> RunState:
    rid = store.new_run_id()
    return RunState(run_id=rid, profile_name="p", project_id=None, phase=phase, reason=None,
                    session_id="s-" + rid, step_index=0, started_at="2026-08-21T00:00:00Z",
                    launched_at=None, ended_at=None, owned_pgids=[], owned_remote_pidfiles=[],
                    owned_tunnels=[], probe_last_ok={}, probe_last_value={}, literals={},
                    evidence_dir=str(store.evidence_dir(rid)))


def test_save_load_roundtrip_is_atomic(tmp_path: Path) -> None:
    store = RunStore()
    st = _state(store)
    st.owned_pgids.append(4242)
    store.save(st)
    assert store.load(st.run_id) == st
    assert not list((tmp_path / "liverun" / "runs" / st.run_id).glob("*.tmp"))


def test_list_non_terminal_filters(tmp_path: Path) -> None:
    store = RunStore()
    a = _state(store, "watching"); b = _state(store, "stopped"); c = _state(store, "lost_on_restart")
    for s in (a, b, c):
        store.save(s)
    assert [s.run_id for s in store.list_non_terminal()] == [a.run_id]


def test_events_are_monotonic_and_readable(tmp_path: Path) -> None:
    store = RunStore(); st = _state(store); store.save(st)
    assert store.append_event(st.run_id, "phase", {"to": "launching"}) == 1
    assert store.append_event(st.run_id, "step", {"name": "x", "ok": True}) == 2
    evs = store.events(st.run_id, after_seq=1)
    assert [e["seq"] for e in evs] == [2] and evs[0]["kind"] == "step"
    raw = (tmp_path / "liverun" / "runs" / st.run_id / "events.jsonl").read_text().splitlines()
    assert json.loads(raw[0])["seq"] == 1


def test_launch_ledger_caps(tmp_path: Path) -> None:
    led = LaunchLedger()
    caps = Caps(max_launches_per_hour=2, min_launch_gap_s=900, max_launches_per_day=3,
                max_consecutive_failed_cycles=2)
    t0 = 1_000_000.0
    assert led.check("p", caps, t0) is None
    led.record("p", "r1", t0)
    assert led.check("p", caps, t0 + 10) == "cap_gap"
    led.record("p", "r2", t0 + 1000)
    assert led.check("p", caps, t0 + 2000) == "cap_hourly"
    assert led.check("p", caps, t0 + 3700) is None
    led.record("p", "r3", t0 + 3700)
    assert led.check("p", caps, t0 + 7300) == "cap_daily"
    # consecutive failures
    led2 = LaunchLedger(tmp_path / "other.jsonl")
    led2.record("p", "a", t0); led2.record_outcome("a", failed=True)
    led2.record("p", "b", t0 + 4000); led2.record_outcome("b", failed=True)
    assert led2.check("p", caps, t0 + 90_000) == "cap_consecutive_failures"
    led2.record("p", "c", t0 + 90_000); led2.record_outcome("c", failed=False)
    assert led2.check("p", caps, t0 + 200_000) is None


def test_launch_ledger_is_per_profile(tmp_path: Path) -> None:
    led = LaunchLedger(); caps = Caps()
    led.record("p", "r1", 0.0)
    assert led.check("q", caps, 1.0) is None
