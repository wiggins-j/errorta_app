"""F087-17 — git branch-per-task layer + PR records."""
from __future__ import annotations

from pathlib import Path

from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.workspace import CodingWorkspace


def _ws(pid: str) -> CodingWorkspace:
    s = LedgerStore(pid)
    s.create_project(north_star="n", definition_of_done="d", target="new", repo_path=None)
    ws = CodingWorkspace(pid, s)
    ws.setup(target="new", repo_path=None)
    return ws


def test_integration_branch_is_master(tmp_errorta_home: Path) -> None:
    ws = _ws("bw1")
    assert ws._ws.current_branch() == "master"


def test_branch_off_master_sees_prior_work_and_merge_accumulates(tmp_errorta_home: Path) -> None:
    ws = _ws("bw2")
    # task 1: add() on its own branch, merged to master
    ws.start_task_branch("t1")
    ws.write_file("calc.py", "def add(a, b):\n    return a + b\n", task_id="t1")
    res1 = ws.merge_pr(ws.task_branch("t1"))
    assert res1["merged"] is True

    # task 2 branches off master -> read-back SEES add(); dev extends with subtract
    ws.start_task_branch("t2")
    back = ws.read_back()
    assert "def add(a, b):" in back  # accumulation: prior work is visible
    ws.write_file("calc.py",
                  "def add(a, b):\n    return a + b\n\n"
                  "def subtract(a, b):\n    return a - b\n", task_id="t2")
    res2 = ws.merge_pr(ws.task_branch("t2"))
    assert res2["merged"] is True

    # master now has BOTH functions (accumulated, not clobbered)
    ws.checkout("master")
    merged = ws._ws.read_file("calc.py")
    assert "def add" in merged and "def subtract" in merged


def test_merge_conflict_is_reported_and_aborted(tmp_errorta_home: Path) -> None:
    ws = _ws("bw3")
    ws.start_task_branch("seed")
    ws.write_file("f.py", "x = 1\n", task_id="seed")
    ws.merge_pr(ws.task_branch("seed"))

    ws.start_task_branch("a", base="master")
    ws.write_file("f.py", "x = 2\n", task_id="a")
    ws.start_task_branch("b", base="master")
    ws.write_file("f.py", "x = 3\n", task_id="b")

    assert ws.merge_pr(ws.task_branch("a"))["merged"] is True
    conflict = ws.merge_pr(ws.task_branch("b"))
    assert conflict["merged"] is False
    assert "f.py" in conflict["conflicts"]
    # fail-closed: the tree is not left half-merged
    ws.checkout("master")
    assert ws._ws.read_file("f.py") == "x = 2\n"


def test_pr_diff_is_branch_vs_master(tmp_errorta_home: Path) -> None:
    ws = _ws("bw4")
    ws.start_task_branch("t1")
    ws.write_file("new.py", "y = 1\n", task_id="t1")
    diff = ws.pr_diff(ws.task_branch("t1"))
    assert "new.py" in diff and "+y = 1" in diff


# --- PR records (ledger) ----------------------------------------------------


def test_pr_record_lifecycle(tmp_errorta_home: Path) -> None:
    s = LedgerStore("prl")
    s.create_project(north_star="n", definition_of_done="d", target="new", repo_path=None)
    pr = s.record_pr(task_id="t1", branch="task-t1", head="h1", dev_member="dev-1")
    assert pr["status"] == "open" and pr["reviewer_approved"] is None
    assert s.open_pr_for_task("t1")["pr_id"] == pr["pr_id"]

    s.update_pr(pr["pr_id"], reviewer_approved=True, reviewed_head="h1")
    s.update_pr(pr["pr_id"], tests_passed=True, tested_head="h1", status="mergeable")
    got = s.get_pr(pr["pr_id"])
    assert got["status"] == "mergeable" and got["tests_passed"] is True

    s.update_pr(pr["pr_id"], status="merged")
    assert s.open_pr_for_task("t1") is None  # terminal -> no longer "open"
    assert len(s.list_prs()) == 1
