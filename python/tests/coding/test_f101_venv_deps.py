"""F101-03 — managed-Python dependency install for Preview.

Two halves of the same fix for "generated app fails at Run with
ModuleNotFoundError because its deps were never installed":

1. Detector grounds a ``pip install`` on the code's ACTUAL imports (so a bare
   ``game.py`` that ``import pygame`` gets a real setup step even with no
   requirements.txt), while refusing to guess a package for an unknown import.
2. Process manager stands up a per-project venv so that install has a stable,
   sandbox-writable target which the later ``start`` shares — otherwise the
   install has nowhere to land and ``start`` couldn't import it anyway.

The venv-preparation tests are pure (no real venv / no network): they assert the
argv/step shaping, not a live pip run.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from errorta_council.coding import runtime_process as rp
from errorta_council.coding.runtime import (
    RuntimeProfileStore,
    detect,
    validate_profile,
)
from errorta_council.coding.runtime_process import (
    RuntimeProcessManager,
    _is_pip_install_step,
    _rewrite_argv_to_venv,
    _venv_python,
)
from errorta_tools.runner.sandbox import (
    SANDBOX_BWRAP,
    SANDBOX_SEATBELT,
    is_available,
)


def _real_backend() -> str | None:
    for backend in (SANDBOX_SEATBELT, SANDBOX_BWRAP):
        if is_available(backend):
            return backend
    return None


# --------------------------------------------------------------------------- #
# Detector: dependency install grounded on real imports
# --------------------------------------------------------------------------- #
def test_bare_pygame_game_gets_grounded_pip_install(tmp_path: Path):
    # The reported case: a lone game.py importing pygame, no requirements.txt.
    (tmp_path / "game.py").write_text("import pygame\npygame.init()\n")
    props = detect(tmp_path, project_id="poke")
    assert props and props[0].kind == "desktop"
    assert props[0].setup == [["pip", "install", "pygame"]]


def test_requirements_txt_wins_over_import_scan(tmp_path: Path):
    # A declared manifest may pin versions — it beats the import scan.
    (tmp_path / "game.py").write_text("import pygame\n")
    (tmp_path / "requirements.txt").write_text("pygame==2.5\n")
    props = detect(tmp_path, project_id="poke")
    assert props[0].setup == [["pip", "install", "-r", "requirements.txt"]]


def test_unknown_import_is_not_guessed(tmp_path: Path):
    # Grounded-or-refuse: an unrecognized third-party import must NOT become a
    # guessed `pip install frobnicate` that would just fail.
    (tmp_path / "main.py").write_text(
        "import frobnicate\nif __name__ == '__main__':\n    frobnicate.go()\n")
    props = detect(tmp_path, project_id="t")
    assert props and props[0].kind == "cli"
    assert props[0].setup == []


def test_stdlib_only_script_needs_no_install(tmp_path: Path):
    (tmp_path / "tool.py").write_text(
        "import json, sys\nif __name__ == '__main__':\n    print(json.dumps([]))\n")
    props = detect(tmp_path, project_id="t")
    assert props and props[0].kind == "cli"
    assert props[0].setup == []


def test_test_file_imports_are_not_runtime_deps(tmp_path: Path):
    # A dev-only `import pytest` in a test file must not become a runtime dep;
    # only the app file's pygame is installed.
    (tmp_path / "game.py").write_text("import pygame\n")
    (tmp_path / "test_game.py").write_text("import pytest\nimport pygame\n")
    props = detect(tmp_path, project_id="t")
    assert props[0].setup == [["pip", "install", "pygame"]]


def test_import_alias_maps_to_pypi_name(tmp_path: Path):
    # import name != package name (cv2 -> opencv-python, PIL -> Pillow).
    (tmp_path / "app.py").write_text("import cv2\nfrom PIL import Image\n")
    props = detect(tmp_path, project_id="t")
    pkgs = props[0].setup[0][2:]  # after ["pip", "install", ...]
    assert set(pkgs) == {"opencv-python", "Pillow"}


def test_multiple_recognized_imports_dedupe_into_one_step(tmp_path: Path):
    (tmp_path / "main.py").write_text("import requests\nimport requests as r2\n")
    props = detect(tmp_path, project_id="t")
    assert props[0].setup == [["pip", "install", "requests"]]


# --------------------------------------------------------------------------- #
# Venv preparation (pure: argv/step shaping, no live venv)
# --------------------------------------------------------------------------- #
def _manager(work_root: Path) -> RuntimeProcessManager:
    ws = work_root / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    rstore = RuntimeProfileStore(work_root / "ledger", threading.Lock())
    return RuntimeProcessManager(
        project_id="p", rstore=rstore, workspace_root=ws, work_root=work_root)


def _profile(setup, start) -> "rp.RuntimeProfile":
    return validate_profile(
        {"kind": "desktop", "runtime_mode": "managed_local",
         "start": start, "setup": setup, "health": {"type": "none"},
         "sandbox": "auto"},
        profile_id="default", project_id="p")


def test_is_pip_install_step_detection():
    assert _is_pip_install_step(["pip", "install", "-r", "requirements.txt"])
    assert _is_pip_install_step(["pip", "install", "pygame"])
    assert _is_pip_install_step(["python", "-m", "pip", "install", "x"])
    assert not _is_pip_install_step(["python", "-c", "print('installing')"])
    assert not _is_pip_install_step(["npm", "install"])
    assert not _is_pip_install_step([])


def test_rewrite_argv_to_venv():
    vpy = Path("/v/bin/python")
    assert _rewrite_argv_to_venv(["python", "game.py"], vpy) == [
        "/v/bin/python", "game.py"]
    assert _rewrite_argv_to_venv(["pip", "install", "pygame"], vpy) == [
        "/v/bin/python", "-m", "pip", "install", "pygame"]
    # A non-python tool / already-absolute path is untouched.
    assert _rewrite_argv_to_venv(["node", "app.js"], vpy) == ["node", "app.js"]


def test_prepare_setup_prepends_venv_create_and_rewrites_pip(tmp_path: Path):
    mgr = _manager(tmp_path)
    prof = _profile([["pip", "install", "pygame"]], ["python", "game.py"])
    steps, extra = mgr._prepare_setup_steps(prof)
    venv = mgr._venv_dir("default")
    assert steps[0] == ["python", "-m", "venv", str(venv)]
    assert steps[1] == [str(_venv_python(venv)), "-m", "pip", "install", "pygame"]
    assert extra == [str(venv)]
    # The venv lives under work_root, never inside the user's workspace.
    assert not str(venv).startswith(str(mgr.workspace_root))


def test_prepare_setup_reuses_existing_venv(tmp_path: Path):
    mgr = _manager(tmp_path)
    venv = mgr._venv_dir("default")
    _venv_python(venv).parent.mkdir(parents=True, exist_ok=True)
    _venv_python(venv).write_text("")  # pretend the venv already exists
    prof = _profile([["pip", "install", "pygame"]], ["python", "game.py"])
    steps, _ = mgr._prepare_setup_steps(prof)
    # No `python -m venv` bootstrap when the venv is already there.
    assert steps == [[str(_venv_python(venv)), "-m", "pip", "install", "pygame"]]


def test_prepare_setup_noop_for_non_install_setup(tmp_path: Path):
    mgr = _manager(tmp_path)
    prof = _profile([["python", "-c", "print('x')"]], ["python", "app.py"])
    steps, extra = mgr._prepare_setup_steps(prof)
    assert steps == [["python", "-c", "print('x')"]]
    assert extra is None


def test_prepare_run_argv_untouched_without_venv(tmp_path: Path):
    mgr = _manager(tmp_path)
    argv, extra = mgr._prepare_run_argv("default", ["python", "game.py"])
    assert argv == ["python", "game.py"] and extra is None


def test_prepare_run_argv_uses_venv_when_present(tmp_path: Path):
    mgr = _manager(tmp_path)
    venv = mgr._venv_dir("default")
    _venv_python(venv).parent.mkdir(parents=True, exist_ok=True)
    _venv_python(venv).write_text("")
    argv, extra = mgr._prepare_run_argv("default", ["python", "game.py"])
    assert argv == [str(_venv_python(venv)), "game.py"]
    assert extra == [str(venv)]


def test_prepare_run_argv_uses_venv_console_script(tmp_path: Path):
    # A hand-edited profile with `uvicorn ...` (not `python ...`) still runs
    # from the venv when the venv provides that console script.
    mgr = _manager(tmp_path)
    venv = mgr._venv_dir("default")
    bindir = _venv_python(venv).parent
    bindir.mkdir(parents=True, exist_ok=True)
    _venv_python(venv).write_text("")
    (bindir / "uvicorn").write_text("")  # venv provides uvicorn
    argv, extra = mgr._prepare_run_argv(
        "default", ["uvicorn", "app:app", "--port", "8000"])
    assert argv == [str(bindir / "uvicorn"), "app:app", "--port", "8000"]
    assert extra == [str(venv)]


def test_prepare_run_argv_leaves_tool_venv_does_not_provide(tmp_path: Path):
    mgr = _manager(tmp_path)
    venv = mgr._venv_dir("default")
    _venv_python(venv).parent.mkdir(parents=True, exist_ok=True)
    _venv_python(venv).write_text("")  # venv exists but has no `node`
    argv, extra = mgr._prepare_run_argv("default", ["node", "server.js"])
    assert argv == ["node", "server.js"] and extra is None


# --------------------------------------------------------------------------- #
# Wiring: _run_setup actually creates the venv + widens the writable set
# --------------------------------------------------------------------------- #
class _FakeProc:
    pid = 0
    returncode = 0

    def wait(self) -> int:
        return 0

    def poll(self) -> int:
        return 0


def test_run_setup_spawns_venv_create_then_install(tmp_path, monkeypatch):
    """End-to-end through ``setup()`` with the sandbox spawn stubbed: the first
    spawned argv is the venv bootstrap, the second is the venv-scoped pip
    install, and both carry the venv dir in ``extra_writable``."""
    monkeypatch.setattr(rp, "_POLL_INTERVAL", 0.01)
    mgr = _manager(tmp_path)
    mgr.rstore.upsert_profile(_profile([["pip", "install", "pygame"]],
                                       ["python", "game.py"]))

    calls: list[dict] = []

    def fake_spawn(argv, **kwargs):
        calls.append({"argv": list(argv), "extra": kwargs.get("extra_writable")})
        return _FakeProc()

    monkeypatch.setattr(mgr, "_spawn", fake_spawn)
    # Resolve sandbox to a concrete value without touching the real backend.
    monkeypatch.setattr(mgr, "_resolve_run_mode",
                        lambda profile, pid: ("none", "none", False, None))

    sess = mgr.setup("default")
    # setup() runs the steps on a daemon thread; wait for it to finish.
    deadline = __import__("time").monotonic() + 5
    while __import__("time").monotonic() < deadline:
        s = mgr.get_session(sess.session_id)
        if s and s.state in ("stopped", "crashed"):
            break
        __import__("time").sleep(0.02)

    venv = mgr._venv_dir("default")
    assert len(calls) == 2
    assert calls[0]["argv"] == ["python", "-m", "venv", str(venv)]
    assert calls[1]["argv"] == [
        str(_venv_python(venv)), "-m", "pip", "install", "pygame"]
    assert calls[0]["extra"] == [str(venv)]
    assert calls[1]["extra"] == [str(venv)]
    # And the venv dir was created so the sandbox can bind it writable.
    assert venv.exists()


def test_start_runs_through_venv_interpreter(tmp_path, monkeypatch):
    """``start()`` (not just the pure helper) reaches ``_spawn`` with the venv
    interpreter + the venv in ``extra_writable`` once the venv exists."""
    mgr = _manager(tmp_path)
    mgr.rstore.upsert_profile(_profile([["pip", "install", "pygame"]],
                                       ["python", "game.py"]))
    venv = mgr._venv_dir("default")
    _venv_python(venv).parent.mkdir(parents=True, exist_ok=True)
    _venv_python(venv).write_text("")  # pretend setup already built it

    seen: dict = {}

    def fake_spawn(argv, **kwargs):
        seen["argv"] = list(argv)
        seen["extra"] = kwargs.get("extra_writable")
        return _FakeProc()

    monkeypatch.setattr(mgr, "_spawn", fake_spawn)
    monkeypatch.setattr(mgr, "_resolve_run_mode",
                        lambda profile, pid: ("none", "none", False, None))
    monkeypatch.setattr(mgr, "_monitor", lambda live, profile: None)

    mgr.start("default")
    assert seen["argv"] == [str(_venv_python(venv)), "game.py"]
    assert seen["extra"] == [str(venv)]


# --------------------------------------------------------------------------- #
# Integration: a REAL `python -m venv` under the resolved OS sandbox
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(_real_backend() is None,
                    reason="no OS sandbox backend (seatbelt/bwrap) available")
def test_setup_creates_real_venv_under_sandbox(tmp_path):
    """The load-bearing path: run setup for real (no stubs) so ``python -m venv``
    actually executes inside the F039 sandbox with only {home, tmp, venv}
    writable, and the venv interpreter really lands. ``pip install --help``
    stands in for a network install so the test stays offline + fast while still
    proving the venv's pip is invoked through the venv interpreter."""
    mgr = _manager(tmp_path)
    mgr.rstore.upsert_profile(
        _profile([["pip", "install", "--help"]], ["python", "game.py"]))
    try:
        sess = mgr.setup("default")
        deadline = time.monotonic() + 60
        end = None
        while time.monotonic() < deadline:
            s = mgr.get_session(sess.session_id)
            if s and s.state in ("stopped", "crashed"):
                end = s
                break
            time.sleep(0.05)
        assert end is not None, "setup never terminated"
        assert end.state == "stopped" and end.exit_code == 0, (
            f"setup did not succeed: {end.state} {end.error}")
        # Proof the venv interpreter was really created (mkdir alone can't do
        # this — only a real `python -m venv` produces bin/python).
        assert _venv_python(mgr._venv_dir("default")).exists()
    finally:
        rp.teardown_all()


