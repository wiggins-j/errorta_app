"""F087-13 WS-4 — repo_path (merge-back destination) is validated at create."""
from __future__ import annotations

import subprocess
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from errorta_app.routes import coding as coding_routes


def _client():
    app = FastAPI()
    app.include_router(coding_routes.router)
    return TestClient(app, headers={"x-errorta-origin": "tauri-ui"})


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "myrepo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    return repo


def test_existing_target_requires_repo_path(tmp_errorta_home) -> None:
    c = _client()
    r = c.post("/coding/projects", json={"project_id": "e1", "north_star": "n",
               "definition_of_done": "d", "target": "existing"})
    assert r.status_code == 422


def test_existing_target_rejects_non_git_dir(tmp_errorta_home, tmp_path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    c = _client()
    r = c.post("/coding/projects", json={"project_id": "e2", "north_star": "n",
               "definition_of_done": "d", "target": "existing", "repo_path": str(plain)})
    assert r.status_code == 422
    assert "git" in r.json()["detail"].lower()


def test_existing_target_rejects_sensitive_root(tmp_errorta_home) -> None:
    c = _client()
    r = c.post("/coding/projects", json={"project_id": "e3", "north_star": "n",
               "definition_of_done": "d", "target": "existing", "repo_path": "/etc"})
    assert r.status_code == 422


def test_existing_target_rejects_home_dotdir(tmp_errorta_home) -> None:
    c = _client()
    ssh = Path.home() / ".ssh"
    r = c.post("/coding/projects", json={"project_id": "e4", "north_star": "n",
               "definition_of_done": "d", "target": "existing", "repo_path": str(ssh)})
    assert r.status_code == 422


def test_existing_target_accepts_real_git_repo(tmp_errorta_home, tmp_path) -> None:
    repo = _git_repo(tmp_path)
    c = _client()
    r = c.post("/coding/projects", json={"project_id": "e5", "north_star": "n",
               "definition_of_done": "d", "target": "existing", "repo_path": str(repo)})
    assert r.status_code == 200, r.text


def test_new_target_ignores_repo_path(tmp_errorta_home) -> None:
    c = _client()
    r = c.post("/coding/projects", json={"project_id": "e6", "north_star": "n",
               "definition_of_done": "d", "target": "new", "repo_path": "/etc"})
    assert r.status_code == 200, r.text
