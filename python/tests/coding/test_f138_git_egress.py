"""F138 S1 — git egress helpers for refresh-from-remote (real temp git repos)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from errorta_tools.runner import publish as egress


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.email=t@t.t", "-c", "user.name=t",
         "-c", "init.defaultBranch=main", "-C", str(cwd), *args],
        capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    (path / "a.txt").write_text("1\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "c1")
    return path


def test_current_branch_reports_branch_and_detached(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "r")
    assert egress.git_current_branch(repo) == "main"
    _git(repo, "checkout", "-q", "-b", "feature-x")
    assert egress.git_current_branch(repo) == "feature-x"
    sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", sha)  # detach
    assert egress.git_current_branch(repo) == "HEAD"


def test_is_shallow(tmp_path: Path) -> None:
    origin = _init_repo(tmp_path / "origin")
    # add a second commit so a --depth 1 clone is genuinely shallow
    (origin / "b.txt").write_text("2\n")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-q", "-m", "c2")
    full = tmp_path / "full"
    subprocess.run(["git", "clone", "-q", f"file://{origin}", str(full)], check=True)
    assert egress.git_is_shallow(full) is False
    shallow = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", f"file://{origin}", str(shallow)],
        check=True)
    assert egress.git_is_shallow(shallow) is True


def _origin_and_clone(tmp_path: Path) -> tuple[Path, Path]:
    origin = _init_repo(tmp_path / "origin")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", f"file://{origin}", str(clone)], check=True)
    return origin, clone


def test_fetch_ahead_behind_and_fast_forward(tmp_path: Path) -> None:
    origin, clone = _origin_and_clone(tmp_path)
    # advance origin by two commits (clone is now 2 behind)
    for i in (2, 3):
        (origin / "a.txt").write_text(f"{i}\n")
        _git(origin, "add", "-A")
        _git(origin, "commit", "-q", "-m", f"c{i}")
    # before fetch, remote-tracking ref is stale -> 0/0
    egress.git_fetch(clone)
    ab = egress.git_ahead_behind(clone, "HEAD", "origin/main")
    assert ab == (0, 2)  # 0 ahead, 2 behind
    egress.git_fast_forward(clone, "origin/main")
    assert egress.git_ahead_behind(clone, "HEAD", "origin/main") == (0, 0)
    assert (clone / "a.txt").read_text() == "3\n"


def test_refresh_remote_head_tracks_default_branch_change(tmp_path: Path) -> None:
    origin, clone = _origin_and_clone(tmp_path)
    assert egress.detect_default_branch(clone) == "main"
    _git(origin, "checkout", "-q", "-b", "trunk")
    (origin / "trunk.txt").write_text("new default\n")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-q", "-m", "trunk")

    egress.git_fetch(clone)
    # Fetch alone intentionally leaves the clone's origin/HEAD stale.
    assert egress.detect_default_branch(clone) == "main"
    egress.git_refresh_remote_head(clone)
    assert egress.detect_default_branch(clone) == "trunk"


def test_fast_forward_refuses_on_diverged(tmp_path: Path) -> None:
    origin, clone = _origin_and_clone(tmp_path)
    # diverge: a commit on origin AND a different local commit on the clone
    (origin / "a.txt").write_text("origin\n")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-q", "-m", "origin-side")
    (clone / "a.txt").write_text("local\n")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-q", "-m", "local-side")
    egress.git_fetch(clone)
    ab = egress.git_ahead_behind(clone, "HEAD", "origin/main")
    assert ab is not None and ab[0] >= 1 and ab[1] >= 1  # ahead AND behind
    with pytest.raises(egress.PublishEgressError) as exc:
        egress.git_fast_forward(clone, "origin/main")
    assert "not_fast_forward" in str(exc.value)


def test_fetch_rejects_bad_remote_name(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "r")
    with pytest.raises(egress.PublishEgressError) as exc:
        egress.git_fetch(repo, remote="--upload-pack=evil")
    assert "invalid_remote_name" in str(exc.value)