# --------------------------------------------------------------------------- #
# Detector: web entrypoint listen-port grounding (fixes the Flask :5000 vs the
# guessed :8000 mismatch that left a working app looking crashed)
# --------------------------------------------------------------------------- #
def _api_port(props) -> int:
    api = next(p for p in props if p.kind == "api")
    return api.ports[0]["preferred"]


def test_detect_reads_flask_hardcoded_port(tmp_path: Path):
    (tmp_path / "app.py").write_text(
        "from flask import Flask\napp = Flask(__name__)\n"
        "app.run(debug=True, port=5000)\n")
    (tmp_path / "requirements.txt").write_text("Flask\n")
    assert _api_port(detect(tmp_path, project_id="p")) == 5000


def test_detect_reads_flask_bare_run_default(tmp_path: Path):
    # Flask's app.run() with no explicit port binds 5000.
    (tmp_path / "app.py").write_text(
        "import flask\napp = flask.Flask(__name__)\napp.run()\n")
    (tmp_path / "requirements.txt").write_text("Flask\n")
    assert _api_port(detect(tmp_path, project_id="p")) == 5000


def test_detect_reads_port_env_default(tmp_path: Path):
    (tmp_path / "app.py").write_text(
        "import os\napp.run(port=int(os.environ.get('PORT', 8080)))\n")
    (tmp_path / "requirements.txt").write_text("Flask\n")
    assert _api_port(detect(tmp_path, project_id="p")) == 8080


