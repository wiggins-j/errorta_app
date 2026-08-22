# python/tests/liverun/test_supervisor.py
from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

import pytest

from errorta_liverun import profile as P
from errorta_liverun.state import TERMINAL_PHASES, LaunchLedger, RunStore
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


def _sup(prof, clock, *, probe, check=None, action=None, store=None, ledger=None, wall=None,
         tunnels=None) -> Supervisor:
    return Supervisor(prof, store=store or RunStore(), ledger=ledger or LaunchLedger(),
                      tunnels=tunnels, remote=None,
                      clock=clock, sleep=clock.sleep, teardown_sleep=clock.sleep, wall=wall or clock,
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


# --- fix round 1 ----------------------------------------------------------- #

def test_literal_on_a_checkless_teardown_step_is_never_present() -> None:
    """`profile.py` refuses to load such a step, but a `Profile` built in code
    must not be able to forge the literal either: an action's exit status says
    the command ran, not that the world changed. `_run_remote_signal` returns
    ok even when there was no pidfile to signal."""
    clock = FakeClock()
    act = P.Action("local", {"argv": ("/bin/true",), "cwd": None})
    prof = P.Profile("p", {}, {}, (), (), (),
                     (P.Step("logoff", act, None, 5, "logoff_verified"),), P.DEFAULT_CAPS, ())
    sup = _sup(prof, clock, probe=lambda p, ctx: True)   # the action reports ok=True
    sup.start(blocking=False)
    sup.stop("operator_stop"); sup._tick()
    assert sup.state.literals["logoff_verified"] is False
    lits = [e for e in sup.store.events(sup.state.run_id) if e["kind"] == "literals"][-1]
    assert lits["detail"]["logoff_verified"] == "ABSENT"
    td = [e for e in sup.store.events(sup.state.run_id) if e["kind"] == "teardown_step"][-1]
    assert td["detail"]["ok"] is True and td["detail"]["literal_ok"] is False


class _FlakyStore(RunStore):
    """A store whose `append_event` throws once, on the first event of `kind`."""

    def __init__(self, kind: str) -> None:
        super().__init__()
        self._boom_kind = kind

    def append_event(self, run_id: str, kind: str, detail: dict) -> int:
        if kind == self._boom_kind:
            self._boom_kind = None
            raise OSError("disk full")
        return super().append_event(run_id, kind, detail)


def test_a_throwing_teardown_still_kills_records_and_finishes() -> None:
    """The close-out sequence lives in a `finally`: an exception halfway through
    teardown must not cost the kills, the literals verdict, the ledger outcome
    or the terminal phase — a run wedged at `stopping` would make the manager
    refuse every future start of the profile forever."""
    clock = FakeClock()
    store = _FlakyStore("teardown_step")
    sup = _sup(_profile(), clock, probe=lambda p, ctx: True, store=store)
    sup.start(blocking=False)
    child = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
    sup.state.owned_pgids.append(child.pid)
    sup.stop("operator_stop")
    with pytest.raises(OSError):                      # `run_once_blocking` catches this
        sup._tick()
    assert child.wait(timeout=5) is not None          # `_kill_owned` still ran
    assert sup.state.owned_pgids == []
    assert sup.state.phase == "stopped" and sup.state.ended_at
    kinds = [e["kind"] for e in store.events(sup.state.run_id)]
    assert "literals" in kinds and kinds[-1] == "phase"
    assert [r for r in sup.ledger._rows() if r.get("kind") == "outcome"]


def test_a_wedged_close_out_still_leaves_the_run_terminal() -> None:
    """Belt and braces: if even the state write fails, the in-memory phase is
    forced terminal so `LiveRunManager._active` stops reporting the run live."""
    clock = FakeClock()
    sup = _sup(_profile(), clock, probe=lambda p, ctx: True)
    sup.start(blocking=False)
    sup.stop("operator_stop")

    def _boom(*a, **k):
        raise OSError("read-only fs")

    sup._set_phase = _boom  # type: ignore[method-assign]
    with pytest.raises(OSError):
        sup._tick()
    assert sup.state.phase == "failed" and sup.state.phase in TERMINAL_PHASES
    assert sup.state.ended_at and sup.state.reason == "operator_stop"


def test_teardown_polling_does_not_busy_loop_after_stop() -> None:
    """`_sleep` defaults to `self._stop.wait`, which returns instantly once
    `stop()` was called — and teardown only ever runs after that. Teardown polls
    must use their own sleep or they spin, hammering the very ssh/HTTP endpoint
    they are waiting on."""
    clock = FakeClock()
    polls = {"n": 0}

    def check(c, ctx, step_start):
        polls["n"] += 1
        return False                      # the logoff never verifies

    def gated_sleep(_s):                  # stands in for `self._stop.wait` post-stop
        raise AssertionError("teardown must not use the stop-gated sleep")

    prof = _profile()
    prof = P.Profile(prof.name, prof.hosts, prof.tunnels, (), (), (),
                     (P.Step("logoff", None, P.Check("file_exists", "/"), 1, "logoff_verified"),),
                     prof.caps, prof.ban_signals)
    sup = Supervisor(prof, store=RunStore(), ledger=LaunchLedger(), tunnels=None, remote=None,
                     clock=clock, sleep=gated_sleep, teardown_sleep=clock.sleep, wall=clock,
                     run_action=_ok, run_check=check, run_probe=lambda p, ctx: True)
    sup.start(blocking=False)
    sup.stop("operator_stop")
    sup._tick()
    assert sup.state.phase == "stopped"
    assert polls["n"] <= 2                       # 1 s timeout, 2 s poll interval
    assert clock.t - 1000.0 >= 2.0               # it really waited


class _CountingStore(RunStore):
    def __init__(self) -> None:
        super().__init__()
        self.saves = 0

    def save(self, state) -> None:
        self.saves += 1
        super().save(state)


def test_watching_ticks_do_not_rewrite_state_every_second() -> None:
    clock = FakeClock()
    store = _CountingStore()
    sup = _sup(_profile(), clock, probe=lambda p, ctx: True, store=store)
    sup.start(blocking=False)
    sup._tick(); sup._tick()                      # launch step, then -> watching
    before = store.saves
    for _ in range(20):                           # 20 s: two probe rounds, no 30 s mark
        clock.sleep(1); sup._tick()
    assert sup.state.phase == "watching"
    assert store.saves - before <= 4


def test_refusals_are_recorded_as_events() -> None:
    clock = FakeClock()
    sup = _sup(_profile(), clock, probe=lambda p, ctx: True)
    sup.start(blocking=False)
    with pytest.raises(LiveRunRefused):
        sup.start(blocking=False)
    codes = [e["detail"]["code"] for e in sup.store.events(sup.state.run_id)
             if e["kind"] == "refused"]
    assert codes == ["already_started"]
    sup.stop("operator_stop"); sup._tick()

    marker = paused_marker("p")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("ban_signal\n")
    blocked = _sup(_profile(), clock, probe=lambda p, ctx: True)
    with pytest.raises(LiveRunRefused):
        blocked.start(blocking=False)
    assert [e["detail"]["code"] for e in blocked.store.events(blocked.state.run_id)
            if e["kind"] == "refused"] == ["paused_awaiting_human"]


def test_unreleased_tunnels_stay_owned_and_are_reported_absent() -> None:
    """`TunnelManager.close` returns False when its in-memory registry has no
    such tunnel — the normal case after a restart. Nothing was closed, so the id
    may not be quietly forgotten."""
    clock = FakeClock()

    class DeadTunnels:
        def close(self, spec) -> bool:
            return False

    prof = P.Profile("p", {"box": P.Host("box")}, {"t1": P.TunnelDef("t1", "box", ((1, 2),))},
                     (), (), (), (P.Step("logoff", None, P.Check("file_exists", "/"), 1,
                                         "logoff_verified"),), P.DEFAULT_CAPS, ())
    sup = _sup(prof, clock, probe=lambda p, ctx: True, tunnels=DeadTunnels())
    sup.start(blocking=False)
    sup.state.owned_tunnels.append("t1")
    sup.stop("operator_stop"); sup._tick()
    assert sup.state.owned_tunnels == ["t1"]
    ev = [e["detail"] for e in sup.store.events(sup.state.run_id)
          if e["kind"] == "recovery" and e["detail"].get("tunnel") == "t1"]
    assert ev and ev[-1]["tunnel_close"] == "ABSENT"


# --- slice 2: the fix loop ------------------------------------------------- #

from errorta_liverun.state import TERMINAL_PHASES as _TERMINAL  # noqa: E402
from errorta_liverun.supervisor import fix_paused_marker  # noqa: E402
from tests.liverun.test_fixloop import Fake as FixFake  # noqa: E402


def _repo(*, deploy: bool = True, fixable: bool = True) -> P.RepoDef:
    steps = (P.Step("rsync", P.Action("local", {"argv": ("/usr/bin/rsync", "-az", "/s/", "h:d/"),
                                                "cwd": None}), None, 300),) if deploy else ()
    return P.RepoDef("brain", "/r/senditai-ng", "senditai-ng", fixable,
                     ("python_traceback", "brain_log_stall", "journal_stall",
                      "brain_pid_dead"), steps)


def _fix_profile(*, repos=None, fix_loop=None, deploy: bool = True) -> P.Profile:
    """A Slice 1 profile whose stall reason (`stall:brain-log`) triages
    deterministically onto the one declared repo."""
    act = P.Action("local", {"argv": ("/bin/true",), "cwd": None})
    launch = (P.Step("one", act, P.Check("file_exists", "/"), 5),)
    watch = (P.WatchProbe("brain-log", 10, 30, "stop", P.Probe("http", {"url": "http://127.0.0.1:1/"})),)
    teardown = (P.Step("logoff", None, P.Check("file_exists", "/"), 5, "logoff_verified"),)
    evidence = (P.Step("ev", act, None, 5),)
    return P.Profile("p", {}, {}, launch, watch, evidence, teardown, P.DEFAULT_CAPS,
                     ("Account is banned",),
                     (_repo(deploy=deploy),) if repos is None else repos,
                     P.FixLoop(enabled=True) if fix_loop is None else fix_loop)


def _fix_sup(clock, fake: FixFake, *, prof=None, ledger=None, relaunch=None, **kw) -> Supervisor:
    sup = Supervisor(prof or _fix_profile(), store=RunStore(), ledger=ledger or LaunchLedger(),
                     tunnels=None, remote=None, project_id="live-proj",
                     clock=clock, sleep=clock.sleep, teardown_sleep=clock.sleep, wall=clock,
                     run_action=kw.pop("action", None) or fake.run_action,
                     run_check=kw.pop("check", None) or (lambda c, ctx, step_start: True),
                     run_probe=kw.pop("probe", None) or (lambda p, ctx: False),
                     fix_deps=fake.deps(), relaunch_fn=relaunch, **kw)
    return sup


def _events(sup) -> list[dict]:
    return sup.store.events(sup.state.run_id)


def _kinds(sup) -> list[str]:
    return [e["kind"] for e in _events(sup)]


def _skip_codes(sup) -> list[str]:
    return [e["detail"]["code"] for e in _events(sup) if e["kind"] == "fix_skipped"]


def _to_terminal(sup, clock, *, ticks: int = 10) -> None:
    """Drive a real stall (`stall:brain-log`) — the probe is dead from the first
    tick — through launch, watch and teardown, exactly as production does."""
    sup.start(blocking=False)
    sup._tick(); sup._tick()
    for _ in range(ticks):
        if sup.state.phase not in ("launching", "watching"):
            break
        clock.sleep(10)
        sup._tick()


def _drive_cycle(sup, clock, fake: FixFake, *, limit: int = 40) -> None:
    """Tick the fix cycle to a terminal phase, finishing the dev run the first
    time the supervisor watches it."""
    for _ in range(limit):
        if sup.state.phase in _TERMINAL:
            return
        sup._tick()
        if fake.started and fake.store.state.get("status") == "running":
            fake.finish_run()
        clock.sleep(31)


@pytest.mark.parametrize("setup,code", [
    (lambda s: s.stop("operator_stop"), "reason_not_fixable"),
    (lambda s: setattr(s, "_refused", True), "brain_refused"),
    (lambda s: s.profile.__dict__.update(fix_loop=None), "fix_loop_disabled"),
    (lambda s: s.profile.__dict__.update(repos=()), "no_repos"),
    (lambda s: fix_paused_marker(s.profile.name).parent.mkdir(parents=True, exist_ok=True)
     or fix_paused_marker(s.profile.name).touch(), "fix_loop_paused"),
])
def test_entry_conditions_emit_exactly_one_fix_skipped(setup, code) -> None:
    clock = FakeClock()
    fake = FixFake()
    sup = _fix_sup(clock, fake)
    setup(sup)
    _to_terminal(sup, clock)
    assert _skip_codes(sup) == [code]
    assert sup.state.phase in ("stopped", "failed")
    assert fake.store.tasks == []


def test_a_ban_signal_pauses_and_never_reaches_the_fix_loop() -> None:
    clock = FakeClock()
    fake = FixFake()
    sup = _fix_sup(clock, fake,
                   action=lambda a, ctx, timeout_s: StepResult(True, "t", "t",
                                                               stdout_tail="Account is banned"))
    _to_terminal(sup, clock)
    assert sup.state.phase == "paused_awaiting_human"
    assert _skip_codes(sup) == [] and fake.store.tasks == []


def test_a_slice1_profile_never_mentions_the_fix_loop_at_all() -> None:
    clock = FakeClock()
    fake = FixFake()
    sup = _fix_sup(clock, fake, prof=_profile())      # no repos, no fix_loop
    _to_terminal(sup, clock)
    assert sup.state.phase == "stopped" and _skip_codes(sup) == []


@pytest.mark.parametrize("result", [
    StepResult(False, "t", "t", exit_code=3),
    StepResult(False, "t", "t", exit_code=1, stdout_tail="REFUSED: risk budget exhausted"),
])
def test_a_refusing_launch_step_is_not_a_fixable_failure(result) -> None:
    clock = FakeClock()
    fake = FixFake()
    sup = _fix_sup(clock, fake, action=lambda a, ctx, timeout_s: result)
    sup.start(blocking=False)
    for _ in range(4):
        sup._tick()
    assert sup.state.phase == "failed"
    assert _skip_codes(sup) == ["brain_refused"]


def test_a_plain_launch_step_failure_is_fixable() -> None:
    clock = FakeClock()
    fake = FixFake()
    sup = _fix_sup(clock, fake, action=lambda a, ctx, timeout_s: StepResult(False, "t", "t",
                                                                            exit_code=1))
    sup.start(blocking=False)
    sup._tick()
    assert sup.state.phase == "fixing"
    assert sup.state.reason.startswith("launch_step_failed:one")
    assert _skip_codes(sup) == []


def test_a_launch_failure_no_repo_claims_pauses_rather_than_guessing() -> None:
    """`launch_step_failed` is a class like any other: a profile whose repos do
    not declare it gets an ambiguous triage and a human, not a coin flip."""
    clock = FakeClock()
    fake = FixFake()
    sup = _fix_sup(clock, fake, action=lambda a, ctx, timeout_s: StepResult(False, "t", "t",
                                                                            exit_code=1))
    sup.start(blocking=False)
    for _ in range(4):
        sup._tick()
    assert sup.state.phase == "paused_awaiting_human"
    assert sup.state.reason == "triage_ambiguous" and fake.store.tasks == []


def test_fixable_stop_enters_fixing_after_teardown_completed() -> None:
    clock = FakeClock()
    fake = FixFake()
    sup = _fix_sup(clock, fake)
    _to_terminal(sup, clock)
    assert sup.state.phase == "fixing"
    sup._tick()                                  # the cycle's first step: triage
    kinds = _kinds(sup)
    assert kinds.index("teardown_step") < kinds.index("fix_triage")
    assert kinds.index("literals") < kinds.index("fix_triage")


def test_the_whole_cycle_files_a_task_deploys_and_relaunches() -> None:
    clock = FakeClock()
    fake = FixFake()
    seen: list[tuple[str, dict]] = []
    sup = _fix_sup(clock, fake,
                   relaunch=lambda **kw: (seen.append((sup.state.phase, kw)),
                                          {"status": "started", "run_id": "lr-2"})[1])
    _to_terminal(sup, clock)
    _drive_cycle(sup, clock, fake)
    assert sup.state.phase == "stopped"
    assert sup.state.reason == "fix_cycle_complete:brain"
    assert fake.store.tasks and fake.started
    assert "/usr/bin/rsync" in fake.actions
    kinds = _kinds(sup)
    for k in ("fix_triage", "fix_task", "fix_run", "fix_accept_staged", "fix_accepted",
              "deploy_step"):
        assert k in kinds, k
    assert len(seen) == 1
    phase_at_relaunch, kw = seen[0]
    assert phase_at_relaunch in _TERMINAL            # never "deploying"
    assert kw["fix_of"] == sup.state.run_id and kw["fix_cycle"] == 1
    assert kw["profile_name"] == "p" and kw["project_id"] == "live-proj"
    assert sup.ledger.fix_cycles_today("p", clock()) == 1
    assert sup.state.fix_repo_id == "brain" and sup.state.fix_task_id == "t1"


def test_a_cap_refused_relaunch_is_an_event_not_a_retry() -> None:
    clock = FakeClock()
    fake = FixFake()
    calls: list[dict] = []
    sup = _fix_sup(clock, fake,
                   relaunch=lambda **kw: (calls.append(kw),
                                          {"status": "refused", "reason": "cap_gap"})[1])
    _to_terminal(sup, clock)
    _drive_cycle(sup, clock, fake)
    refused = [e for e in _events(sup) if e["kind"] == "relaunch_refused"]
    assert len(refused) == 1 and refused[0]["detail"]["code"] == "cap_gap"
    assert len(calls) == 1                            # refused once, never retried
    assert sup.state.phase in _TERMINAL


def test_a_relaunch_that_raises_is_still_only_one_event() -> None:
    clock = FakeClock()
    fake = FixFake()
    def boom(**kw):
        raise RuntimeError("manager down")
    sup = _fix_sup(clock, fake, relaunch=boom)
    _to_terminal(sup, clock)
    _drive_cycle(sup, clock, fake)
    refused = [e for e in _events(sup) if e["kind"] == "relaunch_refused"]
    assert len(refused) == 1 and refused[0]["detail"]["code"] == "error:RuntimeError"


def test_day_cap_pauses_on_the_fourth_cycle() -> None:
    clock = FakeClock()
    fake = FixFake()
    ledger = LaunchLedger()
    for i in range(3):
        ledger.record_fix_cycle("p", f"r{i}", "brain", failed=False, at=clock())
    sup = _fix_sup(clock, fake, ledger=ledger)
    _to_terminal(sup, clock)
    caps = [e for e in _events(sup) if e["kind"] == "fix_cycle_cap"]
    assert len(caps) == 1 and caps[0]["detail"] == {"cycles_today": 3, "cap": 3}
    assert sup.state.phase == "paused_awaiting_human"
    assert paused_marker("p").exists() and fake.store.tasks == []
    assert _skip_codes(sup) == []                     # the cap event says it, once


def test_the_day_cap_counter_survives_a_ledger_reload() -> None:
    clock = FakeClock()
    ledger = LaunchLedger()
    for i in range(3):
        ledger.record_fix_cycle("p", f"r{i}", "brain", failed=False, at=clock())
    assert LaunchLedger().fix_cycles_today("p", clock()) == 3


def test_a_paused_cycle_counts_a_failed_fix_cycle() -> None:
    clock = FakeClock()
    fake = FixFake()
    fake.confirm_state = "declined"
    sup = _fix_sup(clock, fake)
    _to_terminal(sup, clock)
    _drive_cycle(sup, clock, fake)
    assert sup.state.phase == "paused_awaiting_human" and sup.state.reason == "fix_declined"
    assert sup.ledger.fix_cycles_today("p", clock()) == 1
    assert "/usr/bin/rsync" not in fake.actions       # nothing deployed


def test_a_cycle_that_never_starts_costs_no_fix_cycle() -> None:
    clock = FakeClock()
    fake = FixFake()
    fake.gate = False
    sup = _fix_sup(clock, fake)
    _to_terminal(sup, clock)
    _drive_cycle(sup, clock, fake)
    assert sup.state.reason == "fix_no_gate"
    assert sup.ledger.fix_cycles_today("p", clock()) == 0


def test_stop_during_fixing_interrupts_and_lands_terminal() -> None:
    clock = FakeClock()
    fake = FixFake()
    sup = _fix_sup(clock, fake)
    _to_terminal(sup, clock)
    assert sup.state.phase == "fixing"
    sup.stop("operator_stop")
    sup._tick()
    assert sup.state.phase in _TERMINAL
    assert "/usr/bin/rsync" not in fake.actions       # no deploy after an operator stop


def test_stop_during_deploying_interrupts_and_lands_terminal() -> None:
    clock = FakeClock()
    fake = FixFake()
    sup = _fix_sup(clock, fake)
    _to_terminal(sup, clock)
    for _ in range(6):                                # into `deploying`
        sup._tick()
        if fake.started and fake.store.state.get("status") == "running":
            fake.finish_run("stopped")
        clock.sleep(31)
        if sup.state.phase == "deploying":
            break
    assert sup.state.phase == "deploying"
    sup.stop("operator_stop")
    sup._tick()
    assert sup.state.phase in _TERMINAL


def test_the_fix_phases_are_persisted_at_every_transition() -> None:
    clock = FakeClock()
    fake = FixFake()
    sup = _fix_sup(clock, fake)
    _to_terminal(sup, clock)
    assert sup.store.load(sup.state.run_id).phase == "fixing"
    seen = set()
    for _ in range(12):
        sup._tick()
        if fake.started and fake.store.state.get("status") == "running":
            fake.finish_run("stopped")
        clock.sleep(31)
        seen.add(sup.store.load(sup.state.run_id).phase)
        if sup.state.phase in _TERMINAL:
            break
    assert {"fixing", "accepting", "deploying"} <= seen
    saved = sup.store.load(sup.state.run_id)
    assert saved.fix_repo_id == "brain" and saved.fix_task_id == "t1"


def test_snapshot_reports_the_fix_loop_state() -> None:
    clock = FakeClock()
    fake = FixFake()
    sup = _fix_sup(clock, fake)
    sup.start(blocking=False)
    snap = sup.snapshot()
    assert snap["fix_cycle"] == 0 and snap["fix_of"] is None
    assert snap["fix_cycles_today"] == 0 and snap["fix_cap"] == 3
    assert snap["fix_paused"] is False and snap["fix_repo_id"] is None
    fix_paused_marker("p").parent.mkdir(parents=True, exist_ok=True)
    fix_paused_marker("p").touch()
    assert sup.snapshot()["fix_paused"] is True
    sup.stop("operator_stop"); sup._tick()


def test_a_relaunched_run_records_its_lineage() -> None:
    prof = _fix_profile()
    mgr = LiveRunManager(store=RunStore(), ledger=LaunchLedger(), tunnels=None, remote=None,
                         load_profile=lambda path: prof)
    started = mgr.start("p", project_id="live-proj", fix_of="lr-old", fix_cycle=2)
    try:
        assert started["status"] == "started"
        sup = mgr._runs["p"]
        assert sup.state.fix_of == "lr-old" and sup.state.fix_cycle == 2
        assert sup.snapshot()["fix_of"] == "lr-old"
    finally:
        mgr.teardown_all()


def test_fix_paused_marker_lives_beside_the_run_marker() -> None:
    assert fix_paused_marker("p") != paused_marker("p")
    assert fix_paused_marker("p").parent.parent == paused_marker("p").parent.parent


def _to_phase(sup, clock, fake: FixFake, phase: str, *, limit: int = 12) -> None:
    for _ in range(limit):
        if sup.state.phase == phase:
            return
        sup._tick()
        if fake.started and fake.store.state.get("status") == "running":
            fake.finish_run("stopped")
        clock.sleep(31)
    raise AssertionError(f"never reached {phase}: {sup.state.phase}")


def test_a_shutdown_mid_deploy_never_relaunches() -> None:
    clock = FakeClock()
    fake = FixFake()
    calls: list[dict] = []
    sup = _fix_sup(clock, fake, relaunch=lambda **kw: (calls.append(kw), {"status": "started"})[1])
    _to_terminal(sup, clock)
    _to_phase(sup, clock, fake, "deploying")
    sup.stop("sidecar_shutdown")                      # a shutdown lands mid-deploy
    sup._tick()
    assert calls == []                                # no client launched behind it
    assert sup.state.phase in _TERMINAL
    assert "/usr/bin/rsync" not in fake.actions[1:] or sup.state.reason == "sidecar_shutdown"


def test_a_recovered_run_never_resumes_a_fix_cycle() -> None:
    """A run persisted mid-`fixing` is lost on restart like any other: its
    reason becomes `sidecar_restart`, which no fix cycle may act on."""
    from errorta_liverun import recovery

    clock = FakeClock()
    fake = FixFake()
    store = RunStore()
    sup = _fix_sup(clock, fake, prof=_fix_profile())
    sup.store = store
    _to_terminal(sup, clock)
    assert sup.state.phase == "fixing"
    lost = recovery.recover_on_boot(store=store, tunnels=None, remote=None,
                                    load_profile=lambda path: _fix_profile(),
                                    run_action=_ok,
                                    run_check=lambda c, ctx, step_start: True)
    assert lost == [sup.state.run_id]
    reloaded = store.load(sup.state.run_id)
    assert reloaded.phase == "lost_on_restart"
    assert fake.store.tasks == []
    # ... and says nothing about the fix loop: a run lost to a restart is not a
    # failure anyone can attribute to a repository.
    assert [e for e in store.events(sup.state.run_id)
            if e["kind"] == "fix_skipped" and e["seq"] > 1] == []


def test_a_fix_cycle_runs_on_the_daemon_thread_and_leaks_nothing() -> None:
    """End to end on REAL threads and the REAL (unconfigured) seams: the cycle
    pauses on the first thing it cannot do instead of killing the run's thread,
    and the manager still joins it."""
    prof = _fix_profile()
    prof = P.Profile(prof.name, prof.hosts, prof.tunnels, prof.launch,
                     (P.WatchProbe("brain-log", 1, 0, "stop",
                                   P.Probe("http", {"url": "http://127.0.0.1:1/"})),),
                     prof.evidence, prof.teardown, prof.caps, prof.ban_signals,
                     prof.repos, prof.fix_loop)
    before = threading.active_count()
    mgr = LiveRunManager(store=RunStore(), ledger=LaunchLedger(), tunnels=None, remote=None,
                         load_profile=lambda path: prof)
    try:
        assert mgr.start("p", project_id="live-proj")["status"] == "started"
        sup = mgr._runs["p"]
        assert _wait_for(lambda: sup.state.phase in _TERMINAL, timeout=20.0)
    finally:
        mgr.teardown_all()
    assert sup.state.phase == "paused_awaiting_human"
    assert sup.state.reason in ("fix_no_gate", "triage_ambiguous", "fix_run_failed")
    assert _wait_for(lambda: threading.active_count() <= before, timeout=10.0)


def test_manager_pause_and_resume_fix_toggle_the_fix_marker() -> None:
    """`pause_fix_loop` is R-class and instant; `resume_fix_loop` is C-class and
    human-only. Both address the FIX marker, never the run marker — pausing
    autonomous merging must not stop live runs, and clearing it must not clear
    a ban-class hold."""
    mgr = LiveRunManager(store=RunStore(), ledger=LaunchLedger(), tunnels=None, remote=None)

    assert mgr.resume_fix("p") == {"status": "empty"}
    # "paused", not "pausing": no live run to ask, only a hold to write.
    assert mgr.pause_fix("p") == {"status": "paused", "profile": "p"}
    assert fix_paused_marker("p").exists()
    assert not paused_marker("p").exists()
    assert mgr.pause_fix("p")["status"] == "paused"        # idempotent
    assert mgr.resume_fix("p") == {"status": "resumed", "profile": "p"}
    assert not fix_paused_marker("p").exists()


def test_the_fix_pause_verbs_refuse_a_bad_profile_name() -> None:
    mgr = LiveRunManager(store=RunStore(), ledger=LaunchLedger(), tunnels=None, remote=None)

    for name in ("../../etc/passwd", "", ".hidden"):
        assert mgr.pause_fix(name)["reason"] == "bad_profile_name", name
        assert mgr.resume_fix(name)["reason"] == "bad_profile_name", name


def test_a_fix_paused_profile_still_runs_but_files_nothing() -> None:
    clock = FakeClock()
    fake = FixFake()
    sup = _fix_sup(clock, fake)
    marker = fix_paused_marker(sup.profile.name)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("operator\n")

    _to_terminal(sup, clock)

    assert sup.state.phase == "stopped"
    assert fake.store.tasks == []
    assert [e["detail"]["code"] for e in sup.store.events(sup.state.run_id)
            if e["kind"] == "fix_skipped"] == ["fix_loop_paused"]


# --- fix round 1: what a stopped cycle leaves behind ------------------------ #

def _pending_cids(fake: FixFake) -> list[str]:
    return [cid for cid, r in fake.confirmations.items() if r["state"] == "pending"]


def test_stop_while_awaiting_acceptance_leaves_no_run_and_no_button() -> None:
    """The two things a fix cycle owns that outlive this process: a coding run
    burning tokens, and a staged acceptance the autopilot sweep would fire."""
    clock = FakeClock()
    fake = FixFake()
    fake.confirm_state = "pending"
    sup = _fix_sup(clock, fake)
    _to_terminal(sup, clock)
    _to_phase(sup, clock, fake, "accepting")
    sup._tick()                                        # stage -> await
    assert _pending_cids(fake)
    fake.store.state["status"] = "running"             # a dev turn is still going
    sup.stop("operator_stop")
    sup._tick()

    assert sup.state.phase == "stopped"                 # not `failed`
    assert sup.state.reason == "operator_stop"          # not the stall that started it
    assert fake.store.state["cancel_requested"] is True
    assert _pending_cids(fake) == []
    assert fake.resolved == [(fake.staged and list(fake.confirmations)[0], "declined")]
    aborted = [e for e in _events(sup) if e["kind"] == "fix_aborted"]
    assert len(aborted) == 1 and aborted[0]["detail"]["run_cancelled"] is True
    assert aborted[0]["detail"]["accept_withdrawn"] is True
    assert sup.store.load(sup.state.run_id).fix_confirmation_id is None
    # counted against the day cap, but NOT as the repository's failure
    assert sup.ledger.fix_cycles_today("p", clock()) == 1
    rows = [r for r in sup.ledger._rows() if r.get("kind") == "outcome"]
    assert len(rows) == 1                               # the live session's, once


# --- pausing the fix loop stops the cycle already in flight ---------------- #
#
# The fix-pause marker is read by `_enter_fix_loop`, which a cycle already past
# has already passed. So "pause the fix loop" left an in-flight cycle's dev run
# burning tokens AND its staged acceptance pending -- and the autopilot sweep
# fires on pending records, so the merge the operator just turned off would land
# minutes later. `pause_fix_loop` has to reach the cycle, not just the marker.


def _request_pause_via_manager(sup) -> dict:
    """`pause_fix_loop` as Slack calls it: through the manager that holds the
    supervisor, not by touching the supervisor directly. It only REQUESTS —
    the supervisor's own thread does the aborting on its next tick."""
    mgr = LiveRunManager(store=sup.store, ledger=sup.ledger, tunnels=None, remote=None)
    mgr._runs[sup.profile.name] = sup
    return mgr.pause_fix(sup.profile.name)


def _awaiting_acceptance(clock, fake: FixFake):
    """A supervisor parked in the fix cycle's `await` state, its acceptance
    staged. `fake.confirm_state` decides what the poll will find there."""
    sup = _fix_sup(clock, fake)
    _to_terminal(sup, clock)
    _to_phase(sup, clock, fake, "accepting")
    sup._tick()                                         # stage -> await
    assert fake.staged
    return sup


def test_pausing_the_fix_loop_aborts_the_cycle_awaiting_acceptance() -> None:
    clock = FakeClock()
    fake = FixFake()
    fake.confirm_state = "pending"
    sup = _awaiting_acceptance(clock, fake)
    assert _pending_cids(fake)
    fake.store.state["status"] = "running"              # a dev turn is still going

    out = _request_pause_via_manager(sup)

    assert out["status"] == "pausing"                   # asked, not yet done
    assert out["run_id"] == sup.state.run_id
    assert fix_paused_marker("p").exists()              # the hold, immediately
    sup._tick()                                         # the supervisor honours it

    assert fake.store.state["cancel_requested"] is True     # the dev run, told
    assert _pending_cids(fake) == []                        # the button, withdrawn
    assert fake.resolved == [(list(fake.confirmations)[0], "declined")]
    aborted = [e for e in _events(sup) if e["kind"] == "fix_aborted"]
    assert len(aborted) == 1
    assert aborted[0]["detail"]["reason"] == "fix_loop_paused"
    assert sup.state.phase == "stopped" and sup.state.reason == "fix_loop_paused"
    assert sup.store.load(sup.state.run_id).fix_confirmation_id is None


def test_the_request_is_honoured_before_the_cycle_takes_another_step() -> None:
    """`_tick` reads the request ABOVE the phase dispatch. A cycle that got one
    more step could take the step that reads an approval and deploys."""
    clock = FakeClock()
    fake = FixFake()
    fake.confirm_state = "approved"                     # the merge is ready to go
    sup = _awaiting_acceptance(clock, fake)

    _request_pause_via_manager(sup)
    clock.sleep(31)                                     # the await poll is due
    sup._tick()

    assert sup.state.phase == "stopped"
    assert "/usr/bin/rsync" not in fake.actions         # never deployed


def test_pausing_the_fix_loop_is_idempotent_and_ticks_no_further() -> None:
    clock = FakeClock()
    fake = FixFake()
    fake.confirm_state = "pending"
    sup = _awaiting_acceptance(clock, fake)
    assert _pending_cids(fake)

    assert _request_pause_via_manager(sup)["status"] == "pausing"
    sup._tick()
    # Terminal now, so a second ask finds no live run to address at all...
    assert _request_pause_via_manager(sup) == {"status": "paused", "profile": "p"}
    # ...and the tick loop does not resurrect the cycle or deploy anything.
    for _ in range(3):
        clock.sleep(31)
        sup._tick()
    assert sup.state.phase == "stopped"
    assert len([e for e in _events(sup) if e["kind"] == "fix_aborted"]) == 1
    assert "/usr/bin/rsync" not in fake.actions         # the deploy never ran


def test_pausing_the_fix_loop_leaves_a_watching_run_alone() -> None:
    """Subtracting autonomous MERGING is not stopping the live run: a profile
    still launching or watching has no cycle to abort and must keep going. The
    request is still set — it is simply never honoured, because `_tick` only
    reads it while a fix phase is live."""
    clock = FakeClock()
    fake = FixFake()
    sup = _fix_sup(clock, fake)
    sup.start(blocking=False)
    sup._tick(); sup._tick()
    assert sup.state.phase in ("launching", "watching")

    out = _request_pause_via_manager(sup)
    sup._tick()

    assert out["status"] == "pausing"
    assert sup.state.phase in ("launching", "watching")
    assert fake.store.state.get("cancel_requested") is not True


def test_an_aborted_cycle_still_counts_against_the_day_cap() -> None:
    """It spent a dev run. It is not counted as a FAILURE, though — the
    operator turning autonomy off is not the repository's fault."""
    clock = FakeClock()
    fake = FixFake()
    fake.confirm_state = "pending"
    sup = _awaiting_acceptance(clock, fake)

    _request_pause_via_manager(sup)
    sup._tick()

    assert sup.ledger.fix_cycles_today("p", clock()) == 1


def test_a_storm_of_pause_requests_against_a_running_thread_aborts_exactly_once() -> None:
    """The race the request pattern exists to remove.

    `_abort_fix` and `_close_out` are check-then-set on plain attributes
    (`_fix_aborted`, `_closed`). Doing that work on Slack's thread while the
    daemon thread is inside `_tick_fix` is not merely a theoretical interleave:
    the caller lands the run terminal and records a fix-cycle row, then
    `_tick_fix` resumes, translates the outcome it was already computing, and
    records a SECOND row — landing the profile in `paused_awaiting_human` after
    we told the operator it was stopped.

    The window is forced open rather than hoped for: the cycle's confirmation
    poll blocks inside `_do_await` (a real engine seam, doing exactly what a
    slow disk read does) until twenty concurrent pause requests have landed.
    """
    clock = FakeClock()
    fake = FixFake()
    fake.confirm_state = "pending"
    sup = _awaiting_acceptance(clock, fake)
    assert _pending_cids(fake)
    fake.store.state["status"] = "running"
    mgr = LiveRunManager(store=sup.store, ledger=sup.ledger, tunnels=None, remote=None)
    mgr._runs["p"] = sup

    inside_await = threading.Event()
    may_return = threading.Event()

    def _slow_get_confirmation(cid: str):
        inside_await.set()
        assert may_return.wait(10.0)
        return dict(fake.confirmations[cid]) if cid in fake.confirmations else None

    sup._fix.deps.get_confirmation_fn = _slow_get_confirmation

    def _drive() -> None:
        # `run_once_blocking`'s loop, on a real thread, at its real cadence.
        deadline = time.monotonic() + 15.0
        while sup.state.phase not in _TERMINAL and time.monotonic() < deadline:
            sup._tick()
            clock.sleep(31)             # the await poll is due every tick
            time.sleep(0.001)

    thread = threading.Thread(target=_drive, daemon=True)
    thread.start()
    assert inside_await.wait(10.0)      # the daemon thread is INSIDE _tick_fix
    for _ in range(20):
        mgr.pause_fix("p")
    may_return.set()
    thread.join(timeout=20.0)

    assert not thread.is_alive()
    assert sup.state.phase == "stopped" and sup.state.reason == "fix_loop_paused"
    assert not paused_marker("p").exists()      # no spurious human-hold
    assert len([e for e in _events(sup) if e["kind"] == "fix_aborted"]) == 1
    fix_rows = [r for r in sup.ledger._fix_path.read_text().splitlines() if r.strip()]
    assert len(fix_rows) == 1                   # one cycle, not one per caller
    assert sup.ledger.fix_cycles_today("p", clock()) == 1
    assert _pending_cids(fake) == []


def test_accept_is_staged_answers_only_for_the_live_run_and_its_own_cid() -> None:
    clock = FakeClock()
    fake = FixFake()
    fake.confirm_state = "pending"
    sup = _fix_sup(clock, fake)
    _to_terminal(sup, clock)
    _to_phase(sup, clock, fake, "accepting")
    sup._tick()
    cid = list(fake.confirmations)[0]
    mgr = LiveRunManager(store=sup.store, ledger=sup.ledger, tunnels=None, remote=None)
    mgr._runs["p"] = sup

    assert mgr.accept_is_staged(sup.state.run_id, cid) is True
    # A model composing this verb can guess a run id; it cannot guess the id
    # `stage_confirmation` minted, and it cannot make the run wait on another.
    assert mgr.accept_is_staged(sup.state.run_id, "cid-invented") is False
    assert mgr.accept_is_staged("some-other-run", cid) is False
    assert mgr.accept_is_staged("", "") is False


def test_accept_is_staged_is_false_once_the_run_is_terminal() -> None:
    """A withdrawn or answered acceptance must not stay merge-able: `_active`
    filters terminal runs, and an aborted cycle clears the id outright."""
    clock = FakeClock()
    fake = FixFake()
    fake.confirm_state = "pending"
    sup = _fix_sup(clock, fake)
    _to_terminal(sup, clock)
    _to_phase(sup, clock, fake, "accepting")
    sup._tick()
    cid = list(fake.confirmations)[0]
    mgr = LiveRunManager(store=sup.store, ledger=sup.ledger, tunnels=None, remote=None)
    mgr._runs["p"] = sup
    assert mgr.accept_is_staged(sup.state.run_id, cid) is True

    sup.stop("operator_stop")
    sup._tick()

    assert sup.state.phase in _TERMINAL
    assert mgr.accept_is_staged(sup.state.run_id, cid) is False


def test_accept_is_staged_on_an_empty_manager_is_false() -> None:
    mgr = LiveRunManager(store=RunStore(), ledger=LaunchLedger(), tunnels=None, remote=None)

    assert mgr.accept_is_staged("lr-1", "cid-1") is False


def test_stop_while_the_dev_run_is_still_working_cancels_it() -> None:
    clock = FakeClock()
    fake = FixFake()
    sup = _fix_sup(clock, fake)
    _to_terminal(sup, clock)
    sup._tick(); sup._tick(); sup._tick()               # triage, task, run
    assert fake.started and fake.store.state["status"] == "running"
    sup.stop("operator_stop")
    sup._tick()
    assert sup.state.phase in _TERMINAL
    assert fake.store.state["cancel_requested"] is True
    assert fake.staged == []                            # nothing was ever staged


def test_a_supervisor_crash_mid_cycle_still_withdraws_the_acceptance() -> None:
    clock = FakeClock()
    fake = FixFake()
    fake.confirm_state = "pending"
    sup = _fix_sup(clock, fake)
    _to_terminal(sup, clock)
    _to_phase(sup, clock, fake, "accepting")
    sup._tick()                                        # awaiting
    assert _pending_cids(fake)
    sup._stop_reason = "supervisor_error:RuntimeError"
    sup._do_stopping(final_phase="failed")
    assert sup.state.phase in _TERMINAL
    assert _pending_cids(fake) == []


def test_the_pending_confirmation_id_is_persisted_and_then_cleared() -> None:
    clock = FakeClock()
    fake = FixFake()
    fake.confirm_state = "pending"
    sup = _fix_sup(clock, fake)
    _to_terminal(sup, clock)
    _to_phase(sup, clock, fake, "accepting")
    sup._tick()
    cid = list(fake.confirmations)[0]
    assert sup.store.load(sup.state.run_id).fix_confirmation_id == cid
    # ... and once a human answers it, the run stops claiming it is pending
    fake.confirmations[cid]["state"] = "approved"
    clock.sleep(31); sup._tick()
    assert sup.store.load(sup.state.run_id).fix_confirmation_id is None


def test_boot_recovery_withdraws_an_acceptance_the_dead_sidecar_staged() -> None:
    """The sidecar died between staging and approval. The record is still
    PENDING, and `sweep_autopilot` fires on pending records — for a run that no
    longer exists."""
    from errorta_liverun import recovery

    clock = FakeClock()
    fake = FixFake()
    fake.confirm_state = "pending"
    store = RunStore()
    sup = _fix_sup(clock, fake)
    sup.store = store
    _to_terminal(sup, clock)
    _to_phase(sup, clock, fake, "accepting")
    sup._tick()
    cid = list(fake.confirmations)[0]
    assert _pending_cids(fake) == [cid]

    lost = recovery.recover_on_boot(store=store, tunnels=None, remote=None,
                                    load_profile=lambda path: _fix_profile(),
                                    run_action=_ok,
                                    run_check=lambda c, ctx, step_start: True,
                                    resolve_confirmation_fn=fake._resolve)
    assert lost == [sup.state.run_id]
    assert _pending_cids(fake) == []
    assert fake.resolved == [(cid, "declined")]
    reloaded = store.load(sup.state.run_id)
    assert reloaded.phase == "lost_on_restart" and reloaded.fix_confirmation_id is None
    withdrawn = [e for e in store.events(sup.state.run_id) if e["kind"] == "fix_accept_withdrawn"]
    assert len(withdrawn) == 1 and withdrawn[0]["detail"]["claimed"] is True
    assert withdrawn[0]["detail"]["where"] == "boot_recovery"


def test_boot_recovery_survives_a_confirmation_store_that_refuses() -> None:
    from errorta_liverun import recovery

    clock = FakeClock()
    fake = FixFake()
    fake.confirm_state = "pending"
    store = RunStore()
    sup = _fix_sup(clock, fake)
    sup.store = store
    _to_terminal(sup, clock)
    _to_phase(sup, clock, fake, "accepting")
    sup._tick()

    def boom(cid, decision):
        raise KeyError(cid)

    assert recovery.recover_on_boot(store=store, tunnels=None, remote=None,
                                    load_profile=lambda path: _fix_profile(),
                                    run_action=_ok,
                                    run_check=lambda c, ctx, step_start: True,
                                    resolve_confirmation_fn=boom) == [sup.state.run_id]
    assert store.load(sup.state.run_id).phase == "lost_on_restart"
    ev = [e for e in store.events(sup.state.run_id) if e["kind"] == "fix_accept_withdrawn"][0]
    assert ev["detail"]["claimed"] is False and ev["detail"]["error"] == "KeyError"


def test_a_human_who_approved_first_wins_the_withdrawal_race() -> None:
    clock = FakeClock()
    fake = FixFake()
    fake.confirm_state = "pending"
    sup = _fix_sup(clock, fake)
    _to_terminal(sup, clock)
    _to_phase(sup, clock, fake, "accepting")
    sup._tick()
    cid = list(fake.confirmations)[0]
    fake.confirmations[cid]["state"] = "approved"       # tapped a moment earlier
    sup.stop("operator_stop")
    sup._tick()
    assert fake.confirmations[cid]["state"] == "approved"   # the claim is not fought
    aborted = [e for e in _events(sup) if e["kind"] == "fix_aborted"][0]
    assert aborted["detail"]["accept_withdrawn"] is False


def test_snapshot_survives_a_ledger_that_cannot_be_read() -> None:
    clock = FakeClock()
    fake = FixFake()
    sup = _fix_sup(clock, fake)
    sup.start(blocking=False)
    sup.ledger.fix_cycles_today = lambda *a, **k: (_ for _ in ()).throw(OSError("gone"))
    assert sup.snapshot()["fix_cycles_today"] == -1
    sup.stop("operator_stop"); sup._tick()
