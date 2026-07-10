import subprocess
from pathlib import Path
import pytest
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.workspace import CodingWorkspace, CodingWorkspaceError


def _ledger(tmp_path: Path) -> LedgerStore:
    s = LedgerStore("wp", root=tmp_path / "ledger")
    s.create_project(north_star="n", definition_of_done="d", target="new", repo_path=None)
    return s


def test_new_project_write_records_artifact_and_diff(tmp_errorta_home: Path, tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    ws = CodingWorkspace("wp", led)
    ws.setup(target="new", repo_path=None)
    ws.write_file("src/app.py", "print('hi')\n", task_id="t1", summary="entry")
    # artifact recorded
    arts = led.list_artifacts()
    assert len(arts) == 1 and arts[0]["path"] == "src/app.py" and arts[0]["status"] == "created"
    # diff shows the new file
    prev = ws.preview()
    assert "src/app.py" in prev["diff"]
    # accept (new project) returns the worktree as deliverable, gated on confirm
    with pytest.raises(CodingWorkspaceError):
        ws.accept(confirm=False)
    res = ws.accept(confirm=True)
    assert res["mode"] == "new_project" and Path(res["root"]).is_dir()


def test_write_traversal_is_guarded(tmp_errorta_home: Path, tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    ws = CodingWorkspace("wp2", led)
    ws.setup(target="new", repo_path=None)
    with pytest.raises(Exception):
        ws.write_file("../../escape.py", "x", task_id="t1")


def test_existing_repo_merge_back(tmp_errorta_home: Path, tmp_path: Path) -> None:
    # build a tiny "user repo"
    repo = tmp_path / "userrepo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

    led = LedgerStore("wp3", root=tmp_path / "ledger3")
    led.create_project(north_star="n", definition_of_done="d", target="existing",
                       repo_path=str(repo))
    ws = CodingWorkspace("wp3", led)
    ws.setup(target="existing", repo_path=str(repo))
    ws.write_file("feature.py", "def f():\n    return 1\n", task_id="t1")
    prev = ws.preview()
    assert "feature.py" in prev["diff"]
    res = ws.accept(confirm=True)
    assert res.get("applied") or res.get("ok") or "conflicts" in res
    # the new file landed in the user repo
    assert (repo / "feature.py").exists()


def test_existing_target_needs_valid_repo(tmp_errorta_home: Path, tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    ws = CodingWorkspace("wp4", led)
    with pytest.raises(CodingWorkspaceError):
        ws.setup(target="existing", repo_path=str(tmp_path / "nope"))
