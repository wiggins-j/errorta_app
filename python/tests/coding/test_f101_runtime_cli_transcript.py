"""F101-02 — CLI transcript runner (manager.run_cli + route + evidence kind).

Spawns REAL short-lived children through the F039 sandbox and asserts the
transcript-run lifecycle: exit-0 -> stopped/passed, non-zero -> stopped/failed
(NOT crashed), a hanging child group-killed by the time-box -> stopped/timed_out
with no orphan, argv-only args passthrough, unbalanced-quote rejection, sandbox
honesty, redaction of secrets in transcript + argv, the route surface (incl. no
/mobile/v1 analog), and the WARN-only cli_transcript evidence kind.
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
    RuntimeProfileStore,
    validate_profile,
)
from errorta_council.coding.runtime_process import (
    RuntimeProcessError,
    RuntimeProcessManager,
)
from errorta_council.coding.workspace import CodingWorkspace

_TAURI = {"x-errorta-origin": "tauri-ui"}


@pytest.fixture(autouse=True)
def _fast_and_clean(monkeypatch):
    monkeypatch.setattr(rp, "_POLL_INTERVAL", 0.05)
    monkeypatch.setattr(rp, "_GRACE_SECONDS", 0.5)
    yield
    rp.teardown_all()


def _cli_profile(start: list[str], *, sandbox: str = "auto",
                 demo: dict | None = None) -> dict:
    return {
        "kind": "cli", "runtime_mode": "managed_local",
        "start": start, "health": {"type": "none"},
        "demo": demo or {}, "sandbox": sandbox,
    }


def _make_manager(project_id: str, profile_raw: dict) -> RuntimeProcessManager:
    store = LedgerStore(project_id)
    store.create_project(north_star="n", definition_of_done="d",
                         target="new", repo_path=None)
    ws = CodingWorkspace(project_id, store)
    ws.setup(target="new", repo_path=None)
    rstore = RuntimeProfileStore.for_ledger(store)
    rstore.upsert_profile(validate_profile(
        profile_raw, profile_id="default", project_id=project_id))
    return RuntimeProcessManager.for_project(project_id)


def _wait_terminal(mgr, sid, timeout=15.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        s = mgr.get_session(sid)
        last = s.state if s else None
        if s and s.state in ("stopped", "crashed"):
            return s
        time.sleep(0.05)
    raise AssertionError(f"session {sid} never reached terminal; last={last}")


# --- run_cli manager lifecycle --------------------------------------------- #

def test_run_cli_exit_zero_is_stopped_passed(tmp_errorta_home: Path):
    mgr = _make_manager("cli1", _cli_profile(
        ["python", "-c", "print('hello transcript')"]))
    s = mgr.run_cli("default")
    assert s.state in {"starting", "running"}
    assert s.to_dict()["kind"] == "cli_transcript"
    end = _wait_terminal(mgr, s.session_id)
    assert end.state == "stopped" and end.exit_code == 0
    assert end.to_dict()["passed"] is True
    assert end.allocated_ports == []
    assert end.sandbox_backend in {"seatbelt", "bwrap", "none"}
    logs = "\n".join(mgr.get_logs(s.session_id)["lines"])
    assert "hello transcript" in logs


def test_run_cli_nonzero_exit_is_stopped_failed(tmp_errorta_home: Path):
    mgr = _make_manager("cli2", _cli_profile(
        ["python", "-c", "import sys; sys.exit(3)"]))
    end = _wait_terminal(mgr, mgr.run_cli("default").session_id)
    # A completed run that failed its task is stopped, NOT crashed.
    assert end.state == "stopped" and end.exit_code == 3
    assert end.to_dict()["passed"] is False


def test_run_cli_timeout_group_kills_and_marks_timed_out(tmp_errorta_home: Path):
    mgr = _make_manager("cli3", _cli_profile(
        ["python", "-c", "import time; time.sleep(30)"]))
    s = mgr.run_cli("default", timeout_seconds=1)
    end = _wait_terminal(mgr, s.session_id, timeout=20.0)
    assert end.state == "stopped"
    assert end.error == "timed_out"
    assert end.exit_code is None
    assert end.to_dict()["passed"] is False
    # No live entry remains, and the process group is gone.
    assert s.session_id not in rp._LIVE


def test_run_cli_appends_extra_args_argv_style(tmp_errorta_home: Path):
    # Echo argv so the test can see exactly what the program received.
    mgr = _make_manager("cli4", _cli_profile(
        ["python", "-c", "import sys; print('ARGS', sys.argv[1:])"]))
    s = mgr.run_cli("default", args=["--name", "world"])
    end = _wait_terminal(mgr, s.session_id)
    assert end.state == "stopped" and end.exit_code == 0
    logs = "\n".join(mgr.get_logs(s.session_id)["lines"])
    assert "['--name', 'world']" in logs
    assert end.to_dict()["argv"][-2:] == ["--name", "world"]


def test_run_cli_shell_metachars_are_literal_argv(tmp_errorta_home: Path):
    mgr = _make_manager("cli5", _cli_profile(
        ["python", "-c", "import sys; print('GOT', sys.argv[1:])"]))
    # Tokens that a shell would interpret arrive as literal argv (no shell).
    s = mgr.run_cli("default", args=[";", "rm", "-rf", "/"])
    end = _wait_terminal(mgr, s.session_id)
    assert end.state == "stopped" and end.exit_code == 0
    logs = "\n".join(mgr.get_logs(s.session_id)["lines"])
    assert "';'" in logs  # the literal semicolon token, not a shell separator


def test_run_cli_rejects_non_managed_local(tmp_errorta_home: Path):
    mgr = _make_manager("cli6", {
        "kind": "static", "runtime_mode": "static", "start": []})
    with pytest.raises(RuntimeProcessError) as exc:
        mgr.run_cli("default")
    assert str(exc.value) == "cli_run_requires_managed_local"


def test_run_cli_rejects_empty_start(tmp_errorta_home: Path):
    # A managed_local profile with no start is unrunnable; forge it past
    # validation (validation requires start for non-static modes) by editing the
    # stored profile directly.
    mgr = _make_manager("cli7", _cli_profile(["python", "-c", "pass"]))
    import json
    raw = mgr.rstore.get_profile("default").to_dict()
    raw["start"] = []
    (mgr.work_root / "runtime-profiles.json").write_text(
        json.dumps({"default": raw}))
    with pytest.raises(RuntimeProcessError) as exc:
        mgr.run_cli("default")
    assert str(exc.value) == "profile has no start command"


def test_run_cli_unknown_profile_raises(tmp_errorta_home: Path):
    mgr = _make_manager("cli8", _cli_profile(["python", "-c", "pass"]))
    with pytest.raises(RuntimeProcessError) as exc:
        mgr.run_cli("ghost")
    assert str(exc.value) == "profile_not_found"


def test_run_cli_sandbox_none_flags_reduced_isolation(tmp_errorta_home: Path):
    mgr = _make_manager("cli9", _cli_profile(
        ["python", "-c", "pass"], sandbox="none"))
    s = mgr.run_cli("default")
    assert s.sandbox_backend == "none"
    warnings = s.to_dict().get("safety_warnings", [])
    assert any("reduced isolation" in w.lower() for w in warnings)
    _wait_terminal(mgr, s.session_id)


def test_run_cli_explicit_unavailable_sandbox_blocks(tmp_errorta_home, monkeypatch):
    mgr = _make_manager("cli10", _cli_profile(
        ["python", "-c", "pass"], sandbox="docker"))
    from errorta_tools.runner import sandbox as sbx
    monkeypatch.setattr(sbx, "is_available", lambda b: b == "none")
    s = mgr.run_cli("default")
    assert s.state == "crashed"
    assert "sandbox_unavailable_docker" in (s.error or "")
    assert rp.teardown_all() == 0


def test_run_cli_redacts_secrets_in_transcript_and_argv(tmp_errorta_home: Path):
    secret = "sk-ant-" + "A" * 24
    mgr = _make_manager("cli11", _cli_profile(
        ["python", "-c", f"print('leaked {secret}')"]))
    s = mgr.run_cli("default", args=["--token", secret])
    end = _wait_terminal(mgr, s.session_id)
    logs = "\n".join(mgr.get_logs(s.session_id)["lines"])
    assert secret not in logs
    assert all(secret not in tok for tok in end.to_dict()["argv"])


def test_run_cli_per_profile_timeout_from_demo(tmp_errorta_home: Path, monkeypatch):
    captured: dict = {}
    orig = rp._clamp

    def spy(value, lo, hi):
        captured["value"] = value
        return orig(value, lo, hi)

    monkeypatch.setattr(rp, "_clamp", spy)
    mgr = _make_manager("cli12", _cli_profile(
        ["python", "-c", "pass"], demo={"timeout_seconds": 12}))
    s = mgr.run_cli("default")
    _wait_terminal(mgr, s.session_id)
    assert captured["value"] == 12.0


# --- route surface ---------------------------------------------------------- #

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


def test_route_run_cli_happy_path(tmp_errorta_home: Path):
    _route_project("clr1", _cli_profile(["python", "-c", "print('hi')"]))
    c = _client(headers=_TAURI)
    r = c.post("/coding/projects/clr1/runtime/default/run-cli", json={})
    assert r.status_code == 200, r.text
    session = r.json()["session"]
    assert session["kind"] == "cli_transcript"
    assert session["profile_id"] == "default"


def test_route_run_cli_requires_tauri_origin(tmp_errorta_home: Path):
    _route_project("clr2", _cli_profile(["python", "-c", "pass"]))
    c = _client()
    r = c.post("/coding/projects/clr2/runtime/default/run-cli", json={})
    assert r.status_code == 403


def test_route_run_cli_unknown_project_404(tmp_errorta_home: Path):
    c = _client(headers=_TAURI)
    r = c.post("/coding/projects/nope/runtime/default/run-cli", json={})
    assert r.status_code == 404


def test_route_run_cli_no_worktree_409(tmp_errorta_home: Path):
    store = LedgerStore("clr3")
    store.create_project(north_star="n", definition_of_done="d",
                         target="new", repo_path=None)
    c = _client(headers=_TAURI)
    r = c.post("/coding/projects/clr3/runtime/default/run-cli", json={})
    assert r.status_code == 409


def test_route_run_cli_profile_not_found_404(tmp_errorta_home: Path):
    _route_project("clr4", _cli_profile(["python", "-c", "pass"]))
    c = _client(headers=_TAURI)
    r = c.post("/coding/projects/clr4/runtime/ghost/run-cli", json={})
    assert r.status_code == 404


def test_route_run_cli_non_managed_local_400(tmp_errorta_home: Path):
    _route_project("clr5", {"kind": "static", "runtime_mode": "static",
                            "start": []})
    c = _client(headers=_TAURI)
    r = c.post("/coding/projects/clr5/runtime/default/run-cli", json={})
    assert r.status_code == 400
    assert r.json()["detail"] == "cli_run_requires_managed_local"


def test_route_run_cli_unbalanced_quote_422(tmp_errorta_home: Path):
    _route_project("clr6", _cli_profile(["python", "-c", "pass"]))
    c = _client(headers=_TAURI)
    r = c.post("/coding/projects/clr6/runtime/default/run-cli",
               json={"extra_args": 'foo "bar'})
    assert r.status_code == 422


def test_route_run_cli_over_cap_args_422(tmp_errorta_home: Path):
    _route_project("clr7", _cli_profile(["python", "-c", "pass"]))
    c = _client(headers=_TAURI)
    r = c.post("/coding/projects/clr7/runtime/default/run-cli",
               json={"extra_args": "x " * 100})  # 100 tokens > 64 cap
    assert r.status_code == 422


def test_route_run_cli_extra_args_appended(tmp_errorta_home: Path):
    _route_project("clr8", _cli_profile(
        ["python", "-c", "import sys; print('A', sys.argv[1:])"]))
    c = _client(headers=_TAURI)
    r = c.post("/coding/projects/clr8/runtime/default/run-cli",
               json={"extra_args": "--name world"})
    assert r.status_code == 200, r.text
    argv = r.json()["session"]["argv"]
    assert argv[-2:] == ["--name", "world"]


def test_no_mobile_v1_run_cli_route(tmp_errorta_home: Path):
    """The mobile surface never exposes runtime execution — no /mobile/v1 path
    runs a CLI."""
    from errorta_mobile.routes import router as mobile_router
    paths = {route.path for route in mobile_router.routes}
    assert not any("run-cli" in p for p in paths)
    assert not any("runtime" in p for p in paths)


# --- evidence kind (Slice D) ------------------------------------------------ #

def test_cli_transcript_evidence_kind_is_warn_only_head_bound(tmp_errorta_home: Path):
    from errorta_council.coding.runtime import (
        RUNTIME_TEST_KINDS,
        latest_runtime_evidence,
    )
    from errorta_council.coding.testing import run_runtime_test
    assert "cli_transcript" in RUNTIME_TEST_KINDS

    mgr = _make_manager("clev1", _cli_profile(["python", "-c", "print('ok')"]))
    result = run_runtime_test(mgr, "default", "cli_transcript", head="head-1",
                              timeout=10.0)
    assert result.kind == "cli_transcript"
    assert result.passed is True and result.state == "stopped"
    mgr.rstore.record_runtime_test(
        kind=result.kind, profile_id=result.profile_id,
        session_id=result.session_id, passed=result.passed, head="head-1",
        detail=result.detail)

    # Fresh against the head it ran on.
    ev = latest_runtime_evidence(mgr.rstore, current_head="head-1")
    rec = next(r for r in ev["results"] if r["kind"] == "cli_transcript")
    assert rec["passed"] is True and rec["fresh"] is True
    # WARN-only: a head change makes it stale (not fresh) — never a merge blocker.
    ev2 = latest_runtime_evidence(mgr.rstore, current_head="head-2")
    rec2 = next(r for r in ev2["results"] if r["kind"] == "cli_transcript")
    assert rec2["fresh"] is False


def test_cli_transcript_evidence_nonzero_exit_fails(tmp_errorta_home: Path):
    from errorta_council.coding.testing import run_runtime_test
    mgr = _make_manager("clev2", _cli_profile(
        ["python", "-c", "import sys; sys.exit(5)"]))
    result = run_runtime_test(mgr, "default", "cli_transcript", head="h",
                              timeout=10.0)
    assert result.passed is False and result.state == "stopped"
    assert result.detail == "exit 5"


def test_cli_transcript_evidence_timeout_fails(tmp_errorta_home: Path):
    from errorta_council.coding.testing import run_runtime_test
    mgr = _make_manager("clev3", _cli_profile(
        ["python", "-c", "import time; time.sleep(30)"]))
    result = run_runtime_test(mgr, "default", "cli_transcript", head="h",
                              timeout=1.0)
    assert result.passed is False
    assert result.detail == "timed out"


def test_test_route_drives_cli_transcript(tmp_errorta_home: Path):
    _route_project("clev4", _cli_profile(["python", "-c", "print('ok')"]))
    c = _client(headers=_TAURI)
    r = c.post("/coding/projects/clev4/runtime/default/test",
               json={"kind": "cli_transcript"})
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    assert result["kind"] == "cli_transcript"
    assert result["passed"] is True
    # Recorded as evidence.
    from errorta_council.coding.ledger import LedgerStore as LS
    from errorta_council.coding.runtime import RuntimeProfileStore as RPS
    rstore = RPS.for_ledger(LS("clev4"))
    kinds = [t["kind"] for t in rstore.list_runtime_tests()]
    assert "cli_transcript" in kinds


def test_run_cli_default_records_no_evidence(tmp_errorta_home: Path):
    """A plain Run (CLI) via the route records a session only — no graded
    runtime-test verdict (evidence/demo separation, spec D4)."""
    _route_project("clev5", _cli_profile(["python", "-c", "print('ok')"]))
    c = _client(headers=_TAURI)
    r = c.post("/coding/projects/clev5/runtime/default/run-cli", json={})
    assert r.status_code == 200
    from errorta_council.coding.ledger import LedgerStore as LS
    from errorta_council.coding.runtime import RuntimeProfileStore as RPS
    rstore = RPS.for_ledger(LS("clev5"))
    assert rstore.list_runtime_tests() == []
