"""F138 S2 — atomic snapshot re-seed + un-accepted-work detection."""
from __future__ import annotations

from pathlib import Path

import pytest

from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.workspace import CodingWorkspace
from errorta_tools.runner import apply_workspace as aw
from errorta_tools.runner.apply_workspace import _git


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()  # a .git marker; _copy_ignore drops it, snapshot re-inits
    (repo / "a.txt").write_text("orig\n")
    return repo


def _ws(tmp_path: Path) -> tuple[CodingWorkspace, Path]:
    repo = _repo(tmp_path)
    store = LedgerStore("p")
    store.create_project(north_star="", definition_of_done="",
                         target="existing", repo_path=str(repo))
    ws = CodingWorkspace("p", store)
    ws.setup(target="existing", repo_path=str(repo))
    return ws, repo


def test_reseed_picks_up_external_edits(tmp_errorta_home: Path, tmp_path: Path) -> None:
    ws, repo = _ws(tmp_path)
    root = ws._ws._root
    assert (root / "a.txt").read_text() == "orig\n"
    assert not (root / "b.txt").exists()
    # edit the imported repo OUTSIDE Errorta
    (repo / "a.txt").write_text("changed\n")
    (repo / "b.txt").write_text("new\n")
    ws.reseed(str(repo))
    assert (root / "a.txt").read_text() == "changed\n"
    assert (root / "b.txt").read_text() == "new\n"
    # the re-seeded snapshot is a valid git repo on master
    assert _git(root, "rev-parse", "--abbrev-ref", "HEAD").strip() == "master"


def test_has_unaccepted_changes(tmp_errorta_home: Path, tmp_path: Path) -> None:
    ws, _repo_dir = _ws(tmp_path)
    assert ws.has_unaccepted_changes() is False  # baseline == repo
    # simulate committed-but-not-merged-back run output in the snapshot
    root = ws._ws._root
    (root / "feature.py").write_text("code\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "work")
    assert ws.has_unaccepted_changes() is True


def test_reseed_is_atomic_on_copy_failure(
        tmp_errorta_home: Path, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    ws, repo = _ws(tmp_path)
    root = ws._ws._root
    old_head = _git(root, "rev-parse", "HEAD").strip()

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(aw.shutil, "copytree", boom)
    with pytest.raises(OSError):
        ws.reseed(str(repo))
    # the prior good snapshot survives untouched (destroy() never ran)
    assert (root / "a.txt").read_text() == "orig\n"
    assert _git(root, "rev-parse", "HEAD").strip() == old_head
    # no orphaned temp dir left behind
    assert not (root.parent / f"{ws._ws._run_id}.reseed-tmp").exists()


def test_reseed_resets_the_unaccepted_signal(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    ws, repo = _ws(tmp_path)
    root = ws._ws._root
    # a run committed work in the snapshot
    (root / "feature.py").write_text("code\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "work")
    assert ws.has_unaccepted_changes() is True
    # after a re-seed, the snapshot is clean again (seed HEAD re-recorded)
    ws.reseed(str(repo))
    assert ws.has_unaccepted_changes() is False


def test_unaccepted_detects_unmerged_task_branch_work(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    # F138 M-1: a run commits to a task-* branch and never merges to master (e.g.
    # an interrupted run). master stays at seed, but the work must still be seen so
    # a re-seed can't silently destroy it.
    ws, _repo = _ws(tmp_path)
    root = ws._ws._root
    _git(root, "checkout", "-q", "-b", "task-x")
    (root / "f.py").write_text("task work\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "task work")
    _git(root, "checkout", "-q", "master")  # master back at seed
    assert ws.has_unaccepted_changes() is True


def test_unaccepted_detects_uncommitted_primary_work(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    ws, _repo = _ws(tmp_path)
    (ws._ws._root / "draft.py").write_text("not committed\n")
    assert ws.has_unaccepted_changes() is True


def test_unaccepted_detects_uncommitted_task_worktree(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    ws, _repo = _ws(tmp_path)
    task_root = ws._ws.worktree_for("task-x")
    (task_root / "draft.py").write_text("not committed\n")
    assert ws.has_unaccepted_changes() is True


def test_reseed_rolls_back_when_snapshot_swap_fails(
        tmp_errorta_home: Path, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    ws, repo = _ws(tmp_path)
    root = ws._ws._root
    old_head = _git(root, "rev-parse", "HEAD").strip()
    real_replace = aw.os.replace

    def fail_new_snapshot(src, dst):
        if Path(src).name.endswith(".reseed-tmp") and Path(dst) == root:
            raise OSError("swap failed")
        return real_replace(src, dst)

    monkeypatch.setattr(aw.os, "replace", fail_new_snapshot)
    with pytest.raises(OSError, match="swap failed"):
        ws.reseed(str(repo))
    assert (root / "a.txt").read_text() == "orig\n"
    assert _git(root, "rev-parse", "HEAD").strip() == old_head
    assert not (root.parent / f"{ws._ws._run_id}.reseed-tmp").exists()
    assert not (root.parent / f"{ws._ws._run_id}.reseed-backup").exists()


def test_unaccepted_signal_ignores_external_repo_edits(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    # editing the imported repo (not committing snapshot work) is NOT "un-accepted
    # Errorta work" — reseed will pick it up; the gate only protects snapshot commits.
    ws, repo = _ws(tmp_path)
    (repo / "a.txt").write_text("edited outside\n")
    (repo / "c.txt").write_text("added outside\n")
    assert ws.has_unaccepted_changes() is False
