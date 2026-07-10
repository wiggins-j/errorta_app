"""F139 WS-B (slice 1) — git is the sole source of truth for "what is merged".

`CodingWorkspace.list_files(scope="master")` reads the merged tree via
`git ls-tree master`; `LedgerStore.list_artifacts(scope="merged", merged_paths=...)`
projects provenance onto that git truth. The regression these lock is the
`reddit-look-a-like` bug: a file written only on an abandoned task branch must
NEVER appear in the merged view even though its artifact record persists.
"""
from pathlib import Path

import pytest

from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.workspace import CodingWorkspace
from errorta_tools.runner.apply_workspace import ApplyWorkspace


def _new_ws(project_id: str, tmp_path: Path) -> tuple[LedgerStore, CodingWorkspace]:
    store = LedgerStore(project_id, root=tmp_path / f"ledger-{project_id}")
    store.create_project(north_star="n", definition_of_done="d",
                         target="new", repo_path=None)
    ws = CodingWorkspace(project_id, store)
    ws.setup(target="new", repo_path=None)
    return store, ws


def _merge_file(ws: CodingWorkspace, task_id: str, path: str, content: str) -> None:
    branch = ws.start_task_branch(task_id)
    ws.write_file(path, content, task_id=task_id)
    res = ws.merge_pr(branch)
    assert res.get("merged"), res


def test_list_files_scope_master_reflects_merged_tree(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    store, ws = _new_ws("mv1", tmp_path)
    # Only the seeded .gitignore is on master before any PR merges.
    assert ws.list_files(scope="master") == [".gitignore"]
    _merge_file(ws, "t1", "src/app.py", "print('hi')\n")
    merged = ws.list_files(scope="master")
    assert "src/app.py" in merged
    assert ".gitignore" in merged


def test_abandoned_branch_file_absent_from_merged_view(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    """The reddit regression: work stranded on an unmerged branch persists in the
    artifact ledger but must not appear in the merged (git) truth."""
    store, ws = _new_ws("mv2", tmp_path)
    _merge_file(ws, "t1", "keep.py", "x = 1\n")

    # Write on a branch we deliberately never merge.
    ws.start_task_branch("t2")
    ws.write_file("orphan.py", "y = 2\n", task_id="t2")

    # Provenance (scope="all") records BOTH — that is the divergence the old
    # episode summary trusted.
    all_paths = {a["path"] for a in store.list_artifacts(scope="all")}
    assert {"keep.py", "orphan.py"} <= all_paths

    # Git truth: the orphan is not merged.
    merged = set(ws.list_files(scope="master"))
    assert "keep.py" in merged
    assert "orphan.py" not in merged

    # Merged-scope artifacts project provenance onto git truth → orphan excluded.
    merged_arts = {a["path"]
                   for a in store.list_artifacts(scope="merged", merged_paths=merged)}
    assert "keep.py" in merged_arts
    assert "orphan.py" not in merged_arts


def test_list_artifacts_default_scope_is_all_and_backward_compatible(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    store, ws = _new_ws("mv3", tmp_path)
    ws.start_task_branch("t1")
    ws.write_file("a.py", "1\n", task_id="t1")
    # Legacy no-arg call still returns everything (default scope="all").
    assert {a["path"] for a in store.list_artifacts()} == {"a.py"}


def test_list_artifacts_merged_requires_git_truth(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    store, _ws = _new_ws("mv4", tmp_path)
    # The ledger must never invent mergedness — merged_paths is mandatory.
    with pytest.raises(ValueError):
        store.list_artifacts(scope="merged")
    with pytest.raises(ValueError):
        store.list_artifacts(scope="bogus")


def test_list_files_on_ref_missing_ref_is_fail_closed_empty(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    seed = tmp_path / "seed"
    seed.mkdir()
    aw = ApplyWorkspace(run_id="coding-f139-ref")
    aw.ensure(seed)
    # A ref that does not exist yields [] (never invents files).
    assert aw.list_files_on_ref("no-such-branch") == []
    # master exists after ensure() → a real list.
    assert isinstance(aw.list_files_on_ref("master"), list)


def test_reviewer_context_shows_merged_surface_and_pr_additions(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    """F139 WS-B (slice 2): the reviewer sees the true merged master surface plus
    this PR's own additions — not a stale PR-branch listing that omits siblings'
    merged files (the false 'imports absent from master' rejection cause)."""
    from errorta_council.coding.runner import _review_project_context

    store = LedgerStore("revmv", root=tmp_path / "ledger-revmv")
    store.create_project(north_star="Build a calculator",
                         definition_of_done="add+sub all tested",
                         target="new", repo_path=None)
    ws = CodingWorkspace("revmv", store)
    ws.setup(target="new", repo_path=None)
    # A sibling's work is already merged on master.
    _merge_file(ws, "sib", "src/types.py", "Post = dict\n")
    # The PR under review is on its own (unmerged) branch and adds a new file.
    ws.start_task_branch("t1")
    ws.write_file("src/calculator.py", "def add(a, b):\n    return a + b\n",
                  task_id="t1")
    pr = store.record_pr(task_id="t1", branch=ws.task_branch("t1"),
                         head=ws.head(), dev_member="m-dev")

    ctx = _review_project_context(store, ws, pr)
    # merged surface reflects the sibling's merged file (git truth)
    assert "src/types.py" in ctx
    # this PR's own new file is surfaced as an addition
    assert "src/calculator.py" in ctx
    # Definition of Done + blockers still present
    assert "add+sub" in ctx
