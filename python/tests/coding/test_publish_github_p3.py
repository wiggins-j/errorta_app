"""F102 Slice C — P3 existing-repo PR (orchestrator + route, egress mocked)."""
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


@dataclass
class _FakeGate:
    allowed: bool = True
    blockers: list[Any] = field(default_factory=list)


def _allow_gate(monkeypatch: pytest.MonkeyPatch, *, allowed: bool = True,
                blockers: list[str] | None = None) -> None:
    @dataclass
    class _B:
        code: str

    gate = _FakeGate(allowed=allowed,
                     blockers=[_B(c) for c in (blockers or [])])
    monkeypatch.setattr(
        "errorta_council.coding.evidence.merge_review",
        lambda store, ws: {"_gate": gate, "gate": {"allowed": allowed}})


def _existing_project(project_id: str, repo_dir: Path, *, delivered: bool = True):
    from errorta_council.coding.ledger import LedgerStore
    from errorta_council.coding.workspace import CodingWorkspace
    store = LedgerStore(project_id)
    store.create_project(north_star="ns", definition_of_done="d",
                         target="existing", repo_path=str(repo_dir))
    ws = CodingWorkspace(project_id, store)
    ws.setup(target="existing", repo_path=str(repo_dir))
    # write CHANGED content so feature.py is genuinely in the accepted changed set
    ws.write_file("feature.py", "def f():\n    return 42\n", task_id="t1")
    if delivered:
        store.record_decision(title="delivered", context="merge-back",
                              choice="delivered", rationale="delivered to repo")
    return store, ws


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "userrepo"
    repo.mkdir()
    (repo / ".git").mkdir()  # marker; egress is mocked so no real git needed
    (repo / "feature.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    return repo


def _mock_egress_happy(monkeypatch: pytest.MonkeyPatch, *,
                       dirty: list[str] | None = None,
                       tracked: list[str] | None = None) -> dict[str, Any]:
    captured: dict[str, Any] = {"pushed": False, "committed": False,
                                "branch": None, "pr": False}
    from errorta_tools.runner import publish as egress

    monkeypatch.setattr(egress, "has_origin", lambda repo: True)
    monkeypatch.setattr(egress, "target_repo_status",
                        lambda repo: {"clean": not dirty, "dirty_paths": dirty or [],
                                      "detached": False, "in_progress": False})
    monkeypatch.setattr(egress, "detect_default_branch", lambda repo: "main")
    monkeypatch.setattr(egress, "git_tracked_paths",
                        lambda repo: tracked or ["feature.py"])

    def _checkout(repo, branch, *, carry=True):  # noqa: ANN001
        captured["branch"] = branch
        # never the default branch
        assert branch != "main"
    monkeypatch.setattr(egress, "git_checkout_new_branch", _checkout)

    def _commit(repo, message, *, body=""):  # noqa: ANN001
        captured["committed"] = True
        return "commitsha123"
    monkeypatch.setattr(egress, "git_commit_all", _commit)

    def _push(repo, remote, branch, *, set_upstream=True):  # noqa: ANN001
        captured["pushed"] = True
        assert remote == "origin"
        return {"pushed": True, "branch": branch}
    monkeypatch.setattr(egress, "git_push", _push)

    def _pr(repo, *, base, head, title, body):  # noqa: ANN001
        captured["pr"] = True
        captured["base"] = base
        return {"pr_url": "https://github.com/x/y/pull/9"}
    monkeypatch.setattr(egress, "gh_pr_create", _pr)
    return captured


def test_p3_happy_path(tmp_errorta_home: Path, tmp_path: Path,
                       monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _make_repo(tmp_path)
    _existing_project("p3-ok", repo)
    _allow_gate(monkeypatch)
    cap = _mock_egress_happy(monkeypatch, dirty=["feature.py"])

    resp = _client().post("/coding/projects/p3-ok/publish/existing-repo-pr", json={})
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["pr_url"] == "https://github.com/x/y/pull/9"
    assert out["branch"] == "errorta/p3-ok"
    assert out["base"] == "main"
    assert cap["pushed"] and cap["committed"] and cap["pr"]
    # event log records the full transition.
    states = [e["state"] for e in out["events"]]
    assert states[-1] == "pr_opened"
    assert "scanned" in states and "committed" in states and "pushed" in states
    # never a direct push of the default branch.
    assert cap["branch"] == "errorta/p3-ok"
    # no token in the response.
    assert "ghp_" not in resp.text


def test_p3_requires_tauri_origin(tmp_errorta_home: Path, tmp_path: Path,
                                  monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _make_repo(tmp_path)
    _existing_project("p3-origin", repo)
    from errorta_app.routes import coding as coding_routes
    app = FastAPI()
    app.include_router(coding_routes.router)
    resp = TestClient(app).post(
        "/coding/projects/p3-origin/publish/existing-repo-pr", json={})
    assert resp.status_code == 403


def test_p3_not_delivered_409(tmp_errorta_home: Path, tmp_path: Path,
                              monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _make_repo(tmp_path)
    _existing_project("p3-undelivered", repo, delivered=False)
    _allow_gate(monkeypatch)
    _mock_egress_happy(monkeypatch, dirty=["feature.py"])
    resp = _client().post(
        "/coding/projects/p3-undelivered/publish/existing-repo-pr", json={})
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "not_delivered"


def test_p3_open_tasks_409(tmp_errorta_home: Path, tmp_path: Path,
                           monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _make_repo(tmp_path)
    _existing_project("p3-open", repo)
    _allow_gate(monkeypatch, allowed=False, blockers=["open_tasks"])
    _mock_egress_happy(monkeypatch, dirty=["feature.py"])
    resp = _client().post("/coding/projects/p3-open/publish/existing-repo-pr", json={})
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "open_tasks"


def test_p3_gate_blocker_reports_real_code(
        tmp_errorta_home: Path, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-open_tasks merge-gate blocker must surface its OWN code — a
    fully-delivered project with every task done was wrongly told "open_tasks"
    because the reason was hardcoded regardless of the actual blocker."""
    repo = _make_repo(tmp_path)
    _existing_project("p3-tests", repo)
    _allow_gate(monkeypatch, allowed=False, blockers=["tests_missing"])
    _mock_egress_happy(monkeypatch, dirty=["feature.py"])
    resp = _client().post("/coding/projects/p3-tests/publish/existing-repo-pr", json={})
    assert resp.status_code == 409
    body = resp.json()["detail"]
    assert body["error"] == "tests_missing"
    assert body["detail"]["blockers"] == ["tests_missing"]


def test_p3_no_origin_409(tmp_errorta_home: Path, tmp_path: Path,
                          monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _make_repo(tmp_path)
    _existing_project("p3-noorigin", repo)
    _allow_gate(monkeypatch)
    _mock_egress_happy(monkeypatch, dirty=["feature.py"])
    from errorta_tools.runner import publish as egress
    monkeypatch.setattr(egress, "has_origin", lambda repo: False)
    resp = _client().post(
        "/coding/projects/p3-noorigin/publish/existing-repo-pr", json={})
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "no_origin"


def test_p3_clobber_unrelated_changes_409(tmp_errorta_home: Path, tmp_path: Path,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _make_repo(tmp_path)
    _existing_project("p3-clobber", repo)
    _allow_gate(monkeypatch)
    # dirty set has a file NOT in the accepted set (feature.py) -> clobber refuse.
    _mock_egress_happy(monkeypatch, dirty=["feature.py", "user_unrelated.py"])
    resp = _client().post("/coding/projects/p3-clobber/publish/existing-repo-pr", json={})
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "clobber_unrelated_changes"
    assert "user_unrelated.py" in detail["detail"]["unrelated_paths"]


def test_p3_scan_hit_blocks_then_override_passes(
    tmp_errorta_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_repo(tmp_path)
    # plant a secret into the changed file in the repo working tree.
    (repo / "feature.py").write_text(
        "TOKEN = 'ghp_0123456789abcdefghij0123456789abcd'\n", encoding="utf-8")
    _existing_project("p3-scan", repo)
    _allow_gate(monkeypatch)
    _mock_egress_happy(monkeypatch, dirty=["feature.py"])

    # without override -> blocked.
    resp = _client().post("/coding/projects/p3-scan/publish/existing-repo-pr", json={})
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "secret_scan_hit"
    assert not detail["detail"]["clean"]
    assert "ghp_0123456789" not in resp.text  # findings excerpt is redacted

    # with override -> proceeds.
    resp2 = _client().post(
        "/coding/projects/p3-scan/publish/existing-repo-pr", json={"override": True})
    assert resp2.status_code == 200, resp2.text
    assert resp2.json()["pr_url"]


def test_p3_scans_unchanged_tracked_files(
    tmp_errorta_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_repo(tmp_path)
    # Not dirty and not in the accepted changed set, but it is tracked and would
    # be present in the branch tree.
    (repo / ".env").write_text("OPENAI_API_KEY=sk-secretsecretsecretsecret\n",
                               encoding="utf-8")
    _existing_project("p3-tree-scan", repo)
    _allow_gate(monkeypatch)
    _mock_egress_happy(monkeypatch, dirty=["feature.py"],
                       tracked=["feature.py", ".env"])

    resp = _client().post(
        "/coding/projects/p3-tree-scan/publish/existing-repo-pr", json={})
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "secret_scan_hit"
    assert any(f["path"] == ".env" for f in detail["detail"]["findings"])
    assert "sk-secretsecret" not in resp.text
