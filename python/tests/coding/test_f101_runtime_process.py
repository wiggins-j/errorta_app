"""F101 S3 — sandboxed managed-local runtime process manager.

Spawns REAL short-lived children (a tiny loopback HTTP server / exit-N scripts)
through the F039 sandbox, and asserts the full lifecycle: start -> running ->
healthy, capped+redacted logs, one-shot health check, fail-closed sandbox
blocking, setup success/failure, orphan reconciliation, working-dir
confinement, and — the critical D3 invariant — process-GROUP teardown leaves no
orphaned process and no bound port.
"""
from __future__ import annotations

import os
import socket
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from errorta_council.coding import runtime_process as rp
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.runtime import (
    RuntimeProfileStore,
    RuntimeSession,
    validate_profile,
)
from errorta_council.coding.runtime_process import RuntimeProcessManager
from errorta_council.coding.workspace import CodingWorkspace

_TAURI = {"x-errorta-origin": "tauri-ui"}

# A loopback HTTP server that binds the allocated PORT and answers 200.
_SERVER = (
    "import os,http.server,socketserver\n"
    "port=int(os.environ['PORT'])\n"
    "class H(http.server.BaseHTTPRequestHandler):\n"
    " def do_GET(self):\n"
    "  self.send_response(200); self.end_headers(); self.wfile.write(b'ok')\n"
    " def log_message(self,*a): pass\n"
    "socketserver.TCPServer(('127.0.0.1',port),H).serve_forever()\n"
)


@pytest.fixture(autouse=True)
def _fast_and_clean(monkeypatch):
    monkeypatch.setattr(rp, "_POLL_INTERVAL", 0.05)
    monkeypatch.setattr(rp, "_GRACE_SECONDS", 1.0)
    yield
    rp.teardown_all()  # never leak a child across tests


def _make_manager(project_id: str, profile_raw: dict) -> tuple[RuntimeProcessManager, LedgerStore]:
    store = LedgerStore(project_id)
    store.create_project(north_star="n", definition_of_done="d",
                         target="new", repo_path=None)
    ws = CodingWorkspace(project_id, store)
    ws.setup(target="new", repo_path=None)
    rstore = RuntimeProfileStore.for_ledger(store)
    rstore.upsert_profile(validate_profile(
        profile_raw, profile_id="default", project_id=project_id))
    mgr = RuntimeProcessManager.for_project(project_id)
    return mgr, store


def _web_profile(sandbox: str = "auto") -> dict:
    return {
        "kind": "web", "runtime_mode": "managed_local",
        "start": ["python", "-c", _SERVER],
        "health": {"type": "http", "url": "http://127.0.0.1:{port}",
                   "timeout_seconds": 20},
        "ports": [{"name": "web", "container_port": None, "preferred": 0}],
        "sandbox": sandbox,
    }


