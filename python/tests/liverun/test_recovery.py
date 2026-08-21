# python/tests/liverun/test_recovery.py
from __future__ import annotations

import subprocess
import time
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


def test_recovery_is_never_a_launch_but_records_the_outcome() -> None:
    store, ledger = RunStore(), LaunchLedger()
    rid = store.new_run_id()
    store.save(RunState(run_id=rid, profile_name="p", project_id=None, phase="watching", reason=None,
                        session_id="s", step_index=0, started_at="t", launched_at="t", ended_at=None))
    recover_on_boot(store=store, load_profile=lambda path: _prof(),
                    run_action=lambda a, ctx, timeout_s: StepResult(True, "t", "t"),
                    run_check=lambda c, ctx, step_start: True)
    rows = ledger._rows()
    assert not [r for r in rows if r.get("kind") == "launch"]
    assert [r for r in rows if r.get("kind") == "outcome" and r["run_id"] == rid]


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
        def close(self, spec) -> None:
            closed.append(spec)

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