def test_detect_reads_uvicorn_port(tmp_path: Path):
    (tmp_path / "app.py").write_text(
        "import uvicorn\nuvicorn.run(app, host='127.0.0.1', port=9001)\n")
    (tmp_path / "requirements.txt").write_text("fastapi\nuvicorn\n")
    assert _api_port(detect(tmp_path, project_id="p")) == 9001


def test_detect_falls_back_to_8000_without_a_readable_port(tmp_path: Path):
    # No literal to read (a bare framework whose port we can't ground) -> 8000.
    (tmp_path / "app.py").write_text("import bottle\nbottle.default_app().run()\n")
    (tmp_path / "requirements.txt").write_text("bottle\n")
    assert _api_port(detect(tmp_path, project_id="p")) == 8000


def test_detect_listen_port_ignores_support_kwarg():
    # `support=` must not be mistaken for `port=`.
    from errorta_council.coding.runtime import _detect_listen_port
    assert _detect_listen_port("app.run(support=True)\n") is None


# --------------------------------------------------------------------------- #
# Auto-setup on Run: a single Run installs deps then starts (no manual gate)
# --------------------------------------------------------------------------- #
def _api_profile(tmp_path: Path) -> "rp.RuntimeProfile":
    return validate_profile(
        {"kind": "api", "runtime_mode": "managed_local",
         "start": ["python", "app.py"], "setup": [["pip", "install", "flask"]],
         "health": {"type": "none"}, "sandbox": "auto"},
        profile_id="default", project_id="p")