def _wait_state(mgr, sid, targets, timeout=12.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        s = mgr.get_session(sid)
        last = s.state if s else None
        if s and s.state in targets:
            return s
        time.sleep(0.05)
    raise AssertionError(f"session {sid} never reached {targets}; last={last}")


# --- primitives ------------------------------------------------------------- #

def test_resolve_sandbox_auto_and_none():
    assert rp.resolve_sandbox_backend("none") == "none"
    # auto resolves to a concrete backend (seatbelt on mac / bwrap on linux / none)
    assert rp.resolve_sandbox_backend("auto") in {"seatbelt", "bwrap", "none"}


def test_resolve_sandbox_unknown_fails_closed():
    from errorta_tools.runner.sandbox import SandboxUnavailable
    with pytest.raises(SandboxUnavailable):
        rp.resolve_sandbox_backend("bogus")


def test_resolve_sandbox_explicit_unavailable_blocks(monkeypatch):
    from errorta_tools.runner import sandbox as sbx
    monkeypatch.setattr(sbx, "is_available", lambda b: False)
    with pytest.raises(sbx.SandboxUnavailable):
        rp.resolve_sandbox_backend("docker")


def test_allocate_loopback_refuses_privileged():
    # A privileged preferred port is refused; an ephemeral (>=1024) port is used.
    port = rp.allocate_loopback_port(80)
    assert port >= 1024


def test_allocate_loopback_uses_free_preferred():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    free = s.getsockname()[1]
    s.close()
    assert rp.allocate_loopback_port(free) == free


def test_preview_resolves_python_and_pip_aliases(monkeypatch):
    from errorta_tools.runner import preview

    def fake_which(name: str):
        return {
            "python": None,
            "python3": "/usr/bin/python3",
            "pip": None,
            "pip3": "/usr/bin/pip3",
        }[name]

    monkeypatch.setattr(preview.shutil, "which", fake_which)

    assert preview._resolve_common_tool(["python", "-c", "pass"]) == [
        "/usr/bin/python3", "-c", "pass",
    ]
    assert preview._resolve_common_tool(["pip", "install", "-r", "requirements.txt"]) == [
        "/usr/bin/pip3", "install", "-r", "requirements.txt",
    ]


def test_preview_resolves_macos_godot_app_bundle(monkeypatch, tmp_path):
    """A Godot project's ``godot --path .`` start command resolves to the
    ``.app`` bundle binary when Godot ships as a macOS app with no CLI on PATH.
    """
    from errorta_tools.runner import preview

    binary = tmp_path / "Godot.app" / "Contents" / "MacOS" / "Godot"
    binary.parent.mkdir(parents=True)
    binary.write_text("")  # stand-in for the engine binary

    monkeypatch.setattr(preview.sys, "platform", "darwin")
    monkeypatch.setattr(preview.shutil, "which", lambda name: None)  # no CLI on PATH
    monkeypatch.setattr(preview, "_MACOS_APP_DIRS", (tmp_path,))

    assert preview._resolve_common_tool(["godot", "--path", "."]) == [
        str(binary), "--path", ".",
    ]


def test_preview_prefers_path_godot_over_app_bundle(monkeypatch, tmp_path):
    """A real ``godot`` CLI on PATH wins over the ``.app`` fallback."""
    from errorta_tools.runner import preview

    binary = tmp_path / "Godot.app" / "Contents" / "MacOS" / "Godot"
    binary.parent.mkdir(parents=True)
    binary.write_text("")

    monkeypatch.setattr(preview.sys, "platform", "darwin")
    monkeypatch.setattr(preview.shutil, "which", lambda name: "/opt/homebrew/bin/godot")
    monkeypatch.setattr(preview, "_MACOS_APP_DIRS", (tmp_path,))

    # Left as a bare PATH-resolved name (existing behavior), not the bundle path.
    assert preview._resolve_common_tool(["godot", "--path", "."]) == ["godot", "--path", "."]


def test_preview_godot_app_fallback_is_darwin_only(monkeypatch, tmp_path):
    """The ``.app`` fallback never fires off macOS — the bare name falls through."""
    from errorta_tools.runner import preview

    binary = tmp_path / "Godot.app" / "Contents" / "MacOS" / "Godot"
    binary.parent.mkdir(parents=True)
    binary.write_text("")

    monkeypatch.setattr(preview.sys, "platform", "linux")
    monkeypatch.setattr(preview.shutil, "which", lambda name: None)
    monkeypatch.setattr(preview, "_MACOS_APP_DIRS", (tmp_path,))

    assert preview._resolve_common_tool(["godot", "--path", "."]) == ["godot", "--path", "."]


def test_redact_log_line_masks_secrets():
    assert "sk-ant-" not in rp.redact_log_line("key sk-ant-" + "A" * 20)
    out = rp.redact_log_line("API_KEY=supersecretvalue")
    assert "supersecretvalue" not in out and out.startswith("API_KEY=")
    out2 = rp.redact_log_line("DATABASE_PASSWORD=hunter2")
    assert "hunter2" not in out2


# --- start -> healthy -> stop lifecycle ------------------------------------- #

def test_start_reaches_healthy_then_stop_tears_down(tmp_errorta_home: Path):
    mgr, _ = _make_manager("rpx1", _web_profile("auto"))
    started = mgr.start("default")
    assert started.state in {"starting", "running", "healthy"}
    sid = started.session_id
    assert started.allocated_ports and started.allocated_ports[0] >= 1024
    port = started.allocated_ports[0]

    healthy = _wait_state(mgr, sid, {"healthy"})
    assert healthy.health_status == {"ok": True, "detail": "200"}
    assert healthy.sandbox_backend in {"seatbelt", "bwrap", "none"}
    pgid = healthy.pgid
    assert isinstance(pgid, int)

    mgr.stop("default")
    stopped = _wait_state(mgr, sid, {"stopped"})
    assert stopped.state == "stopped"
    assert stopped.ended_at

    # D3: no orphaned process group.
    with pytest.raises(ProcessLookupError):
        os.killpg(pgid, 0)
    # D3: no live listener holds the port. SO_REUSEADDR so this asserts "no
    # active LISTEN socket" rather than tripping on the kernel's TIME_WAIT
    # (which has no owning process and is expected after a clean shutdown).
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("127.0.0.1", port))
    finally:
        s.close()


