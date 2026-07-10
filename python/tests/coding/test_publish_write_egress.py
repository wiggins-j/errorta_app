"""F102 Slice B — gh/git write egress + default-branch detection (mocked)."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from errorta_tools.runner import publish


class _FakeProc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _capture(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Replace subprocess.run with a recorder that returns success + records argv.
    A per-call responder can be installed via the returned list's ``responder``."""
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> _FakeProc:
        calls.append(list(argv))
        return _FakeProc(0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


# -- name / branch validation (reject injection) --------------------------- #

@pytest.mark.parametrize("bad", [
    "--force", "-x", "a b", "a;rm", "..", "a..b", "a/../b", "", "a\nb",
])
def test_validate_branch_rejects_injection(bad: str) -> None:
    with pytest.raises(publish.PublishEgressError):
        publish._validate_branch_name(bad)


@pytest.mark.parametrize("good", ["errorta/proj-1", "main", "feature/x_y.z"])
def test_validate_branch_accepts_clean(good: str) -> None:
    assert publish._validate_branch_name(good) == good


@pytest.mark.parametrize("bad", [
    "--private", "-x", "a b", "a;rm", "..", "with/slash", "", ".lead",
])
def test_validate_repo_name_rejects_injection(bad: str) -> None:
    with pytest.raises(publish.PublishEgressError):
        publish._validate_repo_name(bad)


def test_validate_repo_name_accepts_clean() -> None:
    assert publish._validate_repo_name("my-cool.repo_1") == "my-cool.repo_1"


# -- has_origin / detect_default_branch / target_repo_status --------------- #

def test_has_origin_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: _FakeProc(0, "git@github.com:x/y.git\n"))
    assert publish.has_origin("/repo") is True


def test_has_origin_false_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeProc(2, "", "no remote"))
    assert publish.has_origin("/repo") is False


def test_detect_default_branch_from_symbolic_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(argv: list[str], **k: Any) -> _FakeProc:
        if "symbolic-ref" in argv and "refs/remotes/origin/HEAD" in argv:
            return _FakeProc(0, "refs/remotes/origin/develop\n")
        return _FakeProc(1)
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert publish.detect_default_branch("/repo") == "develop"


def test_detect_default_branch_falls_back_to_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(publish, "get_gh_binary", lambda: "/bin/gh")

    def fake_run(argv: list[str], **k: Any) -> _FakeProc:
        if argv and argv[0] == "/bin/gh":
            return _FakeProc(0, "trunk\n")
        return _FakeProc(1)  # symbolic-ref + show-ref all fail
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert publish.detect_default_branch("/repo") == "trunk"


def test_detect_default_branch_final_fallback_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(publish, "get_gh_binary", lambda: None)

    def fake_run(argv: list[str], **k: Any) -> _FakeProc:
        if "show-ref" in argv and "refs/heads/master" in argv:
            return _FakeProc(0)
        return _FakeProc(1)
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert publish.detect_default_branch("/repo") == "master"


def test_target_repo_status_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(argv: list[str], **k: Any) -> _FakeProc:
        if "status" in argv:
            return _FakeProc(0, "")
        if "symbolic-ref" in argv:
            return _FakeProc(0, "refs/heads/main\n")  # on a branch
        if "rev-parse" in argv and "--git-dir" in argv:
            return _FakeProc(0, "/repo/.git\n")
        return _FakeProc(0)
    monkeypatch.setattr(subprocess, "run", fake_run)
    st = publish.target_repo_status("/repo")
    assert st["clean"] is True
    assert st["dirty_paths"] == []
    assert st["detached"] is False
    assert st["in_progress"] is False


