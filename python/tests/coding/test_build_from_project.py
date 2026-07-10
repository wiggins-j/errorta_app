"""F088-03 completion — build a project corpus from the project's OWN workspace.

The external `build_from_repo` path rejects the project's worktree (it's under
the protected ~/.errorta). `build_from_project` uses the project's exported
master tree as a trusted internal source, so a greenfield coding project can
index the team's own code and the PM/devs retrieve it.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from errorta_project_grounding.corpus_binding import (
    CorpusBindingError,
    ProjectCorpusBinding,
    load_binding,
    save_binding,
)


def _ledger(project_id: str):
    from errorta_council.coding.ledger import LedgerStore
    s = LedgerStore(project_id)
    s.create_project(north_star="n", definition_of_done="d", target="new",
                     repo_path=None)
    return s


# --- binding-mode validation -----------------------------------------------

def test_build_from_project_mode_requires_corpus_id_not_source_root(
    tmp_errorta_home: Path,
) -> None:
    s = _ledger("bfp-validate")
    # No source_root needed (the source is the project itself).
    saved = save_binding(s, ProjectCorpusBinding(
        project_id="bfp-validate", mode="build_from_project",
        corpus_id="project-bfp", adapter_source="remote", health_state="ready"))
    assert saved.mode == "build_from_project"
    assert saved.corpus_id == "project-bfp"
    # But a corpus_id is still required.
    with pytest.raises(CorpusBindingError):
        save_binding(s, ProjectCorpusBinding(
            project_id="bfp-validate", mode="build_from_project", corpus_id=None))


# --- the route: export workspace -> bootstrap -> bind build_from_project ----

def _workspace_with_code(project_id: str):
    from errorta_council.coding.ledger import LedgerStore
    from errorta_council.coding.workspace import CodingWorkspace

    store = LedgerStore(project_id)
    store.create_project(north_star="n", definition_of_done="d", target="new",
                         repo_path=None)
    ws = CodingWorkspace(project_id, store)
    ws.setup(target="new", repo_path=None)
    # Put a real source file on master (the "team's merged code").
    ws._ws.write_and_commit("src/app.py", "def add(a, b):\n    return a + b\n")
    return store, ws


def test_build_from_project_empty_master_is_clear_and_leaves_binding_none(
    tmp_errorta_home: Path,
) -> None:
    """A project with nothing merged to master (PRs all stuck unmerged) can't
    build a corpus. The route must say so clearly (409) and NOT leave a broken
    build_from_project binding pointing at a corpus that was never created."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from errorta_app.routes import coding as coding_routes
    from errorta_council.coding.ledger import LedgerStore
    from errorta_council.coding.workspace import CodingWorkspace

    store = LedgerStore("bfp-empty")
    store.create_project(north_star="n", definition_of_done="d", target="new",
                         repo_path=None)
    CodingWorkspace("bfp-empty", store).setup(target="new", repo_path=None)  # only .gitignore on master

    app = FastAPI()
    app.include_router(coding_routes.router)
    client = TestClient(app, headers={"x-errorta-origin": "tauri-ui"})
    resp = client.post("/coding/projects/bfp-empty/grounding/build-from-project", json={})
    assert resp.status_code == 409, resp.text
    assert "nothing to index" in resp.text.lower()
    # The binding is NOT left in a broken build_from_project state.
    assert load_binding(store).mode == "none"


def test_build_from_project_route_indexes_workspace(
    tmp_errorta_home: Path, monkeypatch,
) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from errorta_app.routes import coding as coding_routes
    from errorta_project_grounding import bootstrap as bootstrap_mod

    store, _ws = _workspace_with_code("bfp-route")

    # Fake adapter: record ingested files, no real AIAR.
    ingested: list[str] = []

    class _FakeAdapter:
        def ensure_instance(self, corpus_id):
            return None

        def ingest_file(self, *, corpus_id, path, metadata=None):
            ingested.append(metadata.get("source") if metadata else str(path))

            class _Ref:
                metadata = {"chunks_added": 3, "duplicates": 0}
            return _Ref()

        def publish(self, corpus_id):
            return None

    monkeypatch.setattr(bootstrap_mod, "_active_remote_adapter", lambda: _FakeAdapter())

    app = FastAPI()
    app.include_router(coding_routes.router)
    client = TestClient(app, headers={"x-errorta-origin": "tauri-ui"})

    resp = client.post("/coding/projects/bfp-route/grounding/build-from-project", json={})
    assert resp.status_code == 200, resp.text
    binding = resp.json()["binding"]
    assert binding["mode"] == "build_from_project"
    assert binding["corpus_id"] == "project-bfp-route"  # derived from project id
    # The project's own source file was ingested into the corpus.
    assert any("app.py" in f for f in ingested), ingested
    # Persisted binding is build_from_project (so merge-refresh recognizes it).
    assert load_binding(store).mode == "build_from_project"