def test_start_sandbox_none_flags_reduced_isolation(tmp_errorta_home: Path):
    mgr, _ = _make_manager("rpx2", _web_profile("none"))
    started = mgr.start("default")
    assert started.sandbox_backend == "none"
    warnings = started.to_dict().get("safety_warnings", [])
    assert any("reduced isolation" in w.lower() for w in warnings)
    _wait_state(mgr, started.session_id, {"running", "healthy"})
    mgr.stop("default")


def test_logs_are_captured_and_tailed(tmp_errorta_home: Path):
    script = "print('LINE-ONE'); print('LINE-TWO')\nimport sys; sys.exit(0)"
    mgr, _ = _make_manager("rpx3", {
        "kind": "cli", "runtime_mode": "managed_local",
        "start": ["python", "-c", script], "health": {"type": "none"},
        "sandbox": "auto"})
    started = mgr.start("default")
    _wait_state(mgr, started.session_id, {"stopped"})
    logs = mgr.get_logs(started.session_id)
    assert set(logs) == {"lines", "truncated"}
    joined = "\n".join(logs["lines"])
    assert "LINE-ONE" in joined and "LINE-TWO" in joined
    assert logs["truncated"] is False


def test_cli_start_exit_zero_is_stopped(tmp_errorta_home: Path):
    mgr, _ = _make_manager("rpx4", {
        "kind": "cli", "runtime_mode": "managed_local",
        "start": ["python", "-c", "pass"], "health": {"type": "none"},
        "sandbox": "auto"})
    s = mgr.start("default")
    end = _wait_state(mgr, s.session_id, {"stopped", "crashed"})
    assert end.state == "stopped" and end.exit_code == 0


def test_cli_start_nonzero_is_crashed(tmp_errorta_home: Path):
    mgr, _ = _make_manager("rpx5", {
        "kind": "cli", "runtime_mode": "managed_local",
        "start": ["python", "-c", "import sys; sys.exit(7)"],
        "health": {"type": "none"}, "sandbox": "auto"})
    s = mgr.start("default")
    end = _wait_state(mgr, s.session_id, {"crashed"})
    assert end.state == "crashed" and end.exit_code == 7


def test_start_blocked_when_pip_setup_declared_but_venv_missing(tmp_errorta_home: Path):
    # A venv-backed project (pip-install setup) must run setup FIRST. Starting
    # without it would fall back to the sidecar interpreter and crash on the missing
    # dep (the pygame case). With ``auto_setup=False`` (the explicit "keep setup a
    # separate step" opt-out) the guard blocks with a clear setup_required message —
    # no process spawned, no confusing ModuleNotFoundError. (The DEFAULT behavior —
    # a single Run auto-runs setup then starts — is covered in test_f101_venv_deps.)
    profile = {
        "kind": "desktop", "runtime_mode": "managed_local",
        "start": ["python", "main.py"],
        "setup": [["pip", "install", "-r", "requirements.txt"]],
        "health": {"type": "none"}, "sandbox": "none"}
    mgr, _ = _make_manager("rp-setupgate", profile)
    s = mgr.start("default", auto_setup=False)
    assert s.state == "crashed"
    assert "setup_required" in (s.error or "")
    # run_cli is guarded the same way.
    c = mgr.run_cli("default", auto_setup=False)
    assert c.state == "crashed"
    assert "setup_required" in (c.error or "")


def test_start_not_blocked_when_no_pip_setup(tmp_errorta_home: Path):
    # A project with no pip-install setup needs no venv, so the guard never fires.
    mgr, _ = _make_manager("rp-nosetup", {
        "kind": "cli", "runtime_mode": "managed_local",
        "start": ["python", "-c", "pass"], "health": {"type": "none"},
        "sandbox": "auto"})
    s = mgr.start("default")
    end = _wait_state(mgr, s.session_id, {"stopped", "crashed"})
    assert end.state == "stopped" and end.exit_code == 0