def test_start_without_auto_setup_still_blocks(tmp_path: Path):
    mgr = _manager(tmp_path)
    mgr.rstore.upsert_profile(_api_profile(tmp_path))
    session = mgr.start("default", auto_setup=False)
    assert session.state == "crashed"
    assert "setup_required" in (session.error or "")


def test_run_auto_runs_setup_then_proceeds_to_start(tmp_path: Path):
    mgr = _manager(tmp_path)
    mgr.rstore.upsert_profile(_api_profile(tmp_path))
    calls = {"setup": 0}

    def fake_setup(profile_id: str):
        # Simulate a clean install: create the venv interpreter so the pending
        # check flips to False, and report a stopped(0) setup session.
        calls["setup"] += 1
        venv_py = _venv_python(mgr._venv_dir(profile_id))
        venv_py.parent.mkdir(parents=True, exist_ok=True)
        venv_py.write_text("")
        return rp.RuntimeSession(
            session_id="setup1", profile_id=profile_id, state="stopped",
            exit_code=0, _extras={"kind": "setup"})

    def boom(*args, **kwargs):
        raise RuntimeError("spawn reached")

    mgr._setup_sync = fake_setup  # type: ignore[assignment]
    mgr._spawn = boom  # type: ignore[assignment]
    session = mgr.start("default")
    # Setup ran exactly once, and start proceeded past the gate to the spawn.
    assert calls["setup"] == 1
    assert session.state == "crashed"
    assert "spawn_failed" in (session.error or "")