def test_editor_save_build_from_project_stays_remote_when_remote_configured(
    tmp_errorta_home: Path, monkeypatch,
) -> None:
    # A manual binding-editor save (PUT corpus-binding) of build_from_project must
    # NOT silently downgrade the corpus to local when a remote AIAR is configured —
    # otherwise its health is probed against a local manifest that never exists.
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from errorta_app.routes import coding as coding_routes
    from errorta_project_grounding import remote_adapter as ra

    store, _ws = _workspace_with_code("bfp-editorsave")
    monkeypatch.setattr(ra, "remote_aiar_config",
                        lambda: ra.RemoteAiarConfig(base_url="http://127.0.0.1:8766"))

    app = FastAPI()
    app.include_router(coding_routes.router)
    client = TestClient(app, headers={"x-errorta-origin": "tauri-ui"})

    resp = client.put(
        "/coding/projects/bfp-editorsave/grounding/corpus-binding",
        json={"mode": "build_from_project", "corpus_id": "project-bfp-editorsave"})
    assert resp.status_code == 200, resp.text
    assert load_binding(store).adapter_source == "remote"


def test_editor_save_build_from_project_local_when_no_remote(
    tmp_errorta_home: Path, monkeypatch,
) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from errorta_app.routes import coding as coding_routes
    from errorta_project_grounding import remote_adapter as ra

    store, _ws = _workspace_with_code("bfp-editorlocal")
    monkeypatch.setattr(ra, "remote_aiar_config", lambda: None)

    app = FastAPI()
    app.include_router(coding_routes.router)
    client = TestClient(app, headers={"x-errorta-origin": "tauri-ui"})

    resp = client.put(
        "/coding/projects/bfp-editorlocal/grounding/corpus-binding",
        json={"mode": "build_from_project", "corpus_id": "project-bfp-editorlocal"})
    assert resp.status_code == 200, resp.text
    assert load_binding(store).adapter_source == "local"


def test_build_from_project_requires_tauri_origin(tmp_errorta_home: Path) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from errorta_app.routes import coding as coding_routes
    _workspace_with_code("bfp-origin")
    app = FastAPI()
    app.include_router(coding_routes.router)
    no_origin = TestClient(app)
    resp = no_origin.post("/coding/projects/bfp-origin/grounding/build-from-project", json={})
    assert resp.status_code == 403


# --- run-end refresh re-ingests merged code into a bound project corpus -----

def test_run_end_refresh_reingests_when_project_corpus_bound(
    tmp_errorta_home: Path, monkeypatch,
) -> None:
    from errorta_council.coding import runner as runner_mod
    store, ws = _workspace_with_code("bfp-refresh")
    save_binding(store, ProjectCorpusBinding(
        project_id="bfp-refresh", mode="build_from_project", corpus_id="project-x",
        adapter_source="remote", health_state="ready"))

    called = {"rebuild": False}

    def fake_rebuild(ledger, workspace, **kw):
        called["rebuild"] = True
        return {"status": "ok", "ingested": 1, "anchored": 1, "superseded": 0}

    import errorta_project_grounding.update_pipeline as up
    monkeypatch.setattr(up, "rebuild_from_repo", fake_rebuild)

    # refresh_corpus=False (per-merge) must NOT re-ingest code.
    runner_mod._sync_grounding(store, ws, refresh_corpus=False)
    assert called["rebuild"] is False
    # refresh_corpus=True (run end) re-ingests the merged code.
    runner_mod._sync_grounding(store, ws, refresh_corpus=True)
    assert called["rebuild"] is True