# --- setup ------------------------------------------------------------------ #

def test_setup_runs_steps_and_succeeds(tmp_errorta_home: Path):
    mgr, _ = _make_manager("rps1", {
        "kind": "web", "runtime_mode": "managed_local",
        "start": ["python", "-c", "pass"],
        "setup": [["python", "-c", "print('installing')"]],
        "health": {"type": "none"}, "sandbox": "auto"})
    s = mgr.setup("default")
    end = _wait_state(mgr, s.session_id, {"stopped", "crashed"})
    assert end.state == "stopped" and end.exit_code == 0
    assert "installing" in "\n".join(mgr.get_logs(s.session_id)["lines"])


def test_setup_failing_step_is_crashed(tmp_errorta_home: Path):
    mgr, _ = _make_manager("rps2", {
        "kind": "web", "runtime_mode": "managed_local",
        "start": ["python", "-c", "pass"],
        "setup": [["python", "-c", "import sys; sys.exit(2)"]],
        "health": {"type": "none"}, "sandbox": "auto"})
    s = mgr.setup("default")
    end = _wait_state(mgr, s.session_id, {"crashed"})
    assert end.state == "crashed"


# --- fail-closed / safety --------------------------------------------------- #

def test_explicit_unavailable_sandbox_records_blocked_session(tmp_errorta_home, monkeypatch):
    mgr, _ = _make_manager("rpb1", _web_profile("docker"))
    from errorta_tools.runner import sandbox as sbx
    monkeypatch.setattr(sbx, "is_available", lambda b: b == "none")
    s = mgr.start("default")
    assert s.state == "crashed"
    assert "sandbox_unavailable_docker" in (s.error or "")
    # nothing was spawned
    assert rp.teardown_all() == 0


def test_working_dir_escape_is_rejected(tmp_errorta_home: Path):
    mgr, _ = _make_manager("rpb2", _web_profile("auto"))
    prof = mgr.rstore.get_profile("default")
    # forge an escaping working_dir directly in the store (bypassing validation)
    raw = prof.to_dict()
    raw["working_dir"] = "../../etc"
    import json
    (mgr.work_root / "runtime-profiles.json").write_text(
        json.dumps({"default": raw}))
    from errorta_council.coding.runtime_process import RuntimeProcessError
    with pytest.raises(RuntimeProcessError):
        mgr.start("default")


# --- teardown_all / reconcile ---------------------------------------------- #

def test_teardown_all_kills_live_process(tmp_errorta_home: Path):
    mgr, _ = _make_manager("rpt1", _web_profile("auto"))
    s = mgr.start("default")
    healthy = _wait_state(mgr, s.session_id, {"healthy"})
    pgid = healthy.pgid
    assert rp.teardown_all() >= 1
    with pytest.raises(ProcessLookupError):
        os.killpg(pgid, 0)


def test_reconcile_orphans_marks_stale_disk_sessions(tmp_errorta_home: Path):
    store = LedgerStore("rpt2")
    store.create_project(north_star="n", definition_of_done="d",
                         target="new", repo_path=None)
    ws = CodingWorkspace("rpt2", store)
    ws.setup(target="new", repo_path=None)
    rstore = RuntimeProfileStore.for_ledger(store)
    # A session left "healthy" by a previous (now-dead) sidecar, not in _LIVE.
    rstore.append_session(RuntimeSession(
        session_id="rs-orphan", profile_id="default", state="healthy"))
    RuntimeProcessManager.for_project("rpt2")  # runs reconcile_orphans
    after = rstore.get_session("rs-orphan")
    assert after.state == "crashed"
    assert after.error == "sidecar_restarted_no_resume"


def test_health_check_one_shot(tmp_errorta_home: Path):
    mgr, _ = _make_manager("rph1", _web_profile("auto"))
    s = mgr.start("default")
    _wait_state(mgr, s.session_id, {"healthy"})
    status = mgr.health_check("default")
    assert status["ok"] is True and status["detail"] == "200"
    mgr.stop("default")


# --- routes ----------------------------------------------------------------- #

def _client(*, headers=None) -> TestClient:
    from errorta_app.routes import coding as coding_routes
    app = FastAPI()
    app.include_router(coding_routes.router)
    return TestClient(app, headers=headers)


