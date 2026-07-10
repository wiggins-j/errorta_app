"""F101 S4 — runtime test evidence (runtime_start / health_check / demo_smoke).

Covers the new runtime test KINDS (extending the F087-10 registry, D6): the
grounded verdict from a real sandboxed session, head-bound evidence with the
F087-10 staleness rule, the POST /test route, and the F093 completion-summary
surfacing via the project projection.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from errorta_council.coding import runtime_process as rp
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.runtime import (
    RUNTIME_TEST_KINDS,
    RuntimeProfileStore,
    RuntimeSession,
    latest_runtime_evidence,
    validate_profile,
)
from errorta_council.coding.runtime_process import RuntimeProcessManager
from errorta_council.coding.testing import RuntimeTestResult, run_runtime_test
from errorta_council.coding.workspace import CodingWorkspace

_TAURI = {"x-errorta-origin": "tauri-ui"}

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


def _web_profile(sandbox="auto"):
    return {
        "kind": "web", "runtime_mode": "managed_local",
        "start": ["python", "-c", _SERVER],
        "health": {"type": "http", "url": "http://127.0.0.1:{port}",
                   "timeout_seconds": 20},
        "demo": {"type": "url", "url": "http://127.0.0.1:{port}"},
        "ports": [{"name": "web", "container_port": None, "preferred": 0}],
        "sandbox": sandbox,
    }


def _make(project_id, profile_raw, *, commit=True):
    store = LedgerStore(project_id)
    store.create_project(north_star="n", definition_of_done="d",
                         target="new", repo_path=None)
    ws = CodingWorkspace(project_id, store)
    ws.setup(target="new", repo_path=None)
    if commit:
        from errorta_tools.runner.apply_workspace import _git
        (ws.root() / "marker.txt").write_text("x")
        _git(ws.root(), "add", "-A")
        _git(ws.root(), "commit", "-q", "-m", "seed")
    rstore = RuntimeProfileStore.for_ledger(store)
    rstore.upsert_profile(validate_profile(
        profile_raw, profile_id="default", project_id=project_id))
    mgr = RuntimeProcessManager.for_project(project_id)
    return mgr, store, ws


# --- store + freshness ------------------------------------------------------ #

def test_record_and_list_runtime_tests(tmp_errorta_home: Path):
    store = LedgerStore("ev1")
    store.create_project(north_star="n", definition_of_done="d",
                         target="new", repo_path=None)
    rstore = RuntimeProfileStore.for_ledger(store)
    rstore.record_runtime_test(kind="runtime_start", profile_id="default",
                               session_id="rs-1", passed=True, head="abc123")
    rows = rstore.list_runtime_tests()
    assert len(rows) == 1 and rows[0]["kind"] == "runtime_start"
    assert rows[0]["head"] == "abc123" and rows[0]["passed"] is True


def test_latest_evidence_freshness_binds_to_head(tmp_errorta_home: Path):
    store = LedgerStore("ev2")
    store.create_project(north_star="n", definition_of_done="d",
                         target="new", repo_path=None)
    rstore = RuntimeProfileStore.for_ledger(store)
    # a pass against an OLD head
    rstore.record_runtime_test(kind="health_check", profile_id="default",
                               session_id="rs-1", passed=True, head="OLD")
    ev = latest_runtime_evidence(rstore, current_head="NEW")
    assert ev["results"][0]["passed"] is True
    assert ev["results"][0]["fresh"] is False   # stale: head changed
    assert ev["any_fresh_pass"] is False
    # a pass against the CURRENT head
    rstore.record_runtime_test(kind="health_check", profile_id="default",
                               session_id="rs-2", passed=True, head="NEW")
    ev2 = latest_runtime_evidence(rstore, current_head="NEW")
    assert ev2["results"][0]["fresh"] is True
    assert ev2["any_fresh_pass"] is True


def test_latest_evidence_one_per_profile_kind(tmp_errorta_home: Path):
    store = LedgerStore("ev3")
    store.create_project(north_star="n", definition_of_done="d",
                         target="new", repo_path=None)
    rstore = RuntimeProfileStore.for_ledger(store)
    for sid, passed in (("rs-1", False), ("rs-2", True)):
        rstore.record_runtime_test(kind="runtime_start", profile_id="default",
                                   session_id=sid, passed=passed, head="H")
    ev = latest_runtime_evidence(rstore, current_head="H")
    # last event per (profile, kind) wins
    assert len(ev["results"]) == 1
    assert ev["results"][0]["session_id"] == "rs-2"
    assert ev["results"][0]["fresh"] is True


# --- run_runtime_test (grounded) -------------------------------------------- #

# cli_transcript is a one-shot CLI kind, not a server-start kind: against a
# long-running web server it would (correctly) time out rather than exit 0, so it
# is graded by its own dedicated tests in test_f101_runtime_cli_transcript.py.
@pytest.mark.parametrize(
    "kind", [k for k in RUNTIME_TEST_KINDS if k != "cli_transcript"])
def test_runtime_test_kinds_pass_for_healthy_web(tmp_errorta_home: Path, kind):
    mgr, _, _ = _make(f"rt-{kind}", _web_profile("auto"))
    result = run_runtime_test(mgr, "default", kind, head="H", timeout=20)
    assert isinstance(result, RuntimeTestResult)
    assert result.kind == kind
    assert result.passed is True, result.detail
    assert result.session_id
    # the runtime was torn down (no leaked server)
    assert rp.teardown_all() == 0


def test_runtime_start_passes_for_cli_exit_zero(tmp_errorta_home: Path):
    mgr, _, _ = _make("rt-cli", {
        "kind": "cli", "runtime_mode": "managed_local",
        "start": ["python", "-c", "pass"], "health": {"type": "none"},
        "demo": {"type": "command", "command": ["python", "-c", "pass"]},
        "sandbox": "auto"})
    r = run_runtime_test(mgr, "default", "runtime_start", head="H", timeout=20)
    assert r.passed is True and r.state == "stopped"


def test_runtime_test_fails_for_crashing_start(tmp_errorta_home: Path):
    mgr, _, _ = _make("rt-crash", {
        "kind": "web", "runtime_mode": "managed_local",
        "start": ["python", "-c", "import sys; sys.exit(1)"],
        "health": {"type": "http", "url": "http://127.0.0.1:{port}",
                   "timeout_seconds": 5},
        "demo": {"type": "url", "url": "http://127.0.0.1:{port}"},
        "ports": [{"name": "web", "container_port": None, "preferred": 0}],
        "sandbox": "auto"})
    r = run_runtime_test(mgr, "default", "health_check", head="H", timeout=10)
    assert r.passed is False
    assert r.state == "crashed"


def test_runtime_test_unknown_profile(tmp_errorta_home: Path):
    mgr, _, _ = _make("rt-noprof", _web_profile("auto"))
    r = run_runtime_test(mgr, "ghost", "runtime_start", head="H")
    assert r.passed is False and r.detail == "profile_not_found"


def test_runtime_test_unknown_kind_raises(tmp_errorta_home: Path):
    mgr, _, _ = _make("rt-badkind", _web_profile("auto"))
    with pytest.raises(ValueError):
        run_runtime_test(mgr, "default", "bogus", head="H")


def test_demo_smoke_probes_the_session_it_started():
    profile = validate_profile(
        _web_profile("auto"), profile_id="default", project_id="p")
    seen: list[str | None] = []
    stopped: list[str] = []

    class Store:
        def get_profile(self, profile_id: str):
            return profile if profile_id == "default" else None

    class Manager:
        rstore = Store()

        def start(self, profile_id: str):
            assert profile_id == "default"
            return RuntimeSession(
                session_id="fresh", profile_id="default", state="healthy",
                started_at="", allocated_ports=[2222], sandbox_backend="none",
            )

        def get_session(self, session_id: str):
            assert session_id == "fresh"
            return RuntimeSession(
                session_id="fresh", profile_id="default", state="healthy",
                started_at="", allocated_ports=[2222], sandbox_backend="none",
                health_status={"ok": True, "detail": "200"},
            )

        def probe_demo(self, profile_id: str, *, session_id: str | None = None):
            assert profile_id == "default"
            seen.append(session_id)
            return {"ok": True, "detail": "200"}

        def stop(self, profile_id: str):
            stopped.append(profile_id)

    r = run_runtime_test(Manager(), "default", "demo_smoke", head="H")

    assert r.passed is True
    assert r.session_id == "fresh"
    assert seen == ["fresh"]
    assert stopped == ["default"]


# --- route + project surfacing ---------------------------------------------- #

def _client(*, headers=None) -> TestClient:
    from errorta_app.routes import coding as coding_routes
    app = FastAPI()
    app.include_router(coding_routes.router)
    return TestClient(app, headers=headers)


def test_test_route_runs_records_and_surfaces_fresh(tmp_errorta_home: Path):
    _make("rtr1", _web_profile("auto"))
    c = _client(headers=_TAURI)

    r = c.post("/coding/projects/rtr1/runtime/default/test",
               json={"kind": "runtime_start"})
    assert r.status_code == 200, r.text
    res = r.json()["result"]
    assert res["kind"] == "runtime_start" and res["passed"] is True

    # F093 surfacing: GET project shows the evidence, fresh against current head.
    proj = c.get("/coding/projects/rtr1").json()["project"]
    ev = proj["runtime_evidence"]
    assert ev["any_fresh_pass"] is True
    entry = next(e for e in ev["results"] if e["kind"] == "runtime_start")
    assert entry["passed"] is True and entry["fresh"] is True


def test_test_route_evidence_goes_stale_when_head_moves(tmp_errorta_home: Path):
    _make("rtr2", _web_profile("auto"))
    c = _client(headers=_TAURI)
    c.post("/coding/projects/rtr2/runtime/default/test",
           json={"kind": "runtime_start"})

    # advance the worktree head after the evidence was recorded
    from errorta_tools.runner.apply_workspace import _git
    store = LedgerStore("rtr2")
    ws = CodingWorkspace("rtr2", store); ws.set_target("new")
    (ws.root() / "more.txt").write_text("y")
    _git(ws.root(), "add", "-A")
    _git(ws.root(), "commit", "-q", "-m", "advance")

    proj = c.get("/coding/projects/rtr2").json()["project"]
    ev = proj["runtime_evidence"]
    entry = next(e for e in ev["results"] if e["kind"] == "runtime_start")
    assert entry["passed"] is True
    assert entry["fresh"] is False   # stale: pass was against the old head
    assert ev["any_fresh_pass"] is False


def test_test_route_bad_kind_422(tmp_errorta_home: Path):
    _make("rtr3", _web_profile("auto"))
    c = _client(headers=_TAURI)
    r = c.post("/coding/projects/rtr3/runtime/default/test", json={"kind": "nope"})
    assert r.status_code == 422


def test_test_route_requires_tauri_origin(tmp_errorta_home: Path):
    _make("rtr4", _web_profile("auto"))
    c = _client()
    r = c.post("/coding/projects/rtr4/runtime/default/test",
               json={"kind": "runtime_start"})
    assert r.status_code == 403


def test_project_runtime_evidence_empty_when_no_runtime(tmp_errorta_home: Path):
    store = LedgerStore("rtr5")
    store.create_project(north_star="n", definition_of_done="d",
                         target="new", repo_path=None)
    c = _client(headers=_TAURI)
    proj = c.get("/coding/projects/rtr5").json()["project"]
    assert proj["runtime_evidence"] == {
        "results": [], "any_fresh_pass": False, "current_head": ""}
