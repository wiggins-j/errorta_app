# python/tests/liverun/test_supervisor.py
from __future__ import annotations

import time
from pathlib import Path

import pytest

from errorta_liverun import profile as P
from errorta_liverun.state import LaunchLedger, RunStore
from errorta_liverun.steps import StepResult
from errorta_liverun.supervisor import (
    LiveRunManager,
    LiveRunRefused,
    Supervisor,
    paused_marker,
)


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    return tmp_path


class FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t
    def __call__(self) -> float:
        return self.t
    def sleep(self, s: float) -> None:
        self.t += s


def _ok(*a, **k) -> StepResult:
    return StepResult(True, "t", "t")


def _profile(*, launch_ok=True, warn=False) -> P.Profile:
    act = P.Action("local", {"argv": ("/bin/true",), "cwd": None})
    launch = (P.Step("one", act, P.Check("file_exists", "/"), 5),)
    watch = (P.WatchProbe("alive", 10, 30, "warn" if warn else "stop", P.Probe("http", {"url": "http://127.0.0.1:1/"})),
             P.WatchProbe("clock", 10, 0, "stop", P.Probe("elapsed_lt_s", 10_000)))
    teardown = (P.Step("logoff", None, P.Check("file_exists", "/"), 5, "logoff_verified"),
                P.Step("quit", act, None, 5))
    evidence = (P.Step("ev", act, None, 5),)
    return P.Profile("p", {}, {}, launch, watch, evidence, teardown, P.DEFAULT_CAPS, ("Account is banned",))


def _sup(prof, clock, *, probe, check=None, action=None, store=None, ledger=None, wall=None) -> Supervisor:
    return Supervisor(prof, store=store or RunStore(), ledger=ledger or LaunchLedger(), tunnels=None, remote=None,
                      clock=clock, sleep=clock.sleep, wall=wall or clock,
                      run_action=action or _ok, run_check=check or (lambda c, ctx, step_start: True),
                      run_probe=probe)


def test_happy_launch_then_stall_tears_down_with_literal() -> None:
    clock = FakeClock()
    probe_ok = {"v": True}
    sup = _sup(_profile(), clock, probe=lambda p, ctx: probe_ok["v"] if p.kind == "http" else True)
    sup.start(blocking=False)
    # drive synchronously: a few healthy ticks, then the probe dies
    sup._tick(); clock.sleep(5); sup._tick()
    assert sup.state.phase == "watching"
    probe_ok["v"] = False
    for _ in range(5):
        clock.sleep(10); sup._tick()
    assert sup.state.phase == "stopped"
    assert sup.state.reason == "stall:alive"
    assert sup.state.literals == {"logoff_verified": True}
    kinds = [e["kind"] for e in sup.store.events(sup.state.run_id)]
    assert "evidence" in kinds and "teardown_step" in kinds and kinds[-1] == "phase"
    assert clock.t - 1000.0 >= 30  # stall_after honoured, not iteration-counted


def test_warn_probe_posts_once_and_keeps_watching() -> None:
    clock = FakeClock()
    sup = _sup(_profile(warn=True), clock, probe=lambda p, ctx: p.kind != "http")
    sup.start(blocking=False)
    for _ in range(8):
        clock.sleep(10); sup._tick()
    assert sup.state.phase == "watching"
    warns = [e for e in sup.store.events(sup.state.run_id) if e["kind"] == "probe_warn"]
    assert len(warns) == 1


def test_launch_step_failure_tears_down() -> None:
    clock = FakeClock()
    sup = _sup(_profile(), clock, probe=lambda p, ctx: True,
               check=lambda c, ctx, step_start: c.kind != "file_exists" or ctx.run_id == "never")
    sup.start(blocking=False)
    for _ in range(5):
        sup._tick()
    assert sup.state.phase == "failed"
    assert sup.state.reason.startswith("launch_step_failed:one")
    assert sup.state.literals.get("logoff_verified") is False


