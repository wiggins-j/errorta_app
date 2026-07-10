"""F138 S3 — refresh routes (pull-ff, local re-seed, refusals, preview)."""
from __future__ import annotations

import subprocess
import time
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


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@t.t", "-c", "user.name=t",
         "-c", "init.defaultBranch=main", "-C", str(cwd), *args],
        capture_output=True, text=True, check=True).stdout.strip()


def _origin_with_commit(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(origin), str(seed)], check=True)
    (seed / "a.txt").write_text("1\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "c1")
    _git(seed, "push", "-q", "origin", "main")
    return origin


def _clone(origin: Path, dest: Path) -> Path:
    subprocess.run(["git", "clone", "-q", str(origin), str(dest)], check=True)
    return dest


def _advance_origin(origin: Path, tmp_path: Path, content: str) -> None:
    w = tmp_path / f"push-{content}"
    subprocess.run(["git", "clone", "-q", str(origin), str(w)], check=True)
    (w / "a.txt").write_text(content + "\n")
    _git(w, "add", "-A")
    _git(w, "commit", "-q", "-m", content)
    _git(w, "push", "-q", "origin", "main")


def _make_project(pid: str, repo: Path, *, kind: str = "github_clone"):
    from errorta_council.coding.ledger import LedgerStore, _now
    from errorta_council.coding.workspace import CodingWorkspace
    from errorta_tools.runner import publish as egress
    store = LedgerStore(pid)
    head = egress.git_rev_parse_head(repo)
    store.create_project(
        north_star="", definition_of_done="", target="existing", repo_path=str(repo),
        import_source={"kind": kind, "cloned_ref": f"main@{head}",
                       "imported_at": _now()})
    CodingWorkspace(pid, store).setup(target="existing", repo_path=str(repo))
    return store


def _refresh(client: TestClient, pid: str, **body) -> dict:
    r = client.post(f"/coding/projects/{pid}/refresh", json=body)
    assert r.status_code == 200, r.text
    jid = r.json()["job_id"]
    for _ in range(200):
        j = client.get(f"/coding/projects/{pid}/refresh/{jid}").json()
        if j["status"] in ("done", "error"):
            return j
        time.sleep(0.05)
    raise AssertionError("refresh job did not finish")


def test_pull_ff_happy_path(tmp_errorta_home: Path, tmp_path: Path) -> None:
    origin = _origin_with_commit(tmp_path)
    repo = _clone(origin, tmp_path / "repo")
    _make_project("p1", repo)
    _advance_origin(origin, tmp_path, "2")  # repo is now 1 behind
    job = _refresh(_client(), "p1", pull=True)
    assert job["status"] == "done", job
    assert job["remote_pulled"] is True
    assert (repo / "a.txt").read_text() == "2\n"  # repo fast-forwarded


def test_no_origin_local_reseed(tmp_errorta_home: Path, tmp_path: Path) -> None:
    repo = tmp_path / "local"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("orig\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "c1")
    _make_project("p2", repo, kind="local_folder")
    # edit the folder outside Errorta, then refresh (no origin -> just re-seed)
    (repo / "b.txt").write_text("new\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "c2")
    job = _refresh(_client(), "p2", pull=True)
    assert job["status"] == "done", job
    assert job["remote_pulled"] is False


def test_local_reseed_tolerates_dirty_tree(tmp_errorta_home: Path, tmp_path: Path) -> None:
    # A local re-seed (no origin) just copies the working tree, so a dirty tree is
    # fine — only the PULL path requires a clean tree.
    repo = tmp_path / "local"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("orig\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "c1")
    _make_project("pd", repo, kind="local_folder")
    (repo / "a.txt").write_text("uncommitted edit\n")  # dirty
    job = _refresh(_client(), "pd", pull=True)  # no origin -> local re-seed
    assert job["status"] == "done", job


def test_refuses_dirty_repo(tmp_errorta_home: Path, tmp_path: Path) -> None:
    origin = _origin_with_commit(tmp_path)
    repo = _clone(origin, tmp_path / "repo")
    _make_project("p3", repo)
    (repo / "a.txt").write_text("uncommitted\n")  # dirty working tree
    job = _refresh(_client(), "p3", pull=True)
    assert job["status"] == "error" and job["message"] == "repo_dirty"


def test_in_progress_reason_wins_over_dirty(
        tmp_errorta_home: Path, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    from errorta_tools.runner import publish as egress
    origin = _origin_with_commit(tmp_path)
    repo = _clone(origin, tmp_path / "repo")
    _make_project("p3b", repo)
    monkeypatch.setattr(
        egress,
        "target_repo_status",
        lambda _path: {
            "clean": False,
            "dirty_paths": ["a.txt"],
            "detached": False,
            "in_progress": True,
        },
    )
    job = _refresh(_client(), "p3b", pull=True)
    assert job["status"] == "error"
    assert job["message"] == "repo_rebase_in_progress"


def test_refuses_not_on_default_branch(tmp_errorta_home: Path, tmp_path: Path) -> None:
    origin = _origin_with_commit(tmp_path)
    repo = _clone(origin, tmp_path / "repo")
    _make_project("p4", repo)
    _git(repo, "checkout", "-q", "-b", "feature-x")
    job = _refresh(_client(), "p4", pull=True)
    assert job["status"] == "error" and job["message"] == "not_on_default_branch"


def test_refuses_diverged(tmp_errorta_home: Path, tmp_path: Path) -> None:
    origin = _origin_with_commit(tmp_path)
    repo = _clone(origin, tmp_path / "repo")
    _make_project("p5", repo)
    _advance_origin(origin, tmp_path, "origin-side")
    (repo / "a.txt").write_text("local-side\n")  # local commit -> diverged
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "local")
    job = _refresh(_client(), "p5", pull=True)
    assert job["status"] == "error" and job["message"] == "branch_diverged"


def test_refuses_detached_head(tmp_errorta_home: Path, tmp_path: Path) -> None:
    origin = _origin_with_commit(tmp_path)
    repo = _clone(origin, tmp_path / "repo")
    _make_project("p6", repo)
    _git(repo, "checkout", "-q", _git(repo, "rev-parse", "HEAD"))  # detach
    job = _refresh(_client(), "p6", pull=True)
    assert job["status"] == "error" and job["message"] == "repo_detached"


def test_discard_gate(tmp_errorta_home: Path, tmp_path: Path) -> None:
    from errorta_council.coding.ledger import LedgerStore
    from errorta_council.coding.workspace import CodingWorkspace
    from errorta_tools.runner.apply_workspace import _git as _awgit
    repo = tmp_path / "local"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("orig\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "c1")
    _make_project("p7", repo, kind="local_folder")
    # simulate committed-but-unmerged run work in the snapshot
    root = CodingWorkspace("p7", LedgerStore("p7"))._ws._root
    (root / "feature.py").write_text("code\n")
    _awgit(root, "add", "-A")
    _awgit(root, "commit", "-q", "-m", "work")
    # refresh without discard -> refused
    assert _refresh(_client(), "p7", pull=True)["message"] == "unaccepted_changes"
    # with discard -> proceeds
    assert _refresh(_client(), "p7", pull=True, discard_workspace=True)["status"] == "done"


def test_unaccepted_gate_runs_before_fast_forward(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    from errorta_council.coding.ledger import LedgerStore
    from errorta_council.coding.workspace import CodingWorkspace
    from errorta_tools.runner.apply_workspace import _git as _awgit
    origin = _origin_with_commit(tmp_path)
    repo = _clone(origin, tmp_path / "repo")
    _make_project("p7b", repo)
    _advance_origin(origin, tmp_path, "remote-update")
    root = CodingWorkspace("p7b", LedgerStore("p7b"))._ws._root
    (root / "feature.py").write_text("unaccepted\n")
    _awgit(root, "add", "-A")
    _awgit(root, "commit", "-q", "-m", "work")

    job = _refresh(_client(), "p7b", pull=True)
    assert job["message"] == "unaccepted_changes"
    assert (repo / "a.txt").read_text() == "1\n"


def test_run_active_409_and_orphaned_allowed(
        tmp_errorta_home: Path, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    from errorta_app.routes import coding as cr
    origin = _origin_with_commit(tmp_path)
    repo = _clone(origin, tmp_path / "repo")
    store = _make_project("p8", repo)
    store.set_run_state(status="running")
    # a genuinely live run -> 409
    monkeypatch.setattr(cr, "_thread_alive", lambda pid: True)
    r = _client().post("/coding/projects/p8/refresh", json={})
    assert r.status_code == 409, r.text
    # orphaned run (status running, thread dead) -> reconciled, refresh allowed
    monkeypatch.setattr(cr, "_thread_alive", lambda pid: False)
    r2 = _client().post("/coding/projects/p8/refresh", json={})
    assert r2.status_code == 200, r2.text


def test_connect_github_target_is_idempotent(tmp_errorta_home: Path) -> None:
    # F138 M-2: repeated connect (every pull-refresh) must update the ONE target,
    # not append a duplicate; and reconcile its default branch (M3).
    from errorta_app.routes.coding import _connect_github_target
    from errorta_council.coding.ledger import LedgerStore
    from errorta_council.coding.publish_ledger import PublishLedger
    LedgerStore("pt").create_project(
        north_star="", definition_of_done="", target="existing", repo_path="/tmp/x")
    _connect_github_target("pt", "/tmp/x", "o", "r", "main")
    _connect_github_target("pt", "/tmp/x", "o", "r", "develop")  # default changed
    targets = [t for t in PublishLedger("pt").list_targets()
               if t.kind == "existing_repo_pr"]
    assert len(targets) == 1
    assert targets[0].default_branch == "develop"


def test_job_bails_if_run_starts_before_lock(
        tmp_errorta_home: Path, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    # F138 H-1: a run can start AFTER the route's 409 check but BEFORE the job takes
    # the lock. The job re-checks liveness inside the lock and bails, so a re-seed
    # can't rmtree a live run's worktrees. Simulate: _thread_alive False on the
    # route path, True by the time the job checks.
    from errorta_app.routes import coding as cr
    repo = tmp_path / "local"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "c1")
    _make_project("ph", repo, kind="local_folder")
    calls = {"n": 0}

    def flaky_alive(_pid: str) -> bool:
        calls["n"] += 1
        return calls["n"] >= 2  # False on the route check, True inside the job

    monkeypatch.setattr(cr, "_thread_alive", flaky_alive)
    job = _refresh(_client(), "ph", pull=False)
    assert job["status"] == "error" and job["message"] == "run_active"


def test_refresh_non_existing_project_422(tmp_errorta_home: Path, tmp_path: Path) -> None:
    from errorta_council.coding.ledger import LedgerStore
    LedgerStore("pg").create_project(
        north_star="", definition_of_done="", target="new", repo_path=None)
    r = _client().post("/coding/projects/pg/refresh", json={})
    assert r.status_code == 422


def test_preview_projection(tmp_errorta_home: Path, tmp_path: Path) -> None:
    origin = _origin_with_commit(tmp_path)
    repo = _clone(origin, tmp_path / "repo")
    _make_project("p9", repo)
    _advance_origin(origin, tmp_path, "2")
    pv = _client().get("/coding/projects/p9/refresh-preview").json()["preview"]
    assert pv["repo_path_exists"] is True
    assert pv["origin_present"] is True
    assert pv["remote_ahead"] == 1  # remote has 1 commit the snapshot lacks
    assert pv["workspace_has_unaccepted_changes"] is False


def test_preview_marks_uncommitted_repo_edits_as_different(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    origin = _origin_with_commit(tmp_path)
    repo = _clone(origin, tmp_path / "repo")
    _make_project("p9b", repo)
    (repo / "a.txt").write_text("edited outside Errorta\n")
    pv = _client().get("/coding/projects/p9b/refresh-preview").json()["preview"]
    assert pv["repo_dirty"] is True
    assert pv["repo_differs"] is True


def test_preview_requires_tauri_origin(tmp_errorta_home: Path, tmp_path: Path) -> None:
    from errorta_app.routes import coding as coding_routes
    repo = tmp_path / "local"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "c1")
    _make_project("p9c", repo, kind="local_folder")
    app = FastAPI()
    app.include_router(coding_routes.router)
    response = TestClient(app).get("/coding/projects/p9c/refresh-preview")
    assert response.status_code == 403


def test_preview_repo_path_missing_fail_open(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    import shutil
    origin = _origin_with_commit(tmp_path)
    repo = _clone(origin, tmp_path / "repo")
    _make_project("pa", repo)
    shutil.rmtree(repo)  # imported repo gone
    r = _client().get("/coding/projects/pa/refresh-preview")
    assert r.status_code == 200  # never 5xx
    assert r.json()["preview"]["repo_path_exists"] is False
