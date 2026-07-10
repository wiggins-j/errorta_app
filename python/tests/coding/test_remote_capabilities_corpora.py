"""F088 enablement Slice 3 — capabilities + corpora reflect the remote AIAR."""
from __future__ import annotations

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from errorta_project_grounding import remote_adapter as ra
from errorta_project_grounding.capabilities import AiarGroundingCapabilities
from errorta_project_grounding.remote_adapter import RemoteAiarConfig, RemoteAiarCorpusAdapter

CFG = RemoteAiarConfig(base_url="http://127.0.0.1:8766", token="t")


def _remote_caps() -> AiarGroundingCapabilities:
    return AiarGroundingCapabilities(
        available=True, version=None, source="remote",
        supports_corpus_ids=True, supports_file_ingest=True, supports_record_ingest=True,
        supports_metadata_filters=False, supports_provenance_metadata=True,
        supports_incremental_refresh=True, supports_supersession=False,
        supports_export_import=False, local_only_embedding=False, notes=())


class _FakeRemote:
    def capabilities(self):
        return _remote_caps()

    def list_instances(self):
        return [{"name": "aerospace", "published": True, "chunk_count": 21394},
                {"name": "errorta-proj", "published": False, "chunk_count": 3}]


def _client() -> TestClient:
    from errorta_app.routes import coding as coding_routes
    app = FastAPI()
    app.include_router(coding_routes.router)
    return TestClient(app, headers={"x-errorta-origin": "tauri-ui"})


# --- adapter.list_instances -------------------------------------------------


def test_list_instances_parses_and_fails_safe() -> None:
    a = RemoteAiarCorpusAdapter(CFG, transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"instances": [{"name": "x"}, "bad"]})))
    assert a.list_instances() == [{"name": "x"}]  # non-dicts dropped

    a2 = RemoteAiarCorpusAdapter(CFG, transport=httpx.MockTransport(
        lambda r: httpx.Response(503, json={})))
    assert a2.list_instances() == []  # fail-safe, never raises


# --- routes reflect the remote when configured ------------------------------


def test_capabilities_route_reports_remote(tmp_errorta_home, monkeypatch) -> None:
    monkeypatch.setattr(ra, "active_remote_adapter", lambda: _FakeRemote())
    c = _client()
    c.post("/coding/projects", json={"project_id": "cap1", "north_star": "n",
           "definition_of_done": "d", "target": "new"})
    body = c.get("/coding/projects/cap1/grounding/capabilities").json()
    assert body["capabilities"]["source"] == "remote"
    assert body["capabilities"]["local_only_embedding"] is False


def test_corpora_route_lists_remote_instances(tmp_errorta_home, monkeypatch) -> None:
    monkeypatch.setattr(ra, "active_remote_adapter", lambda: _FakeRemote())
    body = _client().get("/coding/grounding/corpora").json()
    assert body["source"] == "remote"
    assert {i["name"] for i in body["corpora"]} == {"aerospace", "errorta-proj"}


def test_corpora_route_local_when_unconfigured(tmp_errorta_home, monkeypatch) -> None:
    monkeypatch.setattr(ra, "active_remote_adapter", lambda: None)
    r = _client().get("/coding/grounding/corpora")
    assert r.status_code == 200 and r.json()["source"] == "local"
