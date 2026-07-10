"""F137 — Current Focus routes: CRUD, origin guard, accept gate, interjection."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

_TAURI = {"x-errorta-origin": "tauri-ui"}


def _client() -> TestClient:
    from errorta_app.routes import coding as coding_routes
    app = FastAPI()
    app.include_router(coding_routes.router)
    return TestClient(app, headers=_TAURI)


def _project(project_id: str = "proj", *, work_request: str = ""):
    from errorta_council.coding.ledger import LedgerStore
    store = LedgerStore(project_id)
    store.create_project(north_star="ns", definition_of_done="d",
                         target="existing", repo_path="/tmp/x",
                         work_request=work_request)
    return store


def test_create_list_and_reorder(tmp_errorta_home: Path) -> None:
    _project()
    client = _client()
    a = client.post("/coding/projects/proj/focus", json={"title": "first"})
    assert a.status_code == 200
    fid_a = a.json()["focus"]["id"]
    b = client.post("/coding/projects/proj/focus",
                    json={"title": "second", "body": "notes"})
    fid_b = b.json()["focus"]["id"]

    listed = client.get("/coding/projects/proj/focus")
    assert [f["title"] for f in listed.json()["focuses"]] == ["first", "second"]

    r = client.put("/coding/projects/proj/focus/reorder",
                   json={"ordered_ids": [fid_b, fid_a]})
    assert [f["title"] for f in r.json()["focuses"]] == ["second", "first"]


def test_create_requires_title(tmp_errorta_home: Path) -> None:
    _project()
    r = _client().post("/coding/projects/proj/focus", json={"title": ""})
    assert r.status_code == 422


def test_edit_and_direct_archive(tmp_errorta_home: Path) -> None:
    _project()
    client = _client()
    fid = client.post("/coding/projects/proj/focus",
                      json={"title": "x"}).json()["focus"]["id"]
    r = client.put(f"/coding/projects/proj/focus/{fid}",
                   json={"title": "x2", "status": "archived"})
    assert r.status_code == 200
    assert r.json()["focus"]["status"] == "archived"
    # no longer in the active list
    active = client.get("/coding/projects/proj/focus").json()["focuses"]
    assert active == []
    # visible under status=archived
    arch = client.get("/coding/projects/proj/focus?status=archived").json()["focuses"]
    assert [f["title"] for f in arch] == ["x2"]


def test_edit_missing_focus_404(tmp_errorta_home: Path) -> None:
    _project()
    r = _client().put("/coding/projects/proj/focus/focus-nope",
                      json={"title": "y"})
    assert r.status_code == 404


def test_edit_empty_body_422(tmp_errorta_home: Path) -> None:
    _project()
    client = _client()
    fid = client.post("/coding/projects/proj/focus",
                      json={"title": "x"}).json()["focus"]["id"]
    r = client.put(f"/coding/projects/proj/focus/{fid}", json={})
    assert r.status_code == 422


def test_edit_archived_focus_is_read_only(tmp_errorta_home: Path) -> None:
    store = _project()
    focus = store.add_focus(title="history")
    store.update_focus(focus.id, status="archived")
    r = _client().put(
        f"/coding/projects/proj/focus/{focus.id}", json={"status": "active"})
    assert r.status_code == 409


def test_list_rejects_unknown_status(tmp_errorta_home: Path) -> None:
    _project()
    r = _client().get("/coding/projects/proj/focus?status=bogus")
    assert r.status_code == 422


def test_accept_archives_focus(tmp_errorta_home: Path) -> None:
    store = _project()
    fid = store.add_focus(title="ship").id
    store.propose_focus_complete(fid, "done")
    r = _client().post(f"/coding/projects/proj/focus/{fid}/accept")
    assert r.status_code == 200
    assert r.json()["focus"]["status"] == "archived"
    assert store.get_project().status == "active"  # project not completed


def test_accept_active_focus_conflicts(tmp_errorta_home: Path) -> None:
    store = _project()
    fid = store.add_focus(title="not complete").id
    r = _client().post(f"/coding/projects/proj/focus/{fid}/accept")
    assert r.status_code == 409


def test_accept_409_while_running(tmp_errorta_home: Path, monkeypatch) -> None:
    store = _project()
    fid = store.add_focus(title="ship").id
    from errorta_app.routes import coding as coding_routes
    monkeypatch.setattr(coding_routes, "_thread_alive", lambda pid: True)
    r = _client().post(f"/coding/projects/proj/focus/{fid}/accept")
    assert r.status_code == 409


def test_create_requires_tauri_origin(tmp_errorta_home: Path) -> None:
    _project()
    from errorta_app.routes import coding as coding_routes
    app = FastAPI()
    app.include_router(coding_routes.router)
    bare = TestClient(app)
    r = bare.post("/coding/projects/proj/focus", json={"title": "x"})
    assert r.status_code == 403


def test_live_create_delivers_current_focus_interjection(
        tmp_errorta_home: Path, monkeypatch) -> None:
    store = _project()
    from errorta_app.routes import coding as coding_routes
    monkeypatch.setattr(coding_routes, "_thread_alive", lambda pid: True)
    client = _client()
    client.post("/coding/projects/proj/focus", json={"title": "focus A"})
    client.post("/coding/projects/proj/focus", json={"title": "focus B"})
    pending = store.list_unconsumed_interjections()
    cf = [p for p in pending if p.get("kind") == "current_focus"]
    # superseded to exactly one, carrying the full active set
    assert len(cf) == 1
    assert "focus A" in cf[0]["message"] and "focus B" in cf[0]["message"]


def test_live_archive_last_focus_clears_stale_scope(
        tmp_errorta_home: Path, monkeypatch) -> None:
    store = _project()
    focus = store.add_focus(title="old scope")
    from errorta_app.routes import coding as coding_routes
    monkeypatch.setattr(coding_routes, "_thread_alive", lambda pid: True)
    r = _client().put(
        f"/coding/projects/proj/focus/{focus.id}", json={"status": "archived"})
    assert r.status_code == 200
    current = [
        item for item in store.list_unconsumed_interjections()
        if item.get("kind") == "current_focus"
    ]
    assert len(current) == 1
    assert "No active focuses remain" in current[0]["message"]
    assert "old scope" not in current[0]["message"]


def test_list_migrates_legacy_work_request(tmp_errorta_home: Path) -> None:
    _project(work_request="fix the header")
    r = _client().get("/coding/projects/proj/focus")
    assert [f["title"] for f in r.json()["focuses"]] == ["fix the header"]
