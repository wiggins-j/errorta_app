"""F135 S1 — import a local folder as an existing-repo project."""
from __future__ import annotations

import subprocess
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

_TAURI = {"x-errorta-origin": "tauri-ui"}


def _client() -> TestClient:
    from errorta_app.routes import coding as coding_routes
    app = FastAPI()
    app.include_router(coding_routes.router)
    return TestClient(app, headers=_TAURI)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True)


def _git_repo(tmp_path: Path, *, origin: str | None = None) -> Path:
    repo = tmp_path / "myrepo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@t.local")
    _git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("# my repo\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    if origin:
        _git(repo, "remote", "add", "origin", origin)
    return repo


def test_import_local_git_folder(tmp_errorta_home: Path, tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    r = _client().post("/coding/projects/import/local",
                       json={"project_id": "imp1", "folder_path": str(repo)})
    assert r.status_code == 200
    proj = r.json()["project"]
    assert proj["target"] == "existing"
    assert proj["repo_path"] == str(repo.resolve())
    assert proj["import_source"]["kind"] == "local_folder"


def test_import_local_auto_connects_github_origin(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    repo = _git_repo(tmp_path, origin="https://github.com/octocat/hello.git")
    r = _client().post("/coding/projects/import/local",
                       json={"project_id": "imp2", "folder_path": str(repo)})
    assert r.status_code == 200
    assert r.json()["project"]["import_source"]["origin_url"] == \
        "https://github.com/octocat/hello.git"
    # the F102 connection target is populated
    from errorta_council.coding.publish_ledger import PublishLedger
    targets = PublishLedger("imp2").list_targets()
    conn = [t for t in targets if t.kind == "existing_repo_pr"]
    assert conn and conn[0].github_owner == "octocat" and conn[0].github_repo == "hello"


def test_import_local_non_git_requires_git_init(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    folder = tmp_path / "plain"
    folder.mkdir()
    (folder / "a.txt").write_text("hi")
    r = _client().post("/coding/projects/import/local",
                       json={"project_id": "imp3", "folder_path": str(folder)})
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "not_a_git_repo"


def test_import_local_git_init_requires_confirm(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    folder = tmp_path / "plain2"
    folder.mkdir()
    r = _client().post("/coding/projects/import/local",
                       json={"project_id": "imp4", "folder_path": str(folder),
                             "git_init": True})
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "confirm_required"


def test_import_local_git_init_confirmed(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    folder = tmp_path / "plain3"
    folder.mkdir()
    r = _client().post("/coding/projects/import/local",
                       json={"project_id": "imp5", "folder_path": str(folder),
                             "git_init": True, "confirm": True})
    assert r.status_code == 200
    assert (folder / ".git").exists()
    assert r.json()["project"]["import_source"]["kind"] == "local_folder_git_init"


def test_import_local_rejects_protected_root(tmp_errorta_home: Path) -> None:
    r = _client().post("/coding/projects/import/local",
                       json={"project_id": "imp6", "folder_path": "/etc"})
    assert r.status_code == 422


def test_import_local_409_when_project_exists(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    from errorta_council.coding.ledger import LedgerStore
    LedgerStore("imp7").create_project(north_star="", definition_of_done="",
                                       target="new", repo_path=None)
    repo = _git_repo(tmp_path)
    r = _client().post("/coding/projects/import/local",
                       json={"project_id": "imp7", "folder_path": str(repo)})
    assert r.status_code == 409


def test_import_local_requires_tauri_origin(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    from errorta_app.routes import coding as coding_routes
    app = FastAPI()
    app.include_router(coding_routes.router)
    r = TestClient(app).post("/coding/projects/import/local",
                             json={"project_id": "imp8", "folder_path": str(repo)})
    assert r.status_code == 403