def test_run_auto_setup_failure_does_not_start(tmp_path: Path):
    mgr = _manager(tmp_path)
    mgr.rstore.upsert_profile(_api_profile(tmp_path))

    def failed_setup(profile_id: str):
        # A crashed install must NOT flip the venv and must stop the Run here.
        return rp.RuntimeSession(
            session_id="setup-bad", profile_id=profile_id, state="crashed",
            exit_code=1, error="setup step exited 1", _extras={"kind": "setup"})

    def must_not_spawn(*args, **kwargs):
        raise AssertionError("start must not spawn after a failed setup")

    mgr._setup_sync = failed_setup  # type: ignore[assignment]
    mgr._spawn = must_not_spawn  # type: ignore[assignment]
    session = mgr.start("default")
    assert session.state == "crashed"
    assert session.session_id == "setup-bad"
    assert "setup step exited 1" in (session.error or "")


# --------------------------------------------------------------------------- #
# Fail-closed: a cancelled-mid-install setup must NOT count as success (else a
# program would start with its deps only half-installed — the venv interpreter
# exists after `python -m venv` but the pip step never landed).
# --------------------------------------------------------------------------- #
def test_setup_succeeded_requires_clean_exit():
    from errorta_council.coding.runtime_process import _setup_succeeded
    ok = rp.RuntimeSession(session_id="s", profile_id="p", state="stopped", exit_code=0)
    cancelled = rp.RuntimeSession(session_id="s", profile_id="p", state="stopped", exit_code=None)
    failed = rp.RuntimeSession(session_id="s", profile_id="p", state="crashed", exit_code=1)
    assert _setup_succeeded(ok) is True
    assert _setup_succeeded(cancelled) is False  # cancel lands stopped/None
    assert _setup_succeeded(failed) is False


def test_detect_listen_port_ignores_commented_out_run():
    from errorta_council.coding.runtime import _detect_listen_port
    # A commented example must not win over the real call.
    src = "# app.run(port=9999)\napp.run(port=5000)\n"
    assert _detect_listen_port(src) == (5000, True)
    # A file whose only port literal is in a comment has no readable port.
    assert _detect_listen_port("# app.run(port=9999)\n") is None


