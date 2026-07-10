"""F101 S6 — container runtime execution + teardown command + sandbox exposure.

Container runtime is isolated by the container itself, so the docker child runs
OUTSIDE the F039 OS sandbox (spawn backend "none") while the session honestly
records the ``docker`` isolation tier; it fails closed when docker is
unavailable, and runs the profile's stop command on teardown so no container
leaks. Real docker is NOT required — availability and the spawn are stubbed so
the lifecycle is exercised without a daemon.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from errorta_council.coding import runtime_process as rp
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.runtime import RuntimeProfileStore, validate_profile
from errorta_council.coding.runtime_process import RuntimeProcessManager
from errorta_council.coding.workspace import CodingWorkspace

# A loopback HTTP server standing in for "the container" so the lifecycle runs
# without a docker daemon.
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
    rp.teardown_all()


def _make(project_id, profile_raw):
    store = LedgerStore(project_id)
    store.create_project(north_star="n", definition_of_done="d",
                         target="new", repo_path=None)
    ws = CodingWorkspace(project_id, store)
    ws.setup(target="new", repo_path=None)
    rstore = RuntimeProfileStore.for_ledger(store)
    rstore.upsert_profile(validate_profile(
        profile_raw, profile_id="default", project_id=project_id))
    return RuntimeProcessManager.for_project(project_id), store, ws


def _container_profile(start, *, stop=None):
    return {
        "kind": "container", "runtime_mode": "container", "start": start,
        "stop": stop,
        "health": {"type": "http", "url": "http://127.0.0.1:{port}",
                   "timeout_seconds": 20},
        "ports": [{"name": "web", "container_port": None, "preferred": 0}],
        "sandbox": "auto",
    }


def _wait_state(mgr, sid, targets, timeout=12.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        s = mgr.get_session(sid)
        if s and s.state in targets:
            return s
        time.sleep(0.05)
    raise AssertionError(f"never reached {targets}; last={mgr.get_session(sid).state}")


# --- fail-closed when docker is unavailable --------------------------------- #

def test_container_blocks_when_docker_unavailable(tmp_errorta_home: Path, monkeypatch):
    from errorta_tools.runner import sandbox as sbx
    monkeypatch.setattr(sbx, "is_available", lambda b: b != "docker")
    mgr, _, _ = _make("ct1", _container_profile(["docker", "compose", "up"]))
    s = mgr.start("default")
    assert s.state == "crashed"
    assert s.error == "container_runtime_requires_docker"
    assert rp.teardown_all() == 0  # nothing spawned


# --- container runs UNWRAPPED but records the docker tier ------------------- #

def test_container_runs_unwrapped_and_records_docker(tmp_errorta_home: Path, monkeypatch):
    from errorta_tools.runner import sandbox as sbx
    monkeypatch.setattr(sbx, "is_available", lambda b: True)  # pretend docker is up

    # spy on the spawn to confirm the OS-sandbox wrap is bypassed (backend none)
    from errorta_tools.runner import preview
    seen = {}
    real = preview.spawn_sandboxed_child

    def spy(**kw):
        seen["backend"] = kw.get("backend")
        return real(**kw)

    monkeypatch.setattr(preview, "spawn_sandboxed_child", spy)

    mgr, _, _ = _make("ct2", _container_profile([sys.executable, "-c", _SERVER]))
    s = mgr.start("default")
    assert s.sandbox_backend == "docker"        # honest isolation tier
    # container mode must NOT carry the reduced-isolation (sandbox=none) warning
    assert not any("reduced isolation" in w.lower()
                   for w in s.to_dict().get("safety_warnings", []))
    healthy = _wait_state(mgr, s.session_id, {"healthy"})
    assert healthy.health_status == {"ok": True, "detail": "200"}
    assert seen["backend"] == "none"            # spawned unwrapped (no seatbelt)
    mgr.stop("default")
    _wait_state(mgr, s.session_id, {"stopped"})


# --- teardown runs the stop command ----------------------------------------- #

def test_stop_runs_teardown_command(tmp_errorta_home: Path, monkeypatch):
    from errorta_tools.runner import sandbox as sbx
    monkeypatch.setattr(sbx, "is_available", lambda b: True)

    mgr, _, ws = _make("ct3", _container_profile(
        [sys.executable, "-c", _SERVER],
        stop=[sys.executable, "-c",
              "import pathlib; pathlib.Path('STOPPED.marker').write_text('x')"]))
    s = mgr.start("default")
    _wait_state(mgr, s.session_id, {"healthy"})
    mgr.stop("default")
    _wait_state(mgr, s.session_id, {"stopped"})
    # the stop argv ran in the workspace cwd, leaving its marker
    assert (ws.root() / "STOPPED.marker").exists()


def test_teardown_all_runs_stop_command(tmp_errorta_home: Path, monkeypatch):
    from errorta_tools.runner import sandbox as sbx
    monkeypatch.setattr(sbx, "is_available", lambda b: True)
    mgr, _, ws = _make("ct4", _container_profile(
        [sys.executable, "-c", _SERVER],
        stop=[sys.executable, "-c",
              "import pathlib; pathlib.Path('DOWN.marker').write_text('x')"]))
    s = mgr.start("default")
    _wait_state(mgr, s.session_id, {"healthy"})
    assert rp.teardown_all() >= 1
    assert (ws.root() / "DOWN.marker").exists()


# --- docker as a selectable sandbox for managed_local (exposure) ------------ #

def test_docker_sandbox_backend_resolves_when_available(monkeypatch):
    from errorta_tools.runner import sandbox as sbx
    monkeypatch.setattr(sbx, "is_available", lambda b: True)
    assert rp.resolve_sandbox_backend("docker") == "docker"


def test_run_teardown_command_is_best_effort():
    from errorta_tools.runner.preview import run_teardown_command
    # a nonexistent command must not raise
    assert run_teardown_command(["definitely-not-a-real-binary-xyz"]) is None
    # a real one returns its exit code
    assert run_teardown_command([sys.executable, "-c", "import sys; sys.exit(0)"]) == 0


# --- review fix: docker env is gated to container mode (least privilege) ----- #

def test_child_env_docker_vars_gated_to_container(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "tcp://127.0.0.1:2375")
    # managed_local child env must NOT carry docker vars
    plain = rp._child_env(runner_home=tmp_path, runner_tmp=tmp_path, port=5173)
    assert "DOCKER_HOST" not in plain
    # a container child env does
    dock = rp._child_env(runner_home=tmp_path, runner_tmp=tmp_path, port=None,
                         include_docker=True)
    assert dock["DOCKER_HOST"] == "tcp://127.0.0.1:2375"


# --- review fix: container SETUP also runs unwrapped + fails closed ---------- #

def test_container_setup_fails_closed_without_docker(tmp_errorta_home: Path, monkeypatch):
    from errorta_tools.runner import sandbox as sbx
    monkeypatch.setattr(sbx, "is_available", lambda b: b != "docker")
    mgr, _, _ = _make("cts1", {
        "kind": "container", "runtime_mode": "container",
        "start": ["docker", "compose", "up"],
        "setup": [["docker", "build", "-t", "x", "."]],
        "health": {"type": "none"}, "sandbox": "auto"})
    s = mgr.setup("default")
    assert s.state == "crashed"
    assert s.error == "container_runtime_requires_docker"


def test_container_setup_runs_unwrapped(tmp_errorta_home: Path, monkeypatch):
    from errorta_tools.runner import sandbox as sbx
    monkeypatch.setattr(sbx, "is_available", lambda b: True)
    from errorta_tools.runner import preview
    seen = []
    real = preview.spawn_sandboxed_child
    monkeypatch.setattr(preview, "spawn_sandboxed_child",
                        lambda **kw: (seen.append(kw.get("backend")), real(**kw))[1])

    mgr, _, ws = _make("cts2", {
        "kind": "container", "runtime_mode": "container",
        "start": [sys.executable, "-c", "pass"],
        "setup": [[sys.executable, "-c",
                   "import pathlib; pathlib.Path('BUILT.marker').write_text('x')"]],
        "health": {"type": "none"}, "sandbox": "auto"})
    s = mgr.setup("default")
    _wait_state(mgr, s.session_id, {"stopped", "crashed"})
    assert mgr.get_session(s.session_id).state == "stopped"
    assert seen == ["none"]  # the docker build step spawned unwrapped
    assert (ws.root() / "BUILT.marker").exists()
