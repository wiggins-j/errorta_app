# python/tests/liverun/test_state.py
from __future__ import annotations

import json
from pathlib import Path

import pytest

from errorta_liverun.profile import Caps
from errorta_liverun.state import (LaunchLedger, PHASES, TERMINAL_PHASES,
                                    RunState, RunStore)


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


def test_load_returns_none_on_corrupt_json(tmp_path: Path) -> None:
    store = RunStore()
    st = _state(store)
    store.save(st)
    (tmp_path / "liverun" / "runs" / st.run_id / "state.json").write_text("{not json")
    assert store.load(st.run_id) is None


def test_load_returns_none_on_json_list(tmp_path: Path) -> None:
    store = RunStore()
    st = _state(store)
    store.save(st)
    (tmp_path / "liverun" / "runs" / st.run_id / "state.json").write_text("[1, 2, 3]")
    assert store.load(st.run_id) is None


def test_events_skips_garbage_lines(tmp_path: Path) -> None:
    store = RunStore()
    st = _state(store)
    store.save(st)
    events_path = tmp_path / "liverun" / "runs" / st.run_id / "events.jsonl"
    events_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps({"seq": 1, "at": "t", "kind": "phase", "detail": {}}),
        "not json at all",
        json.dumps([1, 2, 3]),
        json.dumps({"seq": "x", "at": "t", "kind": "bad-seq", "detail": {}}),
        json.dumps({"seq": 2, "at": "t", "kind": "step", "detail": {}}),
    ]
    events_path.write_text("\n".join(lines) + "\n")
    evs = store.events(st.run_id)
    assert [e["seq"] for e in evs] == [1, 2]
    assert [e["kind"] for e in evs] == ["phase", "step"]


def test_launch_ledger_check_survives_garbage_and_missing_at(tmp_path: Path) -> None:
    led = LaunchLedger(tmp_path / "garbage.jsonl")
    caps = Caps(max_launches_per_hour=2, min_launch_gap_s=900, max_launches_per_day=3,
                max_consecutive_failed_cycles=2)
    led._path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "not json",
        json.dumps({"kind": "launch", "profile": "p", "run_id": "bad"}),  # missing "at"
        json.dumps({"kind": "launch", "profile": "p", "run_id": "r1", "at": 1_000_000.0}),
    ]
    led._path.write_text("\n".join(lines) + "\n")
    assert led.check("p", caps, 1_000_000.0 + 10) == "cap_gap"
    assert led.check("p", caps, 1_000_000.0 + 3700) is None


def test_run_state_from_dict_partial_fills_defaults() -> None:
    st = RunState.from_dict({"run_id": "r", "profile_name": "p", "phase": "idle"})
    assert st.run_id == "r"
    assert st.profile_name == "p"
    assert st.phase == "idle"
    assert st.project_id is None
    assert st.reason is None
    assert st.session_id is None
    assert st.step_index is None
    assert st.started_at is None
    assert st.launched_at is None
    assert st.ended_at is None
    assert st.owned_pgids == []
    assert st.owned_remote_pidfiles == []
    assert st.owned_tunnels == []
    assert st.probe_last_ok == {}
    assert st.probe_last_value == {}
    assert st.literals == {}
    assert st.evidence_dir == ""


def test_append_event_is_concurrency_safe_across_threads(tmp_path: Path) -> None:
    import threading

    store = RunStore()
    st = _state(store)
    store.save(st)

    n_threads = 8
    per_thread = 25
    seqs: list[int] = []
    seqs_lock = threading.Lock()

    def worker() -> None:
        for i in range(per_thread):
            seq = store.append_event(st.run_id, "tick", {"i": i})
            with seqs_lock:
                seqs.append(seq)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(seqs) == list(range(1, n_threads * per_thread + 1))


# --- Slice 2: fix-cycle ledger + new phases -------------------------------- #

