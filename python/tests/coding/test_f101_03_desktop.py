"""F101-03 S2 — desktop/GUI modality: detector, sandbox display carve-out,
DesktopLauncher, screenshot evidence, and a live T1 seatbelt start.

The actual pixels of a GUI window can't be validated in a headless CI/dev venv
(no tkinter/pygame/display/Quartz here), so window+screenshot capture degrade to
an honest "no screenshot". What IS live-validated on macOS: a real process runs
under the seatbelt *display-allowed* profile (T1), stays alive, and tears down.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from errorta_council.coding import runtime_process as rp
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.runtime import RuntimeProfile, RuntimeProfileStore, detect
from errorta_council.coding.runtime_launchers import get_launcher
from errorta_council.coding.runtime_process import RuntimeProcessManager
from errorta_council.coding.runtime_resolve import resolve_launch_plan
from errorta_council.coding.workspace import CodingWorkspace
from errorta_tools.runner import sandbox
from errorta_tools.runner.preview import capture_app_window


@pytest.fixture(autouse=True)
def _fast_and_clean(monkeypatch):
    monkeypatch.setattr(rp, "_POLL_INTERVAL", 0.05)
    monkeypatch.setattr(rp, "_GRACE_SECONDS", 1.0)
    yield
    rp.teardown_all()


# --------------------------------------------------------------------------- #
# Detector
# --------------------------------------------------------------------------- #
def test_detects_pygame_game_as_desktop(tmp_path: Path):
    (tmp_path / "game.py").write_text("import pygame\npygame.init()\n")
    props = detect(tmp_path, project_id="poke")
    assert props and props[0].kind == "desktop"
    assert props[0].start == ["python", "game.py"]
    assert props[0].demo.get("toolkit") == "pygame"


def test_detects_tkinter_main_as_desktop(tmp_path: Path):
    (tmp_path / "main.py").write_text("from tkinter import Tk\nTk().mainloop()\n")
    props = detect(tmp_path, project_id="t")
    assert props[0].kind == "desktop" and props[0].demo.get("toolkit") == "tkinter"


def test_bare_main_guarded_script_is_cli_not_desktop(tmp_path: Path):
    (tmp_path / "tool.py").write_text(
        "def go():\n    print('hi')\n\nif __name__ == '__main__':\n    go()\n")
    props = detect(tmp_path, project_id="t")
    assert props and props[0].kind == "cli"
    assert props[0].start == ["python", "tool.py"]


def test_desktop_wins_over_generic_python(tmp_path: Path):
    # A GUI file present alongside plain packaging: desktop is proposed first.
    (tmp_path / "game.py").write_text("import pyglet\n")
    (tmp_path / "requirements.txt").write_text("pyglet\n")
    props = detect(tmp_path, project_id="t")
    assert props[0].kind == "desktop"
    assert props[0].setup == [["pip", "install", "-r", "requirements.txt"]]


# --------------------------------------------------------------------------- #
# Resolver mapping
# --------------------------------------------------------------------------- #
def test_resolver_maps_desktop_to_t1(tmp_path: Path):
    import threading
    (tmp_path / "game.py").write_text("import pygame\n")
    ledger = tmp_path / "ledger"
    ledger.mkdir()
    rstore = RuntimeProfileStore(ledger, threading.Lock())

    plan = resolve_launch_plan(tmp_path, "h", rstore, "poke")

    from errorta_council.coding.runtime_resolve import LaunchPlan
    assert isinstance(plan, LaunchPlan)
    assert plan.modality == "desktop"
    assert plan.trust_tier == 1  # T1 sandboxed-windowed
    assert "game.py" in plan.verified_paths


# --------------------------------------------------------------------------- #
# Sandbox display carve-out — keeps net + write denials at T1.
# --------------------------------------------------------------------------- #
def test_seatbelt_display_allows_windowserver_keeps_denials(tmp_path: Path):
    prof = sandbox._seatbelt_profile(
        writable=[tmp_path], network_allowed=False, display_allowed=True)
    assert "windowserver" in prof
    assert "(deny network*)" in prof       # T1 still denies network
    assert "(deny file-write*)" in prof    # T1 still confines writes


def test_seatbelt_no_display_has_no_windowserver(tmp_path: Path):
    prof = sandbox._seatbelt_profile(
        writable=[tmp_path], network_allowed=False, display_allowed=False)
    assert "windowserver" not in prof


def test_bwrap_display_binds_x11_socket_and_keeps_no_net(tmp_path: Path, monkeypatch):
    x11 = tmp_path / "x11"
    x11.mkdir()
    monkeypatch.setattr(sandbox, "_x11_socket_dir", lambda: x11)
    argv = sandbox._bwrap_argv(
        writable=[tmp_path], workspace=tmp_path, network_allowed=False,
        base=["python", "game.py"], display_allowed=True)
    assert "--unshare-net" in argv          # T1 still has no network
    assert "--ro-bind" in argv and str(x11) in argv


# --------------------------------------------------------------------------- #
# Launcher registration + screenshot best-effort
# --------------------------------------------------------------------------- #
def test_desktop_launcher_registered():
    launcher = get_launcher("desktop")
    assert launcher is not None and launcher.modality == "desktop"


def test_capture_app_window_is_honest_no_screenshot(tmp_path: Path):
    # No such pid / no Quartz here -> False, never a raise.
    assert capture_app_window(pids={2**30}, out_path=tmp_path / "x.png") is False


# --------------------------------------------------------------------------- #
# Live: a real process under the seatbelt display (T1) profile stays alive.
# --------------------------------------------------------------------------- #
def _desktop_manager(project_id: str) -> tuple[RuntimeProcessManager, Path]:
    store = LedgerStore(project_id)
    store.create_project(north_star="n", definition_of_done="d",
                         target="new", repo_path=None)
    ws = CodingWorkspace(project_id, store)
    ws.setup(target="new", repo_path=None)
    root = ws.root()
    # A GUI-shaped stand-in: a long-lived process (no real toolkit available in
    # this venv). Proves the T1 windowed-start machinery + liveness + teardown.
    (root / "game.py").write_text("import time\ntime.sleep(30)\n")
    return RuntimeProcessManager.for_project(project_id), root


@pytest.mark.skipif(
    not sandbox.is_available(sandbox.SANDBOX_SEATBELT),
    reason="seatbelt sandbox not available on this host",
)
def test_desktop_start_runs_under_t1_and_tears_down(tmp_errorta_home: Path):
    mgr, root = _desktop_manager("deskrun")
    rstore = mgr.rstore
    rstore.upsert_profile(RuntimeProfile(
        profile_id="default", project_id="deskrun", kind="desktop",
        runtime_mode="managed_local", start=["python", "game.py"],
        health={"type": "none"}))

    launcher = get_launcher("desktop")
    plan = resolve_launch_plan(root, "h", rstore, "deskrun")
    session = launcher.launch(mgr, plan)

    # It comes up under a real OS sandbox (T1), not reduced isolation.
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        s = mgr.get_session(session.session_id)
        if s and s.state in ("running", "healthy", "crashed"):
            break
        time.sleep(0.05)
    s = mgr.get_session(session.session_id)
    assert s is not None and s.state == "running"
    assert s.sandbox_backend == "seatbelt"
    assert s.to_dict().get("trust_tier") == 1
    assert s.to_dict().get("display") is True
    # Screenshot degrades honestly to None (no window / no Quartz here).
    assert mgr.capture_screenshot(session.session_id) is None
    mgr.stop("default")


def test_launch_evidence_records_liveness(tmp_errorta_home: Path):
    from errorta_council.coding.testing import run_runtime_test
    if not sandbox.is_available(sandbox.SANDBOX_SEATBELT):
        pytest.skip("seatbelt not available")
    mgr, root = _desktop_manager("deskev")
    mgr.rstore.upsert_profile(RuntimeProfile(
        profile_id="default", project_id="deskev", kind="desktop",
        runtime_mode="managed_local", start=["python", "game.py"],
        health={"type": "none"}))

    result = run_runtime_test(mgr, "default", "launch", head="h")

    assert result.kind == "launch"
    assert result.passed is True          # alive through the liveness window
    assert "no screenshot" in result.detail
