"""F135 S5 — PM-drafted branch/title/body extend F102 P3 (defaults unchanged)."""
from __future__ import annotations

from dataclasses import dataclass, field
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


def _allow_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    @dataclass
    class _B:
        code: str

    @dataclass
    class _Gate:
        allowed: bool = True
        blockers: list[Any] = field(default_factory=list)

    monkeypatch.setattr("errorta_council.coding.evidence.merge_review",
                        lambda store, ws: {"_gate": _Gate(), "gate": {"allowed": True}})


def _existing_project(project_id: str, repo_dir: Path):
    from errorta_council.coding.ledger import LedgerStore
    from errorta_council.coding.workspace import CodingWorkspace
    store = LedgerStore(project_id)
    store.create_project(north_star="ns", definition_of_done="d",
                         target="existing", repo_path=str(repo_dir))
    ws = CodingWorkspace(project_id, store)
    ws.setup(target="existing", repo_path=str(repo_dir))
    ws.write_file("feature.py", "def f():\n    return 42\n", task_id="t1")
    store.record_decision(title="delivered", context="merge-back",
                          choice="delivered", rationale="delivered")
    return store, ws


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "userrepo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "feature.py").write_text("def f():\n    return 1\n")
    return repo


def _mock_egress(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    cap: dict[str, Any] = {}
    from errorta_tools.runner import publish as egress
    monkeypatch.setattr(egress, "has_origin", lambda repo: True)
    monkeypatch.setattr(egress, "target_repo_status",
                        lambda repo: {"clean": False, "dirty_paths": ["feature.py"],
                                      "detached": False, "in_progress": False})
    monkeypatch.setattr(egress, "detect_default_branch", lambda repo: "main")
    monkeypatch.setattr(egress, "git_tracked_paths", lambda repo: ["feature.py"])

    def _checkout(repo, branch, *, carry=True):
        cap["branch"] = branch
        assert branch != "main"
    monkeypatch.setattr(egress, "git_checkout_new_branch", _checkout)

    def _commit(repo, message, *, body=""):
        cap["commit_title"] = message
        cap["commit_body"] = body
        return "sha123"
    monkeypatch.setattr(egress, "git_commit_all", _commit)
    monkeypatch.setattr(egress, "git_push",
                        lambda repo, remote, branch, *, set_upstream=True:
                        {"pushed": True, "branch": branch})

    def _pr(repo, *, base, head, title, body):
        cap["pr_title"] = title
        cap["pr_body"] = body
        cap["pr_head"] = head
        return {"pr_url": "https://github.com/x/y/pull/1"}
    monkeypatch.setattr(egress, "gh_pr_create", _pr)
    return cap


def test_pm_drafted_branch_title_body(tmp_errorta_home: Path, tmp_path: Path,
                                      monkeypatch: pytest.MonkeyPatch) -> None:
    _existing_project("ov1", _make_repo(tmp_path))
    _allow_gate(monkeypatch)
    cap = _mock_egress(monkeypatch)
    r = _client().post("/coding/projects/ov1/publish/existing-repo-pr",
                       json={"branch": "feat/custom", "title": "My PR title",
                             "body_override": "A hand-written PR body."})
    assert r.status_code == 200, r.text
    assert r.json()["branch"] == "feat/custom"
    assert cap["branch"] == "feat/custom"
    assert cap["pr_head"] == "feat/custom"
    assert cap["commit_title"] == "My PR title"
    assert cap["pr_title"] == "My PR title"
    assert cap["pr_body"] == "A hand-written PR body."


def test_pm_body_override_is_redacted(tmp_errorta_home: Path, tmp_path: Path,
                                      monkeypatch: pytest.MonkeyPatch) -> None:
    _existing_project("ov2", _make_repo(tmp_path))
    _allow_gate(monkeypatch)
    cap = _mock_egress(monkeypatch)
    secret = "ghp_" + "A" * 36
    r = _client().post("/coding/projects/ov2/publish/existing-repo-pr",
                       json={"body_override": f"look: {secret}"})
    assert r.status_code == 200, r.text
    assert secret not in cap["pr_body"]
    assert secret not in r.text


def test_pm_title_is_redacted(tmp_errorta_home: Path, tmp_path: Path,
                              monkeypatch: pytest.MonkeyPatch) -> None:
    """A PM-drafted title reaches the PUBLIC PR title + commit subject, so it must
    be scrubbed with the same redaction as the body (leak parity)."""
    _existing_project("ov5", _make_repo(tmp_path))
    _allow_gate(monkeypatch)
    cap = _mock_egress(monkeypatch)
    secret = "ghp_" + "B" * 36
    r = _client().post("/coding/projects/ov5/publish/existing-repo-pr",
                       json={"title": f"ship {secret}\nnow"})
    assert r.status_code == 200, r.text
    assert secret not in cap["pr_title"]
    assert secret not in cap["commit_title"]
    assert secret not in r.text
    # Collapsed to a single line (a title, not a body).
    assert "\n" not in cap["pr_title"]


def test_invalid_branch_is_400(tmp_errorta_home: Path, tmp_path: Path,
                               monkeypatch: pytest.MonkeyPatch) -> None:
    _existing_project("ov3", _make_repo(tmp_path))
    _allow_gate(monkeypatch)
    _mock_egress(monkeypatch)
    r = _client().post("/coding/projects/ov3/publish/existing-repo-pr",
                       json={"branch": "bad branch with spaces!"})
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "invalid_branch"


def test_omitted_fields_use_defaults(tmp_errorta_home: Path, tmp_path: Path,
                                     monkeypatch: pytest.MonkeyPatch) -> None:
    _existing_project("ov4", _make_repo(tmp_path))
    _allow_gate(monkeypatch)
    cap = _mock_egress(monkeypatch)
    r = _client().post("/coding/projects/ov4/publish/existing-repo-pr", json={})
    assert r.status_code == 200, r.text
    assert r.json()["branch"] == "errorta/ov4"
    assert cap["branch"] == "errorta/ov4"
    assert cap["commit_title"] == "Errorta: ov4"