def test_target_repo_status_dirty_and_detached(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(argv: list[str], **k: Any) -> _FakeProc:
        if "status" in argv:
            return _FakeProc(0, " M src/app.py\n?? new.txt\n")
        if "symbolic-ref" in argv:
            return _FakeProc(1)  # detached
        if "rev-parse" in argv:
            return _FakeProc(0, "/repo/.git\n")
        return _FakeProc(0)
    monkeypatch.setattr(subprocess, "run", fake_run)
    st = publish.target_repo_status("/repo")
    assert st["clean"] is False
    assert "src/app.py" in st["dirty_paths"]
    assert "new.txt" in st["dirty_paths"]
    assert st["detached"] is True


def test_git_tracked_paths_uses_nul_delimited_ls_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _capture(monkeypatch)
    monkeypatch.setattr(
        subprocess, "run",
        lambda argv, **k: (calls.append(list(argv)) or
                           _FakeProc(0, "a.py\0dir/b.txt\0")),
    )
    assert publish.git_tracked_paths("/repo") == ["a.py", "dir/b.txt"]
    assert calls[-1][-2:] == ["ls-files", "-z"]


# -- branch / commit / push / pr / repo-create argv ------------------------ #

def test_git_checkout_new_branch_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _capture(monkeypatch)
    publish.git_checkout_new_branch("/repo", "errorta/p1", carry=True)
    assert calls
    last = calls[-1]
    assert "checkout" in last and "-b" in last and "errorta/p1" in last


def test_git_commit_all_uses_body_file_not_argv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    seen_files: list[str] = []

    def fake_run(argv: list[str], **k: Any) -> _FakeProc:
        if "commit" in argv:
            # body must be passed via -F <file>, never inline.
            assert "-F" in argv, argv
            idx = argv.index("-F")
            body_path = argv[idx + 1]
            seen_files.append(body_path)
            assert Path(body_path).read_text(encoding="utf-8").find("BODYTEXT") >= 0
            assert "BODYTEXT" not in " ".join(a for a in argv if a != body_path)
            return _FakeProc(0)
        if "rev-parse" in argv:
            return _FakeProc(0, "deadbeef\n")
        return _FakeProc(0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    sha = publish.git_commit_all("/repo", "subject line", body="BODYTEXT in body")
    assert sha == "deadbeef"
    # temp body file is cleaned up.
    assert not Path(seen_files[0]).exists()


def test_git_push_argv_and_set_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _capture(monkeypatch)
    res = publish.git_push("/repo", "origin", "errorta/p1", set_upstream=True)
    assert res == {"pushed": True, "branch": "errorta/p1"}
    last = calls[-1]
    assert last[:1] == ["git"]
    assert "push" in last and "--set-upstream" in last and "origin" in last


def test_git_push_rejects_non_origin_remote(monkeypatch: pytest.MonkeyPatch) -> None:
    _capture(monkeypatch)
    with pytest.raises(publish.PublishEgressError):
        publish.git_push("/repo", "upstream", "errorta/p1")


def test_git_push_maps_failure_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    home = str(Path.home())
    stderr = f"fatal at {home}/secret token=ghp_aaaaaaaaaaaaaaaaaaaaaaaa"
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: _FakeProc(1, "", stderr))
    with pytest.raises(publish.PublishEgressError) as ei:
        publish.git_push("/repo", "origin", "errorta/p1")
    msg = str(ei.value)
    assert "git_failed:push" in msg
    assert "ghp_aaaa" not in msg
    assert home not in msg


def test_gh_pr_create_uses_body_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(publish, "get_gh_binary", lambda: "/bin/gh")
    seen: dict[str, Any] = {}

    def fake_run(argv: list[str], **k: Any) -> _FakeProc:
        seen["argv"] = argv
        assert "--body-file" in argv
        bf = argv[argv.index("--body-file") + 1]
        assert Path(bf).read_text(encoding="utf-8") == "PR BODY"
        return _FakeProc(0, "https://github.com/x/y/pull/7\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    res = publish.gh_pr_create(
        "/repo", base="main", head="errorta/p1", title="t", body="PR BODY")
    assert res == {"pr_url": "https://github.com/x/y/pull/7"}
    assert seen["argv"][0] == "/bin/gh"
    assert "pr" in seen["argv"] and "create" in seen["argv"]


def test_gh_pr_create_absent_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(publish, "get_gh_binary", lambda: None)
    with pytest.raises(publish.PublishEgressError):
        publish.gh_pr_create("/repo", base="main", head="b", title="t", body="x")


def test_gh_repo_create_private_default_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(publish, "get_gh_binary", lambda: "/bin/gh")
    calls = _capture(monkeypatch)
    monkeypatch.setattr(
        subprocess, "run",
        lambda argv, **k: (calls.append(list(argv)) or
                           _FakeProc(0, "https://github.com/me/newrepo\n")))
    res = publish.gh_repo_create("newrepo", source_dir="/tmp/x", push=True)
    assert res == {"repo_url": "https://github.com/me/newrepo"}
    argv = calls[-1]
    assert "repo" in argv and "create" in argv and "newrepo" in argv
    assert "--private" in argv and "--public" not in argv
    assert "--source" in argv and "--push" in argv


def test_gh_repo_create_rejects_bad_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(publish, "get_gh_binary", lambda: "/bin/gh")
    _capture(monkeypatch)
    with pytest.raises(publish.PublishEgressError):
        publish.gh_repo_create("--public", source_dir="/tmp/x")
