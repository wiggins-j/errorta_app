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


# --- code_edit: CodingWorkspace.edit_file ----------------------------------


def test_edit_file_splices_commits_and_records_provenance(
    tmp_errorta_home: Path, tmp_path: Path,
) -> None:
    led = _ledger(tmp_path)
    ws = CodingWorkspace("editok", led)
    ws.setup(target="new", repo_path=None)
    ws.write_file("app.py", "def f():\n    return 1\n", task_id="t-1")
    head = ws.edit_file("app.py", "return 1", "return 2", task_id="t-1")
    assert head
    assert (ws.root() / "app.py").read_text("utf-8") == "def f():\n    return 2\n"
    art = {a["path"]: a for a in led.list_artifacts()}
    assert art["app.py"]["status"] == "modified"


def test_edit_file_missing_target_is_typed(
    tmp_errorta_home: Path, tmp_path: Path,
) -> None:
    led = _ledger(tmp_path)
    ws = CodingWorkspace("editmiss", led)
    ws.setup(target="new", repo_path=None)
    with pytest.raises(CodingWorkspaceError) as exc:
        ws.edit_file("nope.py", "a", "b", task_id="t-1")
    assert str(exc.value).startswith("edit_target_missing: ")
    assert "code_write" in str(exc.value)  # names the recovery tool


def test_edit_file_binary_target_is_typed(
    tmp_errorta_home: Path, tmp_path: Path,
) -> None:
    led = _ledger(tmp_path)
    ws = CodingWorkspace("editbin", led)
    ws.setup(target="new", repo_path=None)
    ws.write_file("sprite.png", b"\x89PNG\x00\x1a", task_id="t-1")
    with pytest.raises(CodingWorkspaceError) as exc:
        ws.edit_file("sprite.png", "PNG", "JPG", task_id="t-1")
    assert str(exc.value).startswith("edit_target_binary: ")


def test_edit_file_traversal_is_rejected(
    tmp_errorta_home: Path, tmp_path: Path,
) -> None:
    led = _ledger(tmp_path)
    ws = CodingWorkspace("edittrav", led)
    ws.setup(target="new", repo_path=None)
    with pytest.raises(Exception):
        ws.edit_file("../escape.py", "a", "b", task_id="t-1")


def test_edit_file_no_match_propagates_typed_code(
    tmp_errorta_home: Path, tmp_path: Path,
) -> None:
    led = _ledger(tmp_path)
    ws = CodingWorkspace("editnom", led)
    ws.setup(target="new", repo_path=None)
    ws.write_file("app.py", "x = 1\n", task_id="t-1")
    with pytest.raises(CodingWorkspaceError) as exc:
        ws.edit_file("app.py", "y = 2", "y = 3", task_id="t-1")
    assert str(exc.value).startswith("edit_no_match: ")


def test_edit_that_guts_a_large_file_hits_the_f140_guard(
    tmp_errorta_home: Path, tmp_path: Path,
) -> None:
    # The spec's required guard interaction: the spliced result is a full
    # old->new pair, so classify_destructive_write applies to code_edit exactly
    # as to code_write — deleting a huge unique block from a large file blocks.
    led = _ledger(tmp_path)
    ws = CodingWorkspace("editgut", led)
    ws.setup(target="new", repo_path=None)
    body = "def start():\n    pass\n" + "\n".join(
        f"def f{i}():\n    return {i}" for i in range(200))
    ws.write_file("big.py", body, task_id="t-1")
    huge_block = "\n".join(f"def f{i}():\n    return {i}" for i in range(200))
    with pytest.raises(CodingWorkspaceError) as exc:
        ws.edit_file("big.py", huge_block, "# gone", task_id="t-1")
    assert "destructive_write_blocked" in str(exc.value)