def test_fix_cycles_today_counts_a_rolling_24h(tmp_path: Path) -> None:
    led = LaunchLedger(tmp_path / "launches.jsonl")
    now = 1_700_000_000.0
    led.record_fix_cycle("osrs", "r1", "brain", failed=False, at=now - 90_000)  # >24h
    led.record_fix_cycle("osrs", "r2", "brain", failed=True, at=now - 100)
    led.record_fix_cycle("osrs", "r3", "brain", failed=False, at=now - 50)
    assert led.fix_cycles_today("osrs", now) == 2
    assert LaunchLedger(tmp_path / "launches.jsonl").fix_cycles_today("osrs", now) == 2
    assert led.fix_cycles_today("other", now) == 0


def test_fix_cycles_live_in_their_own_file_and_never_move_launch_caps(tmp_path: Path) -> None:
    led = LaunchLedger(tmp_path / "launches.jsonl")
    led.record_fix_cycle("osrs", "r1", "brain", failed=True, at=1.0)
    assert (tmp_path / "fixcycles.jsonl").is_file()
    assert not (tmp_path / "launches.jsonl").exists()
    # a fix cycle is not a launch and must not trip a launch cap
    assert led.check("osrs", Caps(), 100.0) is None


def test_fix_cycles_today_survives_a_malformed_row(tmp_path: Path) -> None:
    led = LaunchLedger(tmp_path / "launches.jsonl")
    led.record_fix_cycle("osrs", "r1", "brain", failed=False, at=10.0)
    with (tmp_path / "fixcycles.jsonl").open("a") as fh:
        fh.write("not json\n")
        fh.write(json.dumps({"profile": "osrs", "at": "soon"}) + "\n")
    assert led.fix_cycles_today("osrs", 20.0) == 1


def test_new_phases_are_not_terminal() -> None:
    for phase in ("fixing", "accepting", "deploying"):
        assert phase in PHASES and phase not in TERMINAL_PHASES


def test_runstate_from_dict_defaults_new_fields() -> None:
    st = RunState.from_dict({"run_id": "r", "profile_name": "p", "project_id": None,
                             "phase": "stopped", "reason": None, "session_id": "s",
                             "step_index": 0, "started_at": "x", "launched_at": None,
                             "ended_at": None})
    assert (st.fix_of, st.fix_cycle, st.fix_repo_id, st.fix_task_id) == (None, 0, None, None)


def test_fix_fields_roundtrip_through_the_store(tmp_path: Path) -> None:
    store = RunStore()
    st = _state(store, "fixing")
    st.fix_of, st.fix_cycle, st.fix_repo_id, st.fix_task_id = ("old", 2, "brain", "t-1")
    store.save(st)
    assert store.load(st.run_id) == st
    assert [s.run_id for s in store.list_non_terminal()] == [st.run_id]


def test_a_human_reset_forgives_the_failure_streak_but_not_the_rate_caps(tmp_path: Path) -> None:
    led = LaunchLedger(tmp_path / "l.jsonl")
    caps = Caps(max_launches_per_hour=2, min_launch_gap_s=10, max_launches_per_day=8,
                max_consecutive_failed_cycles=2)
    t0 = 1_000_000.0
    led.record("p", "a", t0); led.record_outcome("a", failed=True)
    led.record("p", "b", t0 + 4000); led.record_outcome("b", failed=True)
    assert led.check("p", caps, t0 + 8000) == "cap_consecutive_failures"
    led.record_reset("p", t0 + 8001)
    assert led.check("p", caps, t0 + 8002) is None
    # the reset does not launder a burst
    led.record("p", "c", t0 + 8010); led.record("p", "d", t0 + 8030)
    assert led.check("p", caps, t0 + 8050) == "cap_hourly"
    # and a NEW streak after the reset still trips
    led.record_outcome("c", failed=True); led.record_outcome("d", failed=True)
    assert led.check("p", caps, t0 + 20000) == "cap_consecutive_failures"
