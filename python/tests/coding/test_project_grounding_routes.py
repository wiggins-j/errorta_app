from pathlib import Path

from fastapi.testclient import TestClient


def _client(tmp_errorta_home: Path) -> TestClient:
    from errorta_app.server import app

    return TestClient(app, headers={"x-errorta-origin": "tauri-ui"})


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".git").mkdir()
    (root / "README.md").write_text("# project\n", encoding="utf-8")
    return root


def test_grounding_corpora_route_lists_existing_corpora(
    tmp_errorta_home: Path,
    isolated_manifest_locks,
) -> None:
    from errorta_corpus.manifest import FileEntry, upsert_entry

    upsert_entry(
        "alpha",
        FileEntry(
            file_id="f1",
            original_path="a.md",
            copied_path="a.md",
            sha256="s",
            size_bytes=1,
            mime_ext=".md",
            status="ready",
        ),
    )

    got = _client(tmp_errorta_home).get("/coding/grounding/corpora").json()["corpora"]

    # F095: the route now delegates to the unified catalog, which normalizes
    # every entry with a status + source so all three consumers agree.
    alpha = next(c for c in got if c["name"] == "alpha")
    assert alpha["file_count"] == 1
    assert alpha["ready_count"] == 1
    assert alpha["status"] == "ready"
    assert alpha["source"] == "local"
    assert alpha["unit"] == "files"
    assert alpha["capabilities"]["list_files"] is True


def test_project_get_includes_default_grounding_status(tmp_errorta_home: Path) -> None:
    client = _client(tmp_errorta_home)
    client.post("/coding/projects", json={"project_id": "p", "north_star": "n", "target": "new"})

    project = client.get("/coding/projects/p").json()["project"]

    assert project["grounding"]["mode"] == "none"
    assert project["grounding"]["health_state"] == "missing"


def test_put_corpus_binding_is_guarded_and_persists(
    tmp_errorta_home: Path,
    isolated_manifest_locks,
) -> None:
    from errorta_corpus.manifest import FileEntry, upsert_entry

    client = _client(tmp_errorta_home)
    client.post("/coding/projects", json={"project_id": "pbind", "north_star": "n", "target": "new"})
    upsert_entry(
        "alpha",
        FileEntry(
            file_id="f1",
            original_path="a.md",
            copied_path="a.md",
            sha256="s",
            size_bytes=1,
            mime_ext=".md",
            status="ready",
        ),
    )

    resp = client.put(
        "/coding/projects/pbind/grounding/corpus-binding",
        json={"mode": "existing", "corpus_id": "alpha"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["binding"]["corpus_id"] == "alpha"
    assert client.get("/coding/projects/pbind/grounding/corpus-binding").json()["binding"]["health_state"] == "ready"


def test_put_corpus_binding_rejects_build_from_repo_without_corpus_id(
    tmp_errorta_home: Path,
    isolated_manifest_locks,
) -> None:
    # F088-10: the PUT path must validate too — an empty corpus_id previously
    # reached start_project_bootstrap and built a corpus literally named "None".
    client = _client(tmp_errorta_home)
    client.post("/coding/projects", json={"project_id": "pnone", "north_star": "n", "target": "new"})
    resp = client.put(
        "/coding/projects/pnone/grounding/corpus-binding",
        json={"mode": "build_from_repo", "source_root": "/tmp"},
    )
    assert resp.status_code == 422, resp.text
    assert "corpus_id" in resp.text


def test_create_project_can_bootstrap_from_repo(
    tmp_path: Path,
    tmp_errorta_home: Path,
    isolated_manifest_locks,
) -> None:
    repo = _repo(tmp_path)
    client = _client(tmp_errorta_home)

    resp = client.post(
        "/coding/projects",
        json={
            "project_id": "pboot",
            "north_star": "n",
            "target": "existing",
            "repo_path": str(repo),
            "grounding": {
                "mode": "build_from_repo",
                "corpus_id": "pboot-corpus",
            },
        },
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["bootstrap"]["status"] == "done"
    job_id = body["bootstrap"]["job_id"]
    assert client.get(f"/coding/projects/pboot/grounding/bootstrap/{job_id}").status_code == 200


def test_mutating_grounding_routes_require_tauri_origin(tmp_errorta_home: Path) -> None:
    from errorta_app.server import app

    no_origin = TestClient(app)
    client = _client(tmp_errorta_home)
    client.post("/coding/projects", json={"project_id": "porigin", "north_star": "n", "target": "new"})

    resp = no_origin.put(
        "/coding/projects/porigin/grounding/corpus-binding",
        json={"mode": "none"},
    )

    assert resp.status_code == 403