def test_missing_logoff_literal_is_reported_absent() -> None:
    clock = FakeClock()
    def check(c, ctx, step_start):
        return False  # logoff never verified
    sup = _sup(_profile(), clock, probe=lambda p, ctx: True, check=check)
    sup.start(blocking=False)
    sup._tick()  # launch step check fails -> failed path
    for _ in range(3):
        sup._tick()
    lits = [e for e in sup.store.events(sup.state.run_id) if e["kind"] == "literals"][-1]
    assert lits["detail"]["logoff_verified"] == "ABSENT"


def test_ban_signal_in_evidence_pauses() -> None:
    clock = FakeClock()
    def action(a, ctx, timeout_s):
        return StepResult(True, "t", "t", stdout_tail="Login failed: Account is banned")
    sup = _sup(_profile(), clock, probe=lambda p, ctx: p.kind != "http", action=action)
    sup.start(blocking=False)
    for _ in range(8):
        clock.sleep(10); sup._tick()
    assert sup.state.phase == "paused_awaiting_human"
    assert any(e["kind"] == "ban_signal" for e in sup.store.events(sup.state.run_id))


def test_caps_refuse_second_launch_inside_gap() -> None:
    clock = FakeClock()
    a = _sup(_profile(), clock, probe=lambda p, ctx: True)
    a.start(blocking=False); a.stop("operator_stop"); a._tick(); a._tick()
    assert a.state.phase == "stopped"
    b = _sup(_profile(), clock, probe=lambda p, ctx: True)
    with pytest.raises(LiveRunRefused) as ei:
        b.start(blocking=False)
    assert ei.value.code == "cap_gap"


def test_stop_during_launch_interrupts_and_tears_down() -> None:
    clock = FakeClock()
    calls = []
    def action(a, ctx, timeout_s):
        calls.append(ctx.run_id)
        return _ok()
    prof = _profile()
    prof = P.Profile(prof.name, prof.hosts, prof.tunnels, prof.launch * 3, prof.watch, prof.evidence,
                     prof.teardown, prof.caps, prof.ban_signals)
    sup = _sup(prof, clock, probe=lambda p, ctx: True, action=action)
    sup.start(blocking=False)
    sup._tick()           # step 0
    sup.stop("operator_stop")
    for _ in range(4):
        sup._tick()
    assert sup.state.phase == "stopped" and sup.state.reason == "operator_stop"
    assert sup.state.step_index < 3


def test_brain_refusal_rc3_counts_failed_cycle() -> None:
    clock = FakeClock()
    def action(a, ctx, timeout_s):
        return StepResult(False, "t", "t", exit_code=3, stderr_tail="REFUSED: risk budget")
    sup = _sup(_profile(), clock, probe=lambda p, ctx: True, action=action)
    sup.start(blocking=False)
    for _ in range(4):
        sup._tick()
    assert sup.state.phase == "failed" and "refused" in sup.state.reason
    rows = sup.ledger._rows()
    assert any(r.get("kind") == "outcome" and r["failed"] for r in rows)


# --- beyond the brief ----------------------------------------------------- #

def test_operator_stop_is_not_a_failed_cycle() -> None:
    clock = FakeClock()
    sup = _sup(_profile(), clock, probe=lambda p, ctx: True)
    sup.start(blocking=False)
    sup.stop("operator_stop")
    sup._tick()
    outcomes = [r for r in sup.ledger._rows() if r.get("kind") == "outcome"]
    assert outcomes and outcomes[-1]["failed"] is False


def test_ban_signal_first_seen_in_evidence_pauses() -> None:
    """The launch was clean; the ban only surfaces in the evidence sweep."""
    clock = FakeClock()
    box: dict[str, Supervisor] = {}
    def action(a, ctx, timeout_s):
        if box["sup"].state.phase == "stopping":
            return StepResult(True, "t", "t", stdout_tail="Account is banned")
        return _ok()
    sup = _sup(_profile(), clock, probe=lambda p, ctx: True, action=action)
    box["sup"] = sup
    sup.start(blocking=False)
    sup._tick()
    sup.stop("operator_stop"); sup._tick()
    assert sup.state.phase == "paused_awaiting_human"
    bans = [e for e in sup.store.events(sup.state.run_id) if e["kind"] == "ban_signal"]
    assert len(bans) == 1 and bans[0]["detail"]["where"] == "evidence:ev"
    assert paused_marker("p").exists()


