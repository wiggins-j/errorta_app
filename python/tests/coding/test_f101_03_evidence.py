"""F101-03 S7 — launch evidence unification + the screenshot demo asset.

The `launch` verdict is head-bound and flows through the same
`latest_runtime_evidence` projection F093 reads (one verdict across modalities);
a captured window screenshot is stamped on the session as the demo asset.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from errorta_council.coding import runtime_process as rp
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.runtime import RuntimeProfile, latest_runtime_evidence
from errorta_council.coding.runtime_process import RuntimeProcessManager
from errorta_council.coding.testing import run_runtime_test
from errorta_council.coding.workspace import CodingWorkspace
from errorta_tools.runner import sandbox


@pytest.fixture(autouse=True)
def _fast_and_clean(monkeypatch):
    monkeypatch.setattr(rp, "_POLL_INTERVAL", 0.05)
    monkeypatch.setattr(rp, "_GRACE_SECONDS", 1.0)
    yield
    rp.teardown_all()


def _desktop_manager(project_id: str) -> RuntimeProcessManager:
    store = LedgerStore(project_id)
    store.create_project(north_star="n", definition_of_done="d",
                         target="new", repo_path=None)
    ws = CodingWorkspace(project_id, store)
    ws.setup(target="new", repo_path=None)
    (ws.root() / "game.py").write_text("import time\ntime.sleep(30)\n")
    mgr = RuntimeProcessManager.for_project(project_id)
    mgr.rstore.upsert_profile(RuntimeProfile(
        profile_id="default", project_id=project_id, kind="desktop",
        runtime_mode="managed_local", start=["python", "game.py"],
        health={"type": "none"}))
    return mgr


@pytest.mark.skipif(
    not sandbox.is_available(sandbox.SANDBOX_SEATBELT),
    reason="seatbelt not available")
def test_launch_verdict_is_head_bound_and_in_evidence(tmp_errorta_home: Path):
    mgr = _desktop_manager("ev1")
    result = run_runtime_test(mgr, "default", "launch", head="HEAD1")
    mgr.rstore.record_runtime_test(
        kind=result.kind, profile_id=result.profile_id,
        session_id=result.session_id, passed=result.passed, head="HEAD1",
        detail=result.detail)

    evidence = latest_runtime_evidence(mgr.rstore, current_head="HEAD1")
    launch = [r for r in evidence["results"] if r["kind"] == "launch"]
    assert launch and launch[0]["fresh"] is True   # passed AND at current head
    assert evidence["any_fresh_pass"] is True

    # A pass against an old head is surfaced but not fresh (staleness discipline).
    stale = latest_runtime_evidence(mgr.rstore, current_head="HEAD2")
    assert [r for r in stale["results"] if r["kind"] == "launch"][0]["fresh"] is False


@pytest.mark.skipif(
    not sandbox.is_available(sandbox.SANDBOX_SEATBELT),
    reason="seatbelt not available")
def test_screenshot_capture_stamps_session(tmp_errorta_home: Path, monkeypatch):
    # Simulate a successful window capture (no real display in this venv).
    def fake_capture(*, pids, out_path):
        Path(out_path).write_bytes(b"\x89PNG\r\n\x1a\n")
        return True

    monkeypatch.setattr(
        "errorta_tools.runner.preview.capture_app_window", fake_capture)

    mgr = _desktop_manager("ev2")
    session = mgr.start("default")
    sid = session.session_id
    import time
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        s = mgr.get_session(sid)
        if s and s.state == "running":
            break
        time.sleep(0.05)

    ref = mgr.capture_screenshot(sid)
    assert ref == f"runtime-shots/{sid}.png"
    # The demo asset is discoverable on the session record (F093 / export read it).
    assert mgr.get_session(sid).to_dict().get("screenshot_ref") == ref
    mgr.stop("default")


def test_no_display_stamps_nothing(tmp_errorta_home: Path):
    store = LedgerStore("ev3")
    store.create_project(north_star="n", definition_of_done="d",
                         target="new", repo_path=None)
    # A session id that isn't live -> capture returns None, no stamp, no raise.
    ws = CodingWorkspace("ev3", store)
    ws.setup(target="new", repo_path=None)
    mgr = RuntimeProcessManager.for_project("ev3")
    assert mgr.capture_screenshot("rs-nope") is None
