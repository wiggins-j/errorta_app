"""F101-03 S1 — the universal Run front door route (`POST /runtime/run`).

Covers the consent-preview vs execute split, grounded-or-refuse (an ungrounded
project resolves to a checklist and NEVER spawns a process), the Tauri-origin
guard, and that an existing CLI profile still starts through the new dispatch.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from errorta_council.coding import runtime_process as rp
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.runtime import RuntimeProfileStore
from errorta_council.coding.workspace import CodingWorkspace

TAURI = {"x-errorta-origin": "tauri-ui"}


@pytest.fixture(autouse=True)
def _fast_and_clean(monkeypatch):
    monkeypatch.setattr(rp, "_POLL_INTERVAL", 0.05)
    monkeypatch.setattr(rp, "_GRACE_SECONDS", 1.0)
    yield
    rp.teardown_all()


def _client() -> TestClient:
    from errorta_app.server import app
    return TestClient(app, headers=TAURI)


def _make_project(project_id: str) -> CodingWorkspace:
    store = LedgerStore(project_id)
    store.create_project(north_star="n", definition_of_done="d",
                         target="new", repo_path=None)
    ws = CodingWorkspace(project_id, store)
    ws.setup(target="new", repo_path=None)
    return ws


def _wait_terminal(client, pid, sid, timeout=15.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        r = client.get(f"/coding/projects/{pid}/runtime/sessions/{sid}")
        assert r.status_code == 200, r.text
        sess = r.json()["session"]
        last = sess["state"]
        if last in {"stopped", "crashed", "healthy", "stopped_error"}:
            return sess
        time.sleep(0.05)
    raise AssertionError(f"session {sid} never terminated; last={last}")


# --------------------------------------------------------------------------- #
def test_run_preview_is_grounded_and_does_not_execute(tmp_errorta_home: Path):
    ws = _make_project("run-preview")
    (ws.root() / "main.py").write_text("print('hi')\n")
    client = _client()

    r = client.post("/coding/projects/run-preview/runtime/run", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["resolved"] is True and body["runnable"] is True
    assert body["session"] is None  # preview only — nothing ran
    plan = body["plan"]
    assert plan["modality"] == "cli"
    assert plan["grounded_by"] == "detector"
    assert "main.py" in plan["verified_paths"]
    assert plan["trust_tier"] == 0

    # No session was created by a mere preview.
    store = LedgerStore("run-preview")
    assert RuntimeProfileStore.for_ledger(store).list_sessions() == []


def test_run_refuses_ungrounded_and_never_spawns(tmp_errorta_home: Path):
    # The reddit case at the route: a stored profile advertising `npm run dev`
    # for a project with no package.json. Even with confirm:true, Run resolves to
    # a checklist and spawns nothing.
    ws = _make_project("run-reddit")
    (ws.root() / "Navigation.tsx").write_text("export const Nav = () => null;\n")
    store = LedgerStore("run-reddit")
    rstore = RuntimeProfileStore.for_ledger(store)
    from errorta_council.coding.runtime import RuntimeProfile
    rstore.upsert_profile(RuntimeProfile(
        profile_id="default", project_id="run-reddit", kind="web",
        runtime_mode="managed_local", start=["npm", "run", "dev"]))
    client = _client()

    r = client.post("/coding/projects/run-reddit/runtime/run",
                    json={"confirm": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["resolved"] is False and body["runnable"] is False
    assert body["reason"] == "unresolved"
    assert body["session"] is None
    assert any("package.json" in line for line in body["looked_for"])

    # Grounded-or-refuse: nothing was ever executed.
    assert rstore.list_sessions() == []


def test_run_ignores_ad_hoc_runtime_profile_json(tmp_errorta_home: Path):
    ws = _make_project("run-adhoc")
    (ws.root() / ".runtime-profile.json").write_text(
        '{"start": ["npm", "run", "dev"], "kind": "web"}')
    (ws.root() / "Navigation.tsx").write_text("export const Nav = () => null;\n")
    client = _client()

    r = client.post("/coding/projects/run-adhoc/runtime/run",
                    json={"confirm": True})
    assert r.status_code == 200, r.text
    assert r.json()["resolved"] is False


def test_run_no_worktree_is_checklist_not_error(tmp_errorta_home: Path):
    # Project exists but no worktree was set up -> honest checklist, not a 409.
    store = LedgerStore("run-noworktree")
    store.create_project(north_star="n", definition_of_done="d",
                         target="new", repo_path=None)
    client = _client()

    r = client.post("/coding/projects/run-noworktree/runtime/run", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["resolved"] is False and body["reason"] == "no_worktree"
    assert body["plan"] is None


def test_run_confirm_executes_cli_through_new_dispatch(tmp_errorta_home: Path):
    ws = _make_project("run-cli")
    (ws.root() / "main.py").write_text("print('hello from run')\n")
    client = _client()

    r = client.post("/coding/projects/run-cli/runtime/run", json={"confirm": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["resolved"] is True and body["runnable"] is True
    session = body["session"]
    assert session is not None
    sid = session["session_id"]

    final = _wait_terminal(client, "run-cli", sid)
    assert final["state"] == "stopped"
    # The detector-grounded profile was persisted so the dispatch could run it.
    store = LedgerStore("run-cli")
    profiles = RuntimeProfileStore.for_ledger(store).list_profiles()
    assert any(p.profile_id == "default" for p in profiles)


def test_run_requires_tauri_origin(tmp_errorta_home: Path):
    _make_project("run-origin")
    from errorta_app.server import app
    nope = TestClient(app)  # no x-errorta-origin
    r = nope.post("/coding/projects/run-origin/runtime/run", json={})
    assert r.status_code == 403


def test_run_unknown_project_is_404(tmp_errorta_home: Path):
    client = _client()
    r = client.post("/coding/projects/does-not-exist/runtime/run", json={})
    assert r.status_code == 404
