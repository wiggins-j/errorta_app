"""F135 S2 — GitHub clone import (egress mocked, no real network)."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_TAURI = {"x-errorta-origin": "tauri-ui"}


def _client() -> TestClient:
    from errorta_app.routes import coding as coding_routes
    app = FastAPI()
    app.include_router(coding_routes.router)
    return TestClient(app, headers=_TAURI)


def test_auth_status_is_project_less(tmp_errorta_home: Path,
                                     monkeypatch: pytest.MonkeyPatch) -> None:
    from errorta_tools.runner import publish as egress
    monkeypatch.setattr(egress, "gh_auth_status",
                        lambda: {"gh_present": True, "login": "octocat"})
    r = _client().get("/coding/projects/import/github/auth-status")
    assert r.status_code == 200
    body = r.json()
    assert body["gh_present"] is True and body["login"] == "octocat"
    assert "token" not in str(body).lower() or "token_in_keychain" in body


def test_clone_rejects_non_github_url(tmp_errorta_home: Path,
                                      monkeypatch: pytest.MonkeyPatch) -> None:
    from errorta_tools.runner import publish as egress
    monkeypatch.setattr(egress, "get_gh_binary", lambda: "/usr/bin/gh")
    r = _client().post("/coding/projects/import/github/clone",
                       json={"project_id": "g1",
                             "repo_url": "https://evil.com/o/r"})
    assert r.status_code == 400


def test_clone_disabled_when_gh_absent(tmp_errorta_home: Path,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
    from errorta_tools.runner import publish as egress
    monkeypatch.setattr(egress, "get_gh_binary", lambda: None)
    r = _client().post("/coding/projects/import/github/clone",
                       json={"project_id": "g2",
                             "repo_url": "https://github.com/o/r"})
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "gh_not_connected"


def test_clone_409_when_project_exists(tmp_errorta_home: Path,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
    from errorta_council.coding.ledger import LedgerStore
    from errorta_tools.runner import publish as egress
    LedgerStore("g3").create_project(north_star="", definition_of_done="",
                                     target="new", repo_path=None)
    monkeypatch.setattr(egress, "get_gh_binary", lambda: "/usr/bin/gh")
    r = _client().post("/coding/projects/import/github/clone",
                       json={"project_id": "g3",
                             "repo_url": "https://github.com/o/r"})
    assert r.status_code == 409


def test_clone_job_creates_connected_project(tmp_errorta_home: Path,
                                             tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    """Drive the clone-job worker directly (no thread) with a mocked git_clone
    that materializes a fake checkout."""
    from errorta_app.routes import coding as coding_routes
    from errorta_tools.runner import publish as egress

    dest = tmp_path / "clone-dest"

    def fake_clone(url, d, *, ref=None, shallow=False, timeout=600.0):
        p = Path(d)
        (p / ".git").mkdir(parents=True)
        (p / "README.md").write_text("# cloned\n")
        return p

    monkeypatch.setattr(egress, "git_clone", fake_clone)
    monkeypatch.setattr(egress, "detect_default_branch", lambda d: "main")
    monkeypatch.setattr(egress, "git_rev_parse_head", lambda d: "abc1234")

    jid = coding_routes._job_new(coding_routes._IMPORT_JOBS, status="cloning")
    coding_routes._run_clone_job(
        jid, "gclone", "https://github.com/octocat/hello", None, str(dest), False)

    job = coding_routes._job_get(coding_routes._IMPORT_JOBS, jid)
    assert job["status"] == "done"
    from errorta_council.coding.ledger import LedgerStore
    proj = LedgerStore("gclone").get_project()
    assert proj.target == "existing"
    assert proj.import_source["kind"] == "github_clone"
    assert proj.import_source["cloned_ref"] == "main@abc1234"
    from errorta_council.coding.publish_ledger import PublishLedger
    conn = [t for t in PublishLedger("gclone").list_targets()
            if t.kind == "existing_repo_pr"]
    assert conn and conn[0].github_owner == "octocat"


def test_clone_job_reports_clean_error_on_egress_failure(
        tmp_errorta_home: Path, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    from errorta_app.routes import coding as coding_routes
    from errorta_tools.runner import publish as egress

    def boom(url, d, *, ref=None, shallow=False, timeout=600.0):
        raise egress.PublishEgressError("git_failed:clone_timeout")

    monkeypatch.setattr(egress, "git_clone", boom)
    jid = coding_routes._job_new(coding_routes._IMPORT_JOBS, status="cloning")
    coding_routes._run_clone_job(
        jid, "gfail", "https://github.com/o/r", None,
        str(tmp_path / "d"), False)
    job = coding_routes._job_get(coding_routes._IMPORT_JOBS, jid)
    assert job["status"] == "error"
    # no project was created
    from errorta_council.coding.ledger import LedgerStore, ProjectNotFound
    with pytest.raises(ProjectNotFound):
        LedgerStore("gfail").get_project()


def test_clone_status_404_for_unknown_job(tmp_errorta_home: Path) -> None:
    r = _client().get("/coding/projects/import/github/clone/deadbeef")
    assert r.status_code == 404


# --- F141 WS-C: branch discovery (ls-remote mocked, no real network) ------- #

def test_branches_rejects_non_github_url(tmp_errorta_home: Path) -> None:
    r = _client().post("/coding/projects/import/github/branches",
                       json={"repo_url": "https://evil.com/o/r"})
    assert r.status_code == 200
    assert r.json() == {"ok": False, "error": "invalid_repo_url"}


def test_branches_lists_from_remote(tmp_errorta_home: Path,
                                    monkeypatch: pytest.MonkeyPatch) -> None:
    from errorta_tools.runner import publish as egress
    monkeypatch.setattr(
        egress, "list_remote_branches",
        lambda url: {"branches": ["main", "dev"], "default_branch": "dev"})
    r = _client().post("/coding/projects/import/github/branches",
                       json={"repo_url": "https://github.com/octocat/hello"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["branches"] == ["main", "dev"]
    assert body["default_branch"] == "dev"


def test_branches_returns_structured_error_never_raises(
        tmp_errorta_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from errorta_tools.runner import publish as egress

    def boom(url):
        raise egress.PublishEgressError("git_failed:ls_remote_timeout")

    monkeypatch.setattr(egress, "list_remote_branches", boom)
    r = _client().post("/coding/projects/import/github/branches",
                       json={"repo_url": "https://github.com/o/r"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "ls_remote_timeout" in body["error"]


def test_list_remote_branches_parses_ls_remote(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The pure parser: mock subprocess.run to return ls-remote wire output."""
    import subprocess

    from errorta_tools.runner import publish as egress

    def fake_run(args, **kwargs):
        if "--symref" in args:
            out = "ref: refs/heads/dev\tHEAD\nsha\tHEAD\n"
        else:
            out = ("aaa\trefs/heads/main\n"
                   "bbb\trefs/heads/dev\n"
                   "ccc\trefs/heads/feature/x\n")
        return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = egress.list_remote_branches("https://github.com/octocat/hello")
    assert result["branches"] == ["main", "dev", "feature/x"]
    assert result["default_branch"] == "dev"