def _route_project(project_id: str, profile_raw: dict) -> None:
    store = LedgerStore(project_id)
    store.create_project(north_star="n", definition_of_done="d",
                         target="new", repo_path=None)
    ws = CodingWorkspace(project_id, store)
    ws.setup(target="new", repo_path=None)
    rstore = RuntimeProfileStore.for_ledger(store)
    rstore.upsert_profile(validate_profile(
        profile_raw, profile_id="default", project_id=project_id))


def test_routes_start_session_logs_stop(tmp_errorta_home: Path):
    _route_project("rpr1", _web_profile("auto"))
    c = _client(headers=_TAURI)

    r = c.post("/coding/projects/rpr1/runtime/default/start")
    assert r.status_code == 200, r.text
    sid = r.json()["session"]["session_id"]

    deadline = time.monotonic() + 12
    state = None
    while time.monotonic() < deadline:
        g = c.get(f"/coding/projects/rpr1/runtime/sessions/{sid}")
        assert g.status_code == 200
        state = g.json()["session"]["state"]
        if state == "healthy":
            break
        time.sleep(0.05)
    assert state == "healthy"

    logs = c.get(f"/coding/projects/rpr1/runtime/sessions/{sid}/logs")
    assert logs.status_code == 200 and set(logs.json()) == {"lines", "truncated"}

    hc = c.post("/coding/projects/rpr1/runtime/default/health-check")
    assert hc.status_code == 200 and hc.json()["health_status"]["ok"] is True

    st = c.post("/coding/projects/rpr1/runtime/default/stop")
    assert st.status_code == 200 and st.json() == {"stopped": True}


def test_route_start_requires_tauri_origin(tmp_errorta_home: Path):
    _route_project("rpr2", _web_profile("auto"))
    c = _client()
    assert c.post("/coding/projects/rpr2/runtime/default/start").status_code == 403


def test_route_setup_requires_confirm(tmp_errorta_home: Path):
    _route_project("rpr3", _web_profile("auto"))
    c = _client(headers=_TAURI)
    r = c.post("/coding/projects/rpr3/runtime/default/setup", json={})
    assert r.status_code == 400 and r.json()["detail"] == "setup_requires_confirm"


def test_route_start_unknown_profile_404(tmp_errorta_home: Path):
    _route_project("rpr4", _web_profile("auto"))
    c = _client(headers=_TAURI)
    r = c.post("/coding/projects/rpr4/runtime/ghost/start")
    assert r.status_code == 404


def test_route_session_unknown_404(tmp_errorta_home: Path):
    _route_project("rpr5", _web_profile("auto"))
    c = _client(headers=_TAURI)
    assert c.get("/coding/projects/rpr5/runtime/sessions/nope").status_code == 404


def test_route_runtime_no_worktree_409(tmp_errorta_home: Path):
    store = LedgerStore("rpr6")
    store.create_project(north_star="n", definition_of_done="d",
                         target="new", repo_path=None)
    c = _client(headers=_TAURI)
    r = c.post("/coding/projects/rpr6/runtime/default/start")
    assert r.status_code == 409


def test_probe_demo_prefers_requested_session_port(monkeypatch, tmp_errorta_home: Path):
    profile = _web_profile("auto")
    profile["demo"] = {"type": "url", "url": "http://127.0.0.1:{port}"}
    mgr, _ = _make_manager("rpdemo-port", profile)
    mgr.rstore.append_session(RuntimeSession(
        session_id="old", profile_id="default", state="running",
        started_at="", allocated_ports=[1111], sandbox_backend="none",
    ))
    mgr.rstore.append_session(RuntimeSession(
        session_id="new", profile_id="default", state="running",
        started_at="", allocated_ports=[2222], sandbox_backend="none",
    ))
    seen: list[str] = []

    def fake_probe(url: str):
        seen.append(url)
        return True, "ok"

    monkeypatch.setattr(rp, "_probe", fake_probe)

    assert mgr.probe_demo("default", session_id="new") == {"ok": True, "detail": "ok"}
    assert seen[-1] == "http://127.0.0.1:2222"

    assert mgr.probe_demo("default") == {"ok": True, "detail": "ok"}
    assert seen[-1] == "http://127.0.0.1:2222"
