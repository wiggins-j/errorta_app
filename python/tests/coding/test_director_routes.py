"""F118-01 — Director CRUD + aggregation routes."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from errorta_council.coding import attention
from errorta_council.coding.ledger import LedgerStore

TAURI = {"x-errorta-origin": "tauri-ui"}
AGENT = {"gateway_route_id": "fake.local.deterministic", "provider_kind": "local"}


@pytest.fixture
def client(tmp_errorta_home):
    from errorta_app.routes import coding as coding_routes
    app = FastAPI()
    app.include_router(coding_routes.router)
    return TestClient(app, headers=TAURI)


def _create(client, name="A", project_ids=None):
    return client.post("/coding/directors", json={
        "name": name, "agent": AGENT, "project_ids": project_ids or []})


def _project(pid: str) -> LedgerStore:
    store = LedgerStore(pid)
    store.create_project(north_star="n", definition_of_done="d", target="new",
                         repo_path=None)
    return store


def test_director_crud(client):
    created = _create(client, "Boss", ["p1", "p2"])
    assert created.status_code == 200, created.text
    did = created.json()["director"]["id"]

    listed = client.get("/coding/directors").json()
    assert [d["id"] for d in listed["directors"]] == [did]

    got = client.get(f"/coding/directors/{did}")
    assert got.status_code == 200
    assert got.json()["director"]["name"] == "Boss"
    assert "attention" in got.json()

    upd = client.put(f"/coding/directors/{did}", json={"name": "Boss2"})
    assert upd.status_code == 200 and upd.json()["director"]["name"] == "Boss2"

    assert client.delete(f"/coding/directors/{did}").status_code == 200
    assert client.get(f"/coding/directors/{did}").status_code == 404


def test_ownership_conflict_returns_409(client):
    _create(client, "A", ["p1"])
    dup = _create(client, "B", ["p1"])
    assert dup.status_code == 409
    # update grabbing an owned project also 409
    b = _create(client, "B", ["p2"]).json()["director"]["id"]
    assert client.put(f"/coding/directors/{b}",
                      json={"project_ids": ["p2", "p1"]}).status_code == 409


def test_invalid_project_id_returns_422(client):
    bad = _create(client, "A", ["bad/project"])
    assert bad.status_code == 422


def test_owner_gated(client):
    no_origin = TestClient(client.app)  # no Tauri origin
    assert no_origin.get("/coding/directors").status_code == 403
    assert no_origin.post("/coding/directors", json={"name": "x"}).status_code == 403
    d = _create(client).json()["director"]["id"]
    assert no_origin.get(f"/coding/directors/{d}").status_code == 403
    assert no_origin.get(f"/coding/directors/{d}/attention").status_code == 403
    assert no_origin.get(f"/coding/directors/{d}/inbox").status_code == 403
    assert no_origin.put(f"/coding/directors/{d}", json={"name": "y"}).status_code == 403
    assert no_origin.delete(f"/coding/directors/{d}").status_code == 403


def test_aggregation_route(client):
    store = _project("proj-x")
    attention.raise_signal("proj-x", kind="problem", source="pm", stage="drafting_spec",
                           title="t", summary="s", pm_evaluation="e",
                           suggestions=[{"id": "s1", "label": "x"}], store=store)
    did = _create(client, "A", ["proj-x"]).json()["director"]["id"]
    agg = client.get(f"/coding/directors/{did}/attention")
    assert agg.status_code == 200
    groups = agg.json()["groups"]
    assert groups[0]["project_id"] == "proj-x"
    assert groups[0]["signals"][0]["kind"] == "problem"


def test_unknown_director_404(client):
    assert client.get("/coding/directors/dir-nope").status_code == 404
    assert client.get("/coding/directors/dir-nope/attention").status_code == 404
    assert client.delete("/coding/directors/dir-nope").status_code == 404


def test_invalid_director_id_is_controlled_422(client):
    assert client.get("/coding/directors/bad%5Cid").status_code == 422
    assert client.delete("/coding/directors/bad%5Cid").status_code == 422


def test_inbox_route(client):
    sa = _project("inbox-proj")
    attention.raise_signal("inbox-proj", kind="problem", source="pm",
                           stage="drafting_spec", title="t", summary="s",
                           pm_evaluation="e", suggestions=[{"id": "s1", "label": "x"}],
                           store=sa)
    did = _create(client, "A", ["inbox-proj"]).json()["director"]["id"]
    r = client.get(f"/coding/directors/{did}/inbox")
    assert r.status_code == 200
    items = r.json()["items"]
    assert items[0]["project_id"] == "inbox-proj"
    assert items[0]["signal"]["kind"] == "problem"