def test_second_start_on_one_supervisor_is_refused() -> None:
    clock = FakeClock()
    sup = _sup(_profile(), clock, probe=lambda p, ctx: True)
    sup.start(blocking=False)
    with pytest.raises(LiveRunRefused) as ei:
        sup.start(blocking=False)
    assert ei.value.code == "already_started"
    assert len([r for r in sup.ledger._rows() if r.get("kind") == "launch"]) == 1
    sup.stop("operator_stop"); sup._tick()


def test_state_is_persisted_at_every_transition() -> None:
    clock = FakeClock()
    sup = _sup(_profile(), clock, probe=lambda p, ctx: True)
    sup.start(blocking=False)
    assert sup.store.load(sup.state.run_id).phase == "launching"
    sup._tick(); sup._tick()
    saved = sup.store.load(sup.state.run_id)
    assert saved.phase == "watching" and saved.step_index == 1 and saved.launched_at
    sup.stop("operator_stop"); sup._tick()
    saved = sup.store.load(sup.state.run_id)
    assert saved.phase == "stopped" and saved.ended_at and saved.literals == {"logoff_verified": True}


def test_stall_window_is_wall_clock_since_last_ok_and_resets_on_recovery() -> None:
    """A failing probe is only a stall once `stall_after_s` of WALL time has
    passed since it last succeeded — not on the first bad reading, and not on
    failures accumulated across a recovery."""
    clock = FakeClock()
    probe_ok = {"v": True}
    sup = _sup(_profile(), clock, probe=lambda p, ctx: probe_ok["v"] if p.kind == "http" else True)
    sup.start(blocking=False)
    sup._tick(); sup._tick()
    assert sup.state.phase == "watching"
    probe_ok["v"] = False
    for _ in range(2):                  # 20s of failure: inside the 30s window
        clock.sleep(10); sup._tick()
    assert sup.state.phase == "watching"
    probe_ok["v"] = True
    clock.sleep(10); sup._tick()        # recovered -> the window restarts here
    probe_ok["v"] = False
    for _ in range(2):
        clock.sleep(10); sup._tick()
    assert sup.state.phase == "watching"  # 20s again, not 40s of accumulated failures
    clock.sleep(10); sup._tick()
    assert sup.state.phase == "stopped" and sup.state.reason == "stall:alive"


def test_check_step_start_is_wall_clock_not_monotonic() -> None:
    """`file_mtime_newer` compares `step_start` against an epoch mtime, so the
    supervisor must hand checks wall time even though it times steps out on
    the monotonic clock."""
    clock = FakeClock(1000.0)              # monotonic: seconds since boot
    wall = FakeClock(1_700_000_000.0)      # epoch seconds
    seen: list[float] = []
    def check(c, ctx, step_start):
        seen.append(step_start)
        return True
    sup = _sup(_profile(), clock, probe=lambda p, ctx: True, check=check, wall=wall)
    sup.start(blocking=False)
    sup._tick()                            # launch step check
    sup.stop("operator_stop"); sup._tick()  # teardown logoff check
    assert len(seen) == 2
    assert all(s >= 1_700_000_000.0 for s in seen)


