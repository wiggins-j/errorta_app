# python/tests/liverun/test_recovery.py
from __future__ import annotations

import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from errorta_liverun import profile as P
from errorta_liverun.recovery import recover_on_boot
from errorta_liverun.state import LaunchLedger, RunState, RunStore
from errorta_liverun.steps import StepResult
from errorta_liverun.supervisor import paused_marker


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    return tmp_path


def _prof() -> P.Profile:
    td = (P.Step("logoff", None, P.Check("file_exists", "/"), 1, "logoff_verified"),)
    return P.Profile("p", {}, {}, (), (), (), td, P.DEFAULT_CAPS, ())


def _sleeper() -> subprocess.Popen:
    return subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)


def _reaped(child: subprocess.Popen, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if child.poll() is not None:
            return True
        time.sleep(0.02)
    return child.poll() is not None


def test_recovery_tears_down_and_marks_lost() -> None:
    store = RunStore()
    child = _sleeper()
    rid = store.new_run_id()
    st = RunState(run_id=rid, profile_name="p", project_id="proj", phase="watching", reason=None,
                  session_id="s", step_index=1, started_at="t", launched_at="t", ended_at=None,
                  owned_pgids=[child.pid], evidence_dir=str(store.evidence_dir(rid)))
    store.save(st)
    done = store.new_run_id()
    store.save(RunState(run_id=done, profile_name="p", project_id=None, phase="stopped", reason="x",
                        session_id="s", step_index=0, started_at="t", launched_at=None, ended_at="t"))

    lost = recover_on_boot(store=store, load_profile=lambda path: _prof(),
                           run_action=lambda a, ctx, timeout_s: StepResult(True, "t", "t"),
                           run_check=lambda c, ctx, step_start: True)
    assert lost == [rid]
    assert _reaped(child)
    after = store.load(rid)
    assert after.phase == "lost_on_restart" and after.literals == {"logoff_verified": True}
    assert after.reason == "sidecar_restart" and after.ended_at
    assert after.owned_pgids == []
    kinds = [e["kind"] for e in store.events(rid)]
    assert "teardown_step" in kinds and "literals" in kinds
    assert store.load(done).phase == "stopped"
    assert store.events(done) == []


def test_recovery_with_invalid_profile_still_kills() -> None:
    store = RunStore()
    child = _sleeper()
    rid = store.new_run_id()
    store.save(RunState(run_id=rid, profile_name="gone", project_id=None, phase="launching",
                        reason=None, session_id="s", step_index=0, started_at="t",
                        launched_at=None, ended_at=None, owned_pgids=[child.pid]))

    def bad(path):
        raise P.ProfileError("profile_outside_dir")

    assert recover_on_boot(store=store, load_profile=bad) == [rid]
    assert _reaped(child)
    after = store.load(rid)
    assert after.phase == "lost_on_restart"
    lits = [e for e in store.events(rid) if e["kind"] == "literals"][-1]
    assert lits["detail"]["logoff_verified"] == "ABSENT"


def test_recovery_touches_the_ledger_not_at_all() -> None:
    """No launch row (a restart storm must not spend the profile's budget) and
    no OUTCOME row either: a lost run is a run we know nothing about, and either
    verdict would corrupt the consecutive-failure streak — `failed=False` would
    launder a real streak clean, `failed=True` would blame the profile for our
    own restart. `LaunchLedger.check` skips outcome-less launches, so saying
    nothing leaves the streak exactly as the lost run found it."""
    store, ledger = RunStore(), LaunchLedger()
    prev = store.new_run_id()
    ledger.record("p", prev, 1.0)
    ledger.record_outcome(prev, failed=True)
    rid = store.new_run_id()
    store.save(RunState(run_id=rid, profile_name="p", project_id=None, phase="watching", reason=None,
                        session_id="s", step_index=0, started_at="t", launched_at="t", ended_at=None))
    recover_on_boot(store=store, load_profile=lambda path: _prof(),
                    run_action=lambda a, ctx, timeout_s: StepResult(True, "t", "t"),
                    run_check=lambda c, ctx, step_start: True)
    rows = ledger._rows()
    assert not [r for r in rows if r.get("kind") == "launch" and r["run_id"] == rid]
    assert not [r for r in rows if r.get("kind") == "outcome" and r["run_id"] == rid]
    # the pre-existing failed cycle is still the streak the next start will see
    assert [r for r in rows if r.get("kind") == "outcome"] == [
        {"kind": "outcome", "run_id": prev, "failed": True}]


def test_recovery_signals_owned_remote_pidfiles_term_then_kill() -> None:
    store = RunStore()
    rid = store.new_run_id()
    store.save(RunState(run_id=rid, profile_name="p", project_id=None, phase="watching", reason=None,
                        session_id="s", step_index=0, started_at="t", launched_at="t", ended_at=None,
                        owned_remote_pidfiles=[{"host": "box", "pidfile": "~/brain.pid"}]))
    seen: list[P.Action] = []

    def action(a, ctx, timeout_s):
        seen.append(a)
        return StepResult(True, "t", "t")

    recover_on_boot(store=store, load_profile=lambda path: _prof(), run_action=action,
                    run_check=lambda c, ctx, step_start: True)
    sig = [a for a in seen if a.kind == "remote_signal"]
    assert len(sig) == 1
    assert sig[0].params == {"host": "box", "pidfile": "~/brain.pid", "signal": "TERM",
                             "grace_s": 10, "then": "KILL"}
    assert store.load(rid).owned_remote_pidfiles == []


def test_recovery_closes_owned_tunnels() -> None:
    store = RunStore()
    host = P.Host("box")
    tdef = P.TunnelDef("t1", "box", ((1, 2),))
    prof = P.Profile("p", {"box": host}, {"t1": tdef}, (), (), (),
                     (P.Step("logoff", None, P.Check("file_exists", "/"), 1, "logoff_verified"),),
                     P.DEFAULT_CAPS, ())
    closed: list[Any] = []

    class FakeTunnels:
        def close(self, spec) -> bool:
            closed.append(spec)
            return True

    rid = store.new_run_id()
    store.save(RunState(run_id=rid, profile_name="p", project_id=None, phase="watching", reason=None,
                        session_id="s", step_index=0, started_at="t", launched_at="t", ended_at=None,
                        owned_tunnels=["t1"]))
    recover_on_boot(store=store, tunnels=FakeTunnels(), remote=object(),
                    load_profile=lambda path: prof,
                    run_action=lambda a, ctx, timeout_s: StepResult(True, "t", "t"),
                    run_check=lambda c, ctx, step_start: True)
    assert len(closed) == 1 and closed[0].reverse_forwards == ((1, 2),)
    assert store.load(rid).owned_tunnels == []


def test_ban_signal_during_recovery_still_leaves_the_human_gate() -> None:
    """A ban surfaced by the recovery teardown must arm the paused marker, even
    though the run itself is recorded as `lost_on_restart` (never resumed)."""
    store = RunStore()
    prof = P.Profile("p", {}, {}, (), (),
                     (P.Step("ev", P.Action("local", {"argv": ("/bin/true",), "cwd": None}), None, 1),),
                     (P.Step("logoff", None, P.Check("file_exists", "/"), 1, "logoff_verified"),),
                     P.DEFAULT_CAPS, ("Account is banned",))
    rid = store.new_run_id()
    store.save(RunState(run_id=rid, profile_name="p", project_id=None, phase="watching", reason=None,
                        session_id="s", step_index=0, started_at="t", launched_at="t", ended_at=None))
    recover_on_boot(store=store, load_profile=lambda path: prof,
                    run_action=lambda a, ctx, timeout_s: StepResult(
                        True, "t", "t", stdout_tail="Account is banned"),
                    run_check=lambda c, ctx, step_start: True)
    assert store.load(rid).phase == "lost_on_restart"
    assert any(e["kind"] == "ban_signal" for e in store.events(rid))
    assert paused_marker("p").exists()


def test_recovery_with_no_runs_is_a_no_op() -> None:
    assert recover_on_boot(store=RunStore(), load_profile=lambda path: _prof()) == []


# --- fix round 1 ----------------------------------------------------------- #

def test_unsignalled_remote_pidfile_stays_owned_and_is_reported() -> None:
    """An unreachable host means a remote process still running against a tunnel
    that no longer exists. Forgetting its pidfile erases the only record of it."""
    store = RunStore()
    rid = store.new_run_id()
    store.save(RunState(run_id=rid, profile_name="p", project_id=None, phase="watching", reason=None,
                        session_id="s", step_index=0, started_at="t", launched_at="t", ended_at=None,
                        owned_remote_pidfiles=[{"host": "box", "pidfile": "~/brain.pid"}]))

    def action(a, ctx, timeout_s):
        if a.kind == "remote_signal":
            return StepResult(False, "t", "t", detail="ssh_unreachable")
        return StepResult(True, "t", "t")

    recover_on_boot(store=store, load_profile=lambda path: _prof(), run_action=action,
                    run_check=lambda c, ctx, step_start: True)
    after = store.load(rid)
    assert after.owned_remote_pidfiles == [{"host": "box", "pidfile": "~/brain.pid"}]
    ev = [e["detail"] for e in store.events(rid) if e["kind"] == "recovery"
          and "remote_pidfile" in e["detail"]]
    assert ev and ev[-1]["ok"] is False and ev[-1]["result"] == "FAILED"


def test_a_raising_remote_signal_also_keeps_the_pidfile() -> None:
    store = RunStore()
    rid = store.new_run_id()
    store.save(RunState(run_id=rid, profile_name="p", project_id=None, phase="watching", reason=None,
                        session_id="s", step_index=0, started_at="t", launched_at="t", ended_at=None,
                        owned_remote_pidfiles=[{"host": "box", "pidfile": "~/brain.pid"}]))

    def action(a, ctx, timeout_s):
        if a.kind == "remote_signal":
            raise OSError("no route to host")
        return StepResult(True, "t", "t")

    assert recover_on_boot(store=store, load_profile=lambda path: _prof(), run_action=action,
                           run_check=lambda c, ctx, step_start: True) == [rid]
    after = store.load(rid)
    assert after.owned_remote_pidfiles and after.phase == "lost_on_restart"


def test_tunnel_whose_spec_cannot_be_rebuilt_stays_owned() -> None:
    """The invalid-profile path synthesizes an EMPTY profile, so there is no
    `TunnelDef` to build a spec from — nothing is closed, so nothing may be
    forgotten. (Reconstructing the ssh child from persisted state alone is out
    of scope here: `errorta_tunnels` keeps its registry in memory only.)"""
    store = RunStore()
    closed: list[Any] = []

    class FakeTunnels:
        def close(self, spec) -> bool:
            closed.append(spec)
            return True

    rid = store.new_run_id()
    store.save(RunState(run_id=rid, profile_name="gone", project_id=None, phase="watching",
                        reason=None, session_id="s", step_index=0, started_at="t",
                        launched_at="t", ended_at=None, owned_tunnels=["t1"]))

    def bad(path):
        raise P.ProfileError("profile_outside_dir")

    recover_on_boot(store=store, tunnels=FakeTunnels(), remote=object(), load_profile=bad)
    assert closed == []
    after = store.load(rid)
    assert after.owned_tunnels == ["t1"] and after.phase == "lost_on_restart"
    ev = [e["detail"] for e in store.events(rid) if e["kind"] == "recovery"
          and e["detail"].get("tunnel") == "t1"]
    assert ev and ev[-1]["tunnel_close"] == "ABSENT" and ev[-1]["reason"] == "spec_unavailable"


def test_a_recycled_pgid_is_not_killed() -> None:
    """A pgid read back from `state.json` names a number, not a process. If it
    predates the run it cannot be a child the run spawned — the pid was recycled
    while the sidecar was down, and SIGKILLing it would hit a stranger."""
    store = RunStore()
    stranger = _sleeper()
    try:
        started = (datetime.now(timezone.utc) + timedelta(seconds=60)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        rid = store.new_run_id()
        store.save(RunState(run_id=rid, profile_name="p", project_id=None, phase="watching",
                            reason=None, session_id="s", step_index=0, started_at=started,
                            launched_at=started, ended_at=None, owned_pgids=[stranger.pid]))
        recover_on_boot(store=store, load_profile=lambda path: _prof(),
                        run_action=lambda a, ctx, timeout_s: StepResult(True, "t", "t"),
                        run_check=lambda c, ctx, step_start: True)
        time.sleep(0.3)
        assert stranger.poll() is None                 # the stranger is still alive
        ev = [e["detail"] for e in store.events(rid) if e["kind"] == "recovery"
              and "pgid" in e["detail"]]
        assert ev and ev[-1]["result"] == "SKIPPED_NOT_OURS"
        assert store.load(rid).phase == "lost_on_restart"
    finally:
        stranger.kill()
        stranger.wait()


def test_a_pgid_the_run_really_spawned_is_killed() -> None:
    """The guard's other half: a live process owned by us and created after the
    run began is exactly what recovery is for."""
    store = RunStore()
    started = (datetime.now(timezone.utc) - timedelta(seconds=60)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    child = _sleeper()
    rid = store.new_run_id()
    store.save(RunState(run_id=rid, profile_name="p", project_id=None, phase="watching",
                        reason=None, session_id="s", step_index=0, started_at=started,
                        launched_at=started, ended_at=None, owned_pgids=[child.pid]))
    recover_on_boot(store=store, load_profile=lambda path: _prof(),
                    run_action=lambda a, ctx, timeout_s: StepResult(True, "t", "t"),
                    run_check=lambda c, ctx, step_start: True)
    assert _reaped(child)
    ev = [e["detail"] for e in store.events(rid) if e["kind"] == "recovery"
          and "pgid" in e["detail"]]
    assert ev and ev[-1]["result"] == "KILLED"
    assert store.load(rid).owned_pgids == []
