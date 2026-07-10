"""F101 S5 — demo-repair loop: failed runtime -> context-rich dev task."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.runtime import (
    RuntimeProfileStore,
    RuntimeSession,
    build_repair_brief,
    validate_profile,
)
from errorta_council.coding.workspace import CodingWorkspace

_TAURI = {"x-errorta-origin": "tauri-ui"}


def _client(*, headers=None) -> TestClient:
    from errorta_app.routes import coding as coding_routes
    app = FastAPI()
    app.include_router(coding_routes.router)
    return TestClient(app, headers=headers)


def _project(project_id: str, *, workspace=True) -> LedgerStore:
    store = LedgerStore(project_id)
    store.create_project(north_star="n", definition_of_done="d",
                         target="new", repo_path=None)
    if workspace:
        ws = CodingWorkspace(project_id, store)
        ws.setup(target="new", repo_path=None)
    return store


def _profile(project_id: str):
    return validate_profile(
        {"kind": "web", "runtime_mode": "managed_local",
         "setup": [["npm", "install"]], "start": ["npm", "run", "dev"],
         "health": {"type": "http", "url": "http://127.0.0.1:{port}",
                    "timeout_seconds": 20}, "sandbox": "auto"},
        profile_id="default", project_id=project_id)


# --- build_repair_brief (pure) --------------------------------------------- #

def test_brief_includes_commands_session_and_logs():
    profile = _profile("p")
    session = RuntimeSession(
        session_id="rs-1", profile_id="default", state="crashed",
        sandbox_backend="seatbelt", exit_code=1, error="spawn_failed: boom")
    title, detail = build_repair_brief(
        profile=profile, session=session,
        log_lines=["line one", "ERROR something broke"])
    assert "Fix runtime preview: default (crashed)" == title
    assert "npm run dev" in detail
    assert "npm install" in detail
    assert "state=crashed" in detail and "spawn_failed: boom" in detail
    assert "ERROR something broke" in detail
    assert "Reviewer:" in detail


def test_brief_caps_log_tail():
    title, detail = build_repair_brief(
        profile=None, session=None,
        log_lines=[f"l{i}" for i in range(200)], max_log_lines=5)
    assert "l199" in detail and "l194" not in detail  # only last 5 kept
    assert "Fix runtime preview: default" == title


def test_brief_handles_missing_profile_and_session():
    title, detail = build_repair_brief(profile=None, session=None, log_lines=[])
    assert title == "Fix runtime preview: default"
    assert "Reviewer:" in detail


# --- route ------------------------------------------------------------------ #

def test_repair_route_creates_dev_task_with_context(tmp_errorta_home: Path):
    store = _project("rep1")
    rstore = RuntimeProfileStore.for_ledger(store)
    rstore.upsert_profile(_profile("rep1"))
    # a crashed session for context
    rstore.append_session(RuntimeSession(
        session_id="rs-crash", profile_id="default", state="crashed",
        sandbox_backend="seatbelt", exit_code=1, error="spawn_failed: boom",
        log_ref="runtime-logs/rs-crash.log"))

    c = _client(headers=_TAURI)
    r = c.post("/coding/projects/rep1/runtime/default/repair", json={})
    assert r.status_code == 200, r.text
    task = r.json()["task"]
    assert task["role"] == "dev"
    assert task["title"].startswith("Fix runtime preview: default")
    assert "npm run dev" in task["detail"]
    assert "spawn_failed: boom" in task["detail"]
    # the task is actually on the backlog
    assert any(t.task_id == task["task_id"] for t in store.list_tasks(role="dev"))


def test_repair_route_binds_named_session(tmp_errorta_home: Path):
    store = _project("rep2")
    rstore = RuntimeProfileStore.for_ledger(store)
    rstore.upsert_profile(_profile("rep2"))
    rstore.append_session(RuntimeSession(session_id="rs-old", profile_id="default",
                                         state="stopped"))
    rstore.append_session(RuntimeSession(session_id="rs-new", profile_id="default",
                                         state="crashed", error="picked"))
    c = _client(headers=_TAURI)
    r = c.post("/coding/projects/rep2/runtime/default/repair",
               json={"session_id": "rs-old"})
    assert r.status_code == 200
    assert "rs-old" in r.json()["task"]["detail"]
    assert "picked" not in r.json()["task"]["detail"]


def test_repair_route_unknown_profile_404(tmp_errorta_home: Path):
    _project("rep3")
    c = _client(headers=_TAURI)
    r = c.post("/coding/projects/rep3/runtime/ghost/repair", json={})
    assert r.status_code == 404


def test_repair_route_requires_tauri_origin(tmp_errorta_home: Path):
    store = _project("rep4")
    RuntimeProfileStore.for_ledger(store).upsert_profile(_profile("rep4"))
    c = _client()
    r = c.post("/coding/projects/rep4/runtime/default/repair", json={})
    assert r.status_code == 403


def test_repair_route_no_session_still_works(tmp_errorta_home: Path):
    store = _project("rep5")
    RuntimeProfileStore.for_ledger(store).upsert_profile(_profile("rep5"))
    c = _client(headers=_TAURI)
    r = c.post("/coding/projects/rep5/runtime/default/repair", json={})
    assert r.status_code == 200
    assert r.json()["task"]["title"] == "Fix runtime preview: default"