def test_consecutive_failed_cycles_pause_the_profile() -> None:
    clock = FakeClock()
    store, ledger = RunStore(), LaunchLedger()
    def action(a, ctx, timeout_s):
        return StepResult(False, "t", "t", exit_code=1)
    prof = _profile()
    for _ in range(2):
        sup = _sup(prof, clock, probe=lambda p, ctx: True, action=action, store=store, ledger=ledger)
        sup.start(blocking=False)
        sup._tick()
        clock.sleep(4000)  # past min_launch_gap_s and outside the hourly window
    assert sup.state.phase == "paused_awaiting_human"
    assert sup.state.reason == "cap_consecutive_failures"
    assert [e["kind"] for e in sup.store.events(sup.state.run_id)].count("caps") == 1
    assert paused_marker("p").exists()
    blocked = _sup(prof, clock, probe=lambda p, ctx: True, store=store, ledger=ledger)
    with pytest.raises(LiveRunRefused) as ei:
        blocked.start(blocking=False)
    assert ei.value.code == "paused_awaiting_human"


def test_supervisor_crash_still_tears_down() -> None:
    clock = FakeClock()
    def action(a, ctx, timeout_s):
        raise RuntimeError("boom")
    sup = _sup(_profile(), clock, probe=lambda p, ctx: True, action=action)
    sup.start(blocking=True)
    assert sup.state.phase == "failed"
    assert sup.state.reason == "supervisor_error:RuntimeError"
    kinds = [e["kind"] for e in sup.store.events(sup.state.run_id)]
    assert "teardown_step" in kinds and "literals" in kinds
    assert sup.state.literals["logoff_verified"] is True  # the logoff check still ran


def test_probe_exception_counts_as_not_ok_and_is_recorded() -> None:
    clock = FakeClock()
    def probe(p, ctx):
        if p.kind == "http":
            raise OSError("no route")
        return True
    sup = _sup(_profile(), clock, probe=probe)
    sup.start(blocking=False)
    sup._tick(); sup._tick()
    for _ in range(4):
        clock.sleep(10); sup._tick()
    assert sup.state.phase == "stopped" and sup.state.reason == "stall:alive"
    assert any(e["kind"] == "probe_error" for e in sup.store.events(sup.state.run_id))


def _wait_for(pred, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def test_manager_start_status_stop_roundtrip(tmp_path: Path) -> None:
    prof = P.Profile("m", {}, {}, (), (), (), (), P.DEFAULT_CAPS, ())
    mgr = LiveRunManager(store=RunStore(), ledger=LaunchLedger(), tunnels=None, remote=None,
                         load_profile=lambda path: prof)
    try:
        assert mgr.status() == {"status": "empty", "last": None}
        assert mgr.stop() == {"status": "empty"}
        started = mgr.start("m", project_id="proj-1")
        assert started["status"] == "started"
        assert _wait_for(lambda: mgr.status()["phase"] == "watching")
        st = mgr.status()
        assert st["run_id"] == started["run_id"] and st["probes"] == {}
        assert st["caps"]["would_refuse"] == "cap_gap"  # a relaunch right now is capped
        assert mgr.start("m", project_id="proj-2")["reason"] == "already_running"
        assert mgr.start("other", project_id="proj-1")["reason"] == "project_has_live_run"
        assert mgr.stop(project_id="proj-1")["run_id"] == started["run_id"]
    finally:
        mgr.teardown_all()
    assert _wait_for(lambda: mgr.status()["status"] == "empty")
    last = mgr.status()["last"]
    assert last["phase"] == "stopped" and last["reason"] == "operator_stop"


def test_manager_refuses_traversal_and_unloadable_profiles() -> None:
    mgr = LiveRunManager(store=RunStore(), ledger=LaunchLedger(), tunnels=None, remote=None)
    assert mgr.start("../../etc/passwd")["reason"] == "bad_profile_name"
    assert mgr.start("nope")["reason"].startswith("profile_invalid")


def test_manager_resume_clears_the_paused_marker() -> None:
    mgr = LiveRunManager(store=RunStore(), ledger=LaunchLedger(), tunnels=None, remote=None)
    assert mgr.resume("p") == {"status": "empty"}
    marker = paused_marker("p")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("ban_signal\n")
    assert mgr.resume("p") == {"status": "resumed"}
    assert not marker.exists()
