"""F146 Slice C — runtime launch evidence for the delivery review.

Slice B verifies the integrated delivered head with a reviewer + the registered
test suite. Slice C adds the crash catcher the per-PR reviews + unit tests miss:
for a runnable ``managed_local`` runtime, the delivery review LAUNCHES the
delivered program headless + bounded and requires it to get past startup without
a traceback. A runnable project is not truly ``done`` until it launches cleanly
(the ``pygame.font`` case). Real launch of the exact delivered head — never a
faked probe; fail-closed on a crash / an inability to launch.

These tests spawn REAL short-lived children (plain ``python -c`` programs — NOT a
GUI) through the F039 sandbox, exactly as the F101 runtime-process suite does.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from errorta_council.coding import runtime_process as rp
from errorta_council.coding.evidence import merge_review
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.runner import (
    build_run_turn,
    members_by_coding_role,
)
from errorta_council.coding.runtime import (
    RuntimeProfileStore,
    validate_profile,
)
from errorta_council.coding.runtime_process import RuntimeProcessManager
from errorta_council.coding.workspace import CodingWorkspace

MEMBERS = [
    {"id": "m-pm", "enabled": True, "metadata": {"coding_role": "pm"}},
    {"id": "m-dev", "enabled": True, "metadata": {"coding_role": "dev"}},
    {"id": "m-rev", "enabled": True, "metadata": {"coding_role": "reviewer"}},
]

# start argvs for the runtime profile — self-contained (no repo entrypoint file):
_CLEAN_EXIT = ["python", "-c", "print('booted ok')"]
_CRASH = ["python", "-c",
          "raise RuntimeError('startup boom: pygame.font not available')"]
_LONG_LIVED = ["python", "-c", "import time; print('serving'); time.sleep(30)"]


@pytest.fixture(autouse=True)
def _fast_and_clean(monkeypatch):
    # Keep the probe fast + never leak a child across tests.
    monkeypatch.setattr(rp, "_POLL_INTERVAL", 0.05)
    monkeypatch.setattr(rp, "_GRACE_SECONDS", 1.0)
    monkeypatch.setattr(rp, "_LAUNCH_PROBE_SECONDS", 1.0)
    yield
    rp.teardown_all()


def _delivery_head(prompt: str) -> str:
    return re.search(r"delivered head you are reviewing is '([^']*)'", prompt).group(1)


def _rev_env(head: str, *, approved: bool = True) -> str:
    return json.dumps({
        "schema_version": "coding_turn.v1", "role": "reviewer",
        "task_id": "delivery-review",
        "intent": {"kind": "review_verdict", "reviewed_head": head,
                   "approved": approved, "findings": []}})


def _approve_caller(member: dict, prompt: str) -> str:
    # The DELIVERY reviewer always approves so the verdict hinges on the launch.
    return _rev_env(_delivery_head(prompt), approved=True)


def _make(pid: str, *, start_argv=None, sandbox: str = "auto",
          runtime_mode: str = "managed_local", kind: str = "cli"):
    """A populated, committed workspace + (optionally) a runtime profile. No test
    commands, so the delivery verdict rests on the reviewer (auto-approve) + the
    launch probe alone."""
    store = LedgerStore(pid)
    store.create_project(north_star="a runnable app", definition_of_done="it runs",
                         target="new", repo_path=None)
    ws = CodingWorkspace(pid, store)
    ws.setup(target="new", repo_path=None)
    ws.write_file("app.py", "print('hi')\n", task_id="seed")
    if start_argv is not None:
        rstore = RuntimeProfileStore.for_ledger(store)
        rstore.upsert_profile(validate_profile(
            {"kind": kind, "runtime_mode": runtime_mode, "start": start_argv,
             "sandbox": sandbox},
            profile_id="default", project_id=pid))
    return store, ws


def _run_delivery_review(store, ws, caller=_approve_caller):
    rt = build_run_turn(store, ws, members_by_coding_role(MEMBERS), caller,
                        guardrail_enabled=True)
    return rt.delivery_review(store)


def _launch_runs(store) -> list[dict]:
    rstore = RuntimeProfileStore.for_ledger(store)
    return [r for r in rstore.list_runtime_tests() if r.get("kind") == "launch"]


# --- acceptance #3: a startup crash blocks done -------------------------------

def test_launch_crash_blocks_done_and_files_finding(tmp_errorta_home: Path) -> None:
    store, ws = _make("f146c-crash", start_argv=_CRASH)
    head = ws.head()
    result = _run_delivery_review(store, ws)

    # The crash blocks done and files a dev finding so the run re-opens (Slice E).
    assert result.passed is False
    assert result.reason == "rejected"
    assert result.filed_findings is True
    titles = [t.title for t in store.list_tasks()]
    assert "fix runtime launch crash" in titles, titles

    # Launch evidence is recorded against the exact delivered head, passed=False.
    runs = _launch_runs(store)
    assert runs and all(r.get("head") == head for r in runs)
    assert not any(r.get("passed") for r in runs)

    # A crash is a real verdict -> cached (passed=False) so it isn't re-run at the
    # same head until the team fixes it and the head changes.
    rs = store.get_run_state()
    assert rs.get("delivery_reviewed_head") == head
    assert rs.get("delivery_review_passed") is False


# --- acceptance #1/#3: a clean launch lets done proceed -----------------------

def test_launch_clean_allows_done(tmp_errorta_home: Path) -> None:
    store, ws = _make("f146c-clean", start_argv=_CLEAN_EXIT)
    head = ws.head()
    result = _run_delivery_review(store, ws)

    assert result.passed is True
    assert result.reason == "reviewed"
    assert not any(t.title == "fix runtime launch crash" for t in store.list_tasks())

    runs = _launch_runs(store)
    assert runs and any(r.get("passed") and r.get("head") == head for r in runs)
    rs = store.get_run_state()
    assert rs.get("delivery_review_passed") is True


def test_runnable_test_less_project_gate_clean_after_clean_launch(
    tmp_errorta_home: Path,
) -> None:
    # Acceptance #1 for a runnable, TEST-LESS project (a game/app with no unit
    # suite — the pygame case): Slice D makes the runtime count as tests_required,
    # and a clean delivery LAUNCH (Slice C) must SATISFY it — else the accept gate
    # would show tests_missing forever. No registered test commands here.
    store, ws = _make("f146c-gate", start_argv=_CLEAN_EXIT)
    result = _run_delivery_review(store, ws)
    assert result.passed is True
    # Mark done (the delivery review authorized it) so the gate's dod check passes.
    store.set_project_status("done")
    codes = {b["code"] for b in merge_review(store, ws)["gate"]["blockers"]}
    assert "tests_missing" not in codes, codes


def test_launch_survives_startup_window_is_clean(tmp_errorta_home: Path) -> None:
    # A long-lived server/desktop that keeps running past the startup window is a
    # CLEAN launch (surviving the window IS the intended state).
    store, ws = _make("f146c-server", start_argv=_LONG_LIVED, kind="web")
    result = _run_delivery_review(store, ws)
    assert result.passed is True
    runs = _launch_runs(store)
    assert runs and any(r.get("passed") for r in runs)
    assert "survived" in runs[-1].get("detail", "")


# --- non-runnable: probe skipped, unchanged behavior --------------------------

def test_non_runnable_skips_launch(tmp_errorta_home: Path) -> None:
    # No runtime profile at all -> the launch probe is skipped (vacuously clean);
    # the delivery review passes on the reviewer alone, exactly as pre-Slice-C.
    store, ws = _make("f146c-norun", start_argv=None)
    result = _run_delivery_review(store, ws)
    assert result.passed is True
    assert result.reason == "reviewed"
    assert _launch_runs(store) == []  # nothing was launched


def test_container_runtime_skips_launch(tmp_errorta_home: Path) -> None:
    # A non-managed_local (container) runtime is out of Slice C scope -> skipped,
    # not misreported as a crash. (Reviewer alone gates.)
    store, ws = _make("f146c-container", start_argv=["run", "app"],
                      runtime_mode="container")
    result = _run_delivery_review(store, ws)
    assert result.passed is True
    assert _launch_runs(store) == []


# --- fail-closed: an inability to launch blocks done, retries -----------------

def test_launch_cannot_verify_blocks_done_without_finding(
    tmp_errorta_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An explicit-but-unavailable sandbox is an INABILITY to launch (not a code
    # defect): it must block done, file NO launch finding, and NOT cache a verdict
    # (so the next completion claim retries), never a false `done`.
    from errorta_tools.runner import sandbox as sbx
    monkeypatch.setattr(sbx, "is_available", lambda b: False)

    store, ws = _make("f146c-cannot", start_argv=_CLEAN_EXIT, sandbox="docker")
    head = ws.head()
    result = _run_delivery_review(store, ws)

    assert result.passed is False
    assert result.reason == "launch_cannot_verify"
    # No launch-crash finding (environmental, not a delivered-code defect).
    assert not any(t.title == "fix runtime launch crash" for t in store.list_tasks())
    # No clean evidence recorded; the verdict is NOT cached at this head -> retries.
    assert not any(r.get("passed") for r in _launch_runs(store))
    rs = store.get_run_state()
    assert rs.get("delivery_reviewed_head") != head
    assert rs.get("delivery_review_passed") is not True


# --- process-manager unit coverage: launch_probe classification ---------------

def _manager(pid: str, start_argv, *, sandbox="auto",
             kind="cli") -> RuntimeProcessManager:
    store = LedgerStore(pid)
    store.create_project(north_star="n", definition_of_done="d",
                         target="new", repo_path=None)
    ws = CodingWorkspace(pid, store)
    ws.setup(target="new", repo_path=None)
    rstore = RuntimeProfileStore.for_ledger(store)
    rstore.upsert_profile(validate_profile(
        {"kind": kind, "runtime_mode": "managed_local", "start": start_argv,
         "sandbox": sandbox}, profile_id="default", project_id=pid))
    return RuntimeProcessManager.for_project(pid)


def test_launch_probe_clean(tmp_errorta_home: Path) -> None:
    mgr = _manager("f146c-probe-clean", _CLEAN_EXIT)
    res = mgr.launch_probe("default", head="deadbeef")
    assert res["status"] == "clean"


def test_launch_probe_crash(tmp_errorta_home: Path) -> None:
    mgr = _manager("f146c-probe-crash", _CRASH, kind="desktop")
    res = mgr.launch_probe("default", head="deadbeef")
    assert res["status"] == "crashed"
    # The captured detail carries the startup failure (stderr merged into the log).
    assert "startup boom" in res["detail"] or "Traceback" in res["detail"]


def test_launch_probe_cancel_is_cannot_verify(tmp_errorta_home: Path) -> None:
    mgr = _manager("f146c-probe-cancel", _LONG_LIVED)
    res = mgr.launch_probe("default", head="deadbeef", should_cancel=lambda: True)
    assert res["status"] == "cannot_verify"
    assert "cancel" in res["detail"].lower()


def test_launch_probe_cli_nonzero_no_traceback_is_clean(tmp_errorta_home: Path) -> None:
    # HIGH (adversarial review): a one-shot CLI that exits non-zero WITHOUT a
    # traceback (e.g. a validator/usage exit) ran to completion — it is NOT a
    # startup crash and must not be misreported (over-block) as one.
    mgr = _manager("f146c-cli-nz", ["python", "-c", "import sys; sys.exit(2)"],
                   kind="cli")
    res = mgr.launch_probe("default", head="deadbeef")
    assert res["status"] == "clean", res


def test_launch_probe_long_running_early_exit_is_crash(tmp_errorta_home: Path) -> None:
    # A window/server runtime that EXITS during the startup window (even a clean
    # non-zero, no traceback — e.g. a port-bind failure) failed to stay up -> crash.
    mgr = _manager("f146c-web-exit", ["python", "-c", "import sys; sys.exit(1)"],
                   kind="web")
    res = mgr.launch_probe("default", head="deadbeef")
    assert res["status"] == "crashed", res


def test_launch_probe_signal_death_is_crash(tmp_errorta_home: Path) -> None:
    # A child killed by a signal (segfault/abort) is an unambiguous crash even
    # with no Python traceback.
    mgr = _manager("f146c-signal",
                   ["python", "-c", "import os, signal; os.kill(os.getpid(), signal.SIGABRT)"],
                   kind="cli")
    res = mgr.launch_probe("default", head="deadbeef")
    assert res["status"] == "crashed", res
    assert "signal" in res["detail"].lower()
