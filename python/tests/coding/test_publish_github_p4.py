"""F102 Slice D — P4 new GitHub repo (orchestrator + route, egress mocked)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_TAURI = {"x-errorta-origin": "tauri-ui"}


def _client() -> TestClient:
    from errorta_app.routes import coding as coding_routes
    app = FastAPI()
    app.include_router(coding_routes.router)
    return TestClient(app, headers=_TAURI)


def _allow_gate(monkeypatch: pytest.MonkeyPatch, *, allowed: bool = True) -> None:
    @dataclass
    class _Gate:
        allowed: bool
        blockers: list

    monkeypatch.setattr(
        "errorta_council.coding.evidence.merge_review",
        lambda store, ws: {"_gate": _Gate(allowed, []),
                           "gate": {"allowed": allowed}})


def _new_project(project_id: str, *, delivered: bool = True, secret: bool = False):
    from errorta_council.coding.ledger import LedgerStore
    from errorta_council.coding.workspace import CodingWorkspace
    store = LedgerStore(project_id)
    store.create_project(north_star="ns", definition_of_done="d",
                         target="new", repo_path=None)
    ws = CodingWorkspace(project_id, store)
    ws.setup(target="new", repo_path=None)
    if secret:
        ws.write_file("config.py",
                      "KEY='ghp_0123456789abcdefghij0123456789abcd'\n", task_id="t1")
    else:
        ws.write_file("main.py", "print('hello')\n", task_id="t1")
    if delivered:
        store.record_decision(title="delivered", context="merge-back",
                              choice="delivered", rationale="delivered")
    return store, ws


def _mock_repo_create(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    cap: dict[str, Any] = {}
    from errorta_tools.runner import publish as egress

    def _create(name, *, private=True, source_dir, push=True):  # noqa: ANN001
        cap["name"] = name
        cap["private"] = private
        cap["push"] = push
        return {"repo_url": f"https://github.com/me/{name}"}
    monkeypatch.setattr(egress, "gh_repo_create", _create)
    return cap


def test_p4_private_by_default(tmp_errorta_home: Path,
                               monkeypatch: pytest.MonkeyPatch) -> None:
    _new_project("p4-ok")
    _allow_gate(monkeypatch)
    cap = _mock_repo_create(monkeypatch)
    resp = _client().post("/coding/projects/p4-ok/publish/new-github-repo",
                          json={"repo_name": "my-new-repo"})
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["repo_url"] == "https://github.com/me/my-new-repo"
    assert out["private"] is True
    assert cap["private"] is True and cap["push"] is True
    assert "main.py" in out["initial_files"]
    states = [e["state"] for e in out["events"]]
    assert "scanned" in states and "committed" in states and "pushed" in states


def test_p4_local_only(tmp_errorta_home: Path,
                       monkeypatch: pytest.MonkeyPatch) -> None:
    _new_project("p4-local")
    _allow_gate(monkeypatch)
    # gh_repo_create must NOT be called on the local_only path.
    from errorta_tools.runner import publish as egress

    def _boom(*a, **k):  # noqa: ANN001, ANN002
        raise AssertionError("gh_repo_create must not run for local_only")
    monkeypatch.setattr(egress, "gh_repo_create", _boom)

    resp = _client().post("/coding/projects/p4-local/publish/new-github-repo",
                          json={"repo_name": "localrepo", "local_only": True})
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["local_only"] is True
    assert out["local_path"]
    assert Path(out["local_path"]).exists()
    assert (Path(out["local_path"]) / ".git").exists()


def test_p4_scan_hit_blocks_then_override(tmp_errorta_home: Path,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    _new_project("p4-scan", secret=True)
    _allow_gate(monkeypatch)
    _mock_repo_create(monkeypatch)

    resp = _client().post("/coding/projects/p4-scan/publish/new-github-repo",
                          json={"repo_name": "scanrepo"})
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "secret_scan_hit"
    assert "ghp_0123456789" not in resp.text

    resp2 = _client().post("/coding/projects/p4-scan/publish/new-github-repo",
                           json={"repo_name": "scanrepo", "override": True})
    assert resp2.status_code == 200, resp2.text
    assert resp2.json()["repo_url"]


def test_p4_not_delivered_409(tmp_errorta_home: Path,
                              monkeypatch: pytest.MonkeyPatch) -> None:
    _new_project("p4-undelivered", delivered=False)
    _allow_gate(monkeypatch)
    _mock_repo_create(monkeypatch)
    resp = _client().post("/coding/projects/p4-undelivered/publish/new-github-repo",
                          json={"repo_name": "x"})
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "not_delivered"


def test_p4_repo_name_validation(tmp_errorta_home: Path,
                                 monkeypatch: pytest.MonkeyPatch) -> None:
    _new_project("p4-badname")
    _allow_gate(monkeypatch)
    _mock_repo_create(monkeypatch)
    resp = _client().post("/coding/projects/p4-badname/publish/new-github-repo",
                          json={"repo_name": "--public"})
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "invalid_repo_name"


def test_p4_requires_tauri_origin(tmp_errorta_home: Path,
                                  monkeypatch: pytest.MonkeyPatch) -> None:
    _new_project("p4-origin")
    from errorta_app.routes import coding as coding_routes
    app = FastAPI()
    app.include_router(coding_routes.router)
    resp = TestClient(app).post(
        "/coding/projects/p4-origin/publish/new-github-repo",
        json={"repo_name": "x"})
    assert resp.status_code == 403


def test_publish_routes_not_under_mobile_v1() -> None:
    from errorta_app.routes import coding as coding_routes
    paths = [getattr(r, "path", "") for r in coding_routes.router.routes]
    publish_paths = [p for p in paths if "publish/existing-repo-pr" in p
                     or "publish/new-github-repo" in p]
    assert publish_paths, "publish routes must exist"
    assert all(not p.startswith("/mobile/v1") for p in publish_paths)
