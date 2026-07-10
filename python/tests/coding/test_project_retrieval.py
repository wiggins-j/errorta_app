"""F088 enablement Slice 4 — retrieval over a project's bound corpus."""
from __future__ import annotations

from pathlib import Path

from errorta_council.coding.ledger import LedgerStore
from errorta_project_grounding import retrieval
from errorta_project_grounding.adapter import GroundingHit
from errorta_project_grounding.corpus_binding import ProjectCorpusBinding, save_binding


class _FakeAdapter:
    def __init__(self, *, hits=None, raises=None):
        self.hits = hits or []
        self.raises = raises
        self.calls = []

    def retrieve(self, *, corpus_id, query, top_k, filters=None):
        self.calls.append((corpus_id, query, top_k))
        if self.raises:
            raise self.raises
        return self.hits


def _bound(tmp: Path, pid: str, *, corpus_id="proj-corpus", remote=True) -> LedgerStore:
    s = LedgerStore(pid, root=tmp)
    s.create_project(north_star="n", definition_of_done="d", target="new", repo_path=None)
    save_binding(s, ProjectCorpusBinding(
        project_id=pid, mode="existing", corpus_id=corpus_id,
        adapter_source="remote" if remote else "local", health_state="ready"))
    return s


def _use(monkeypatch, adapter):
    monkeypatch.setattr(retrieval, "_adapter_for_project", lambda: adapter)


def test_retrieves_from_bound_corpus(tmp_path, monkeypatch) -> None:
    s = _bound(tmp_path, "r1")
    fake = _FakeAdapter(hits=[GroundingHit(content="calc.add", corpus_id="proj-corpus",
                                           chunk_id="c1", score=0.9)])
    _use(monkeypatch, fake)
    hits = retrieval.retrieve_project_corpus(s, query="how to add", top_k=5)
    assert [h.content for h in hits] == ["calc.add"]
    assert fake.calls == [("proj-corpus", "how to add", 5)]


def test_unbound_corpus_returns_empty(tmp_path, monkeypatch) -> None:
    s = LedgerStore("r2", root=tmp_path)
    s.create_project(north_star="n", definition_of_done="d", target="new", repo_path=None)
    fake = _FakeAdapter(hits=[GroundingHit(content="x", corpus_id="c", chunk_id="c1")])
    _use(monkeypatch, fake)
    assert retrieval.retrieve_project_corpus(s, query="q") == []
    assert fake.calls == []  # never queried — no corpus bound


def test_retrieval_error_is_fail_safe(tmp_path, monkeypatch) -> None:
    s = _bound(tmp_path, "r3")
    _use(monkeypatch, _FakeAdapter(raises=RuntimeError("remote down")))
    assert retrieval.retrieve_project_corpus(s, query="q") == []  # degrades, never raises


def test_empty_query_returns_empty(tmp_path, monkeypatch) -> None:
    s = _bound(tmp_path, "r4")
    fake = _FakeAdapter(hits=[GroundingHit(content="x", corpus_id="c", chunk_id="c1")])
    _use(monkeypatch, fake)
    assert retrieval.retrieve_project_corpus(s, query="   ") == []
    assert fake.calls == []


# --- route ------------------------------------------------------------------


def test_retrieve_route_returns_hits(tmp_errorta_home, monkeypatch) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from errorta_app.routes import coding as coding_routes

    _use(monkeypatch, _FakeAdapter(hits=[GroundingHit(content="evidence", corpus_id="c",
                                                      chunk_id="c1", score=0.7)]))
    app = FastAPI(); app.include_router(coding_routes.router)
    c = TestClient(app, headers={"x-errorta-origin": "tauri-ui"})
    c.post("/coding/projects", json={"project_id": "rt", "north_star": "n",
           "definition_of_done": "d", "target": "new"})
    save_binding(LedgerStore("rt"), ProjectCorpusBinding(
        project_id="rt", mode="existing", corpus_id="rt-corpus",
        adapter_source="remote", health_state="ready"))
    body = c.get("/coding/projects/rt/grounding/retrieve", params={"q": "find", "k": 3}).json()
    assert body["hits"][0]["content"] == "evidence"
    # F088-10: the route also reports the retrieval status so the UI can tell
    # "served, has matches" apart from no_corpus / unavailable / empty.
    assert body["status"] == "ok"


# --- P1 fix: no local corpus fallback under remote residency -----------------


def test_adapter_fails_closed_under_remote_residency(tmp_errorta_home, monkeypatch) -> None:
    from errorta_residency import config as residency_config
    monkeypatch.delenv("ERRORTA_AIAR_REMOTE_URL", raising=False)
    residency_config.update(mode="ssh-remote", ssh_host="example-host",
                            remote_sidecar_port=8770, local_tunnel_port=18770)
    # no remote AIAR + remote residency -> None (never the local adapter)
    assert retrieval._adapter_for_project() is None


def test_retrieve_route_fails_closed_under_remote_residency(tmp_errorta_home, monkeypatch) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from errorta_residency import config as residency_config
    from errorta_app.routes import coding as coding_routes

    monkeypatch.delenv("ERRORTA_AIAR_REMOTE_URL", raising=False)
    app = FastAPI(); app.include_router(coding_routes.router)
    c = TestClient(app, headers={"x-errorta-origin": "tauri-ui"})
    c.post("/coding/projects", json={"project_id": "res", "north_star": "n",
           "definition_of_done": "d", "target": "new"})
    residency_config.update(mode="ssh-remote", ssh_host="example-host",
                            remote_sidecar_port=8770, local_tunnel_port=18770)
    r = c.get("/coding/projects/res/grounding/retrieve", params={"q": "x"})
    assert r.status_code == 409  # local corpus read refused under remote residency