# --------------------------------------------------------------------------- #
# Fixed port: an app that HARDCODES its port (ignores injected PORT) must be
# targeted at that exact port, not an allocated ephemeral one — otherwise the
# demo link / health probe point where nothing is listening (the AirPlay-on-5000
# case: the preferred-port bind test fails and the old code fell back to an
# ephemeral, but the app still bound its hardcoded port).
# --------------------------------------------------------------------------- #
def _api_web_port(props) -> dict:
    return next(p for p in props if p.kind == "api").ports[0]


def test_detect_marks_hardcoded_port_fixed(tmp_path: Path):
    (tmp_path / "app.py").write_text(
        "from flask import Flask\napp = Flask(__name__)\napp.run(port=5000)\n")
    (tmp_path / "requirements.txt").write_text("Flask\n")
    web = _api_web_port(detect(tmp_path, project_id="p"))
    assert web["preferred"] == 5000 and web.get("fixed") is True


def test_detect_env_driven_port_not_fixed(tmp_path: Path):
    # An app that reads PORT honors the port Errorta injects, so it is NOT fixed.
    (tmp_path / "app.py").write_text(
        "import os\napp.run(port=int(os.environ.get('PORT', 8080)))\n")
    (tmp_path / "requirements.txt").write_text("Flask\n")
    web = _api_web_port(detect(tmp_path, project_id="p"))
    assert web["preferred"] == 8080 and not web.get("fixed")


def _fixed_profile() -> "rp.RuntimeProfile":
    return validate_profile(
        {"kind": "api", "runtime_mode": "managed_local",
         "start": ["python", "app.py"], "setup": [], "health": {"type": "http"},
         "sandbox": "auto",
         "ports": [{"name": "web", "container_port": None,
                    "preferred": 5000, "fixed": True}]},
        profile_id="default", project_id="p")


def test_resolve_listen_port_uses_fixed_port_exactly(tmp_path: Path):
    # A fixed port is returned verbatim with no allocation — even if the port is
    # occupied (AirPlay), because the app binds it regardless and health/demo must
    # target it.
    mgr = _manager(tmp_path)
    # Occupy 5000 to prove the fixed path does NOT fall back to an ephemeral.
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        try:
            s.bind(("127.0.0.1", 5000))
        except OSError:
            s = None  # 5000 already taken on this host — the point still holds
        assert mgr._resolve_listen_port(_fixed_profile()) == 5000
    finally:
        if s is not None:
            s.close()


def test_resolve_listen_port_allocates_when_not_fixed(tmp_path: Path):
    mgr = _manager(tmp_path)
    prof = validate_profile(
        {"kind": "api", "runtime_mode": "managed_local",
         "start": ["python", "app.py"], "setup": [], "health": {"type": "http"},
         "sandbox": "auto",
         "ports": [{"name": "web", "container_port": None, "preferred": 8080}]},
        profile_id="default", project_id="p")
    port = mgr._resolve_listen_port(prof)
    assert isinstance(port, int) and 1024 <= port <= 65535


def test_detect_hardcoded_port_wins_over_unrelated_env_read():
    # An app that HARDCODES its bind port but reads PORT elsewhere (for some other
    # purpose) must classify as fixed at the hardcoded port, not env-driven — the
    # literal .run(port=N) is checked before the env read.
    from errorta_council.coding.runtime import _detect_listen_port
    src = "import os\napp.run(port=3000)\nlog(os.getenv('PORT', 9999))\n"
    assert _detect_listen_port(src) == (3000, True)
    # But a port computed FROM the env inside run() is still env-driven.
    assert _detect_listen_port(
        "import os\napp.run(port=int(os.environ.get('PORT', 5000)))\n") == (5000, False)


def test_resolve_listen_port_privileged_fixed_falls_back_to_allocation(tmp_path: Path):
    # A hardcoded privileged port (<1024) is NOT targeted directly (the child can't
    # bind it) — the fixed path falls through to allocation, preserving the guard.
    mgr = _manager(tmp_path)
    prof = validate_profile(
        {"kind": "api", "runtime_mode": "managed_local",
         "start": ["python", "app.py"], "setup": [], "health": {"type": "http"},
         "sandbox": "auto",
         "ports": [{"name": "web", "container_port": None,
                    "preferred": 80, "fixed": True}]},
        profile_id="default", project_id="p")
    port = mgr._resolve_listen_port(prof)
    assert port != 80 and 1024 <= port <= 65535
