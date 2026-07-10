"""F087 Slice 3 — stale-base revalidation.

After a PR lands, ``master`` moves; any other mergeable PR was validated against
an older base. ``update_branch_from_base`` brings the new master into the stale
branch (conflict-aware) and ``_revalidate_stale_prs`` demotes the PR back through
re-test (or to a resolve task on conflict) so a clean-but-untested integration
can never merge.
"""
from __future__ import annotations

from pathlib import Path

from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.runner import _revalidate_stale_prs
from errorta_council.coding.topology import DEV, TESTER
from errorta_council.coding.workspace import CodingWorkspace


def _workspace(project_id: str) -> tuple[LedgerStore, CodingWorkspace]:
    store = LedgerStore(project_id)
    store.create_project(north_star="n", definition_of_done="d", target="new",
                         repo_path=None)
    ws = CodingWorkspace(project_id, store)
    ws.setup(target="new", repo_path=None)
    return store, ws


# --- update_branch_from_base (real git) ------------------------------------ #

def test_update_branch_from_base_brings_in_new_master(tmp_errorta_home: Path) -> None:
    _store, ws = _workspace("sb-update")
    # Branch A adds a.py; branch B adds b.py — independent files (no conflict).
    ws.start_task_branch("a")
    ws.write_file("a.py", "A = 1\n", task_id="a")
    ws.start_task_branch("b")
    ws.write_file("b.py", "B = 2\n", task_id="b")

    # A lands first -> master now has a.py. B's branch does NOT yet.
    assert ws.merge_pr(ws.task_branch("a"))["merged"] is True

    res = ws.update_branch_from_base("b", ws.task_branch("b"))
    assert res["updated"] is True
    assert res["changed"] is True            # the merge actually moved B's head
    # B's worktree now contains BOTH the integrated master file and its own.
    root_b = ws.task_root("b", branch=ws.task_branch("b"))
    assert (root_b / "a.py").exists()
    assert (root_b / "b.py").exists()


def test_update_branch_from_base_no_change_when_already_current(
    tmp_errorta_home: Path,
) -> None:
    _store, ws = _workspace("sb-current")
    ws.start_task_branch("a")
    ws.write_file("a.py", "A = 1\n", task_id="a")
    # master has not moved since branch a was cut.
    res = ws.update_branch_from_base("a", ws.task_branch("a"))
    assert res["updated"] is True
    assert res["changed"] is False           # nothing to revalidate


def test_update_branch_from_base_reports_conflict(tmp_errorta_home: Path) -> None:
    _store, ws = _workspace("sb-conflict")
    # Seed shared.py on master so both branches edit the SAME file -> conflict.
    ws.start_task_branch("seed")
    ws.write_file("shared.py", "x = 0\n", task_id="seed")
    assert ws.merge_pr(ws.task_branch("seed"))["merged"] is True

    ws.start_task_branch("a", base="master")
    ws.write_file("shared.py", "x = 1\n", task_id="a")
    ws.start_task_branch("b", base="master")
    ws.write_file("shared.py", "x = 2\n", task_id="b")

    assert ws.merge_pr(ws.task_branch("a"))["merged"] is True
    res = ws.update_branch_from_base("b", ws.task_branch("b"))
    assert res["updated"] is False
    assert res["conflicts"]                  # conflicted paths reported
    assert "shared.py" in res["conflicts"]


# --- _revalidate_stale_prs (runner helper) --------------------------------- #

def _mergeable_pr(store: LedgerStore, ws: CodingWorkspace, task_id: str,
                  *, file: str) -> dict:
    dev = store.add_task(title=f"impl {task_id}", role=DEV)
    branch = ws.start_task_branch(dev.task_id)
    ws.write_file(file, f"# {file}\n", task_id=dev.task_id)
    pr = store.record_pr(task_id=dev.task_id, branch=branch,
                         head=ws.branch_head(branch), dev_member="m-dev")
    store.update_pr(pr["pr_id"], reviewer_approved=True, reviewed_head=pr["head"],
                    tests_passed=True, status="mergeable")
    return store.get_pr(pr["pr_id"])


def test_revalidate_demotes_other_mergeable_pr(tmp_errorta_home: Path) -> None:
    store, ws = _workspace("sb-reval")
    pr_a = _mergeable_pr(store, ws, "a", file="a.py")
    pr_b = _mergeable_pr(store, ws, "b", file="b.py")

    # A lands -> master moves -> B is stale (validated against the old base).
    assert ws.merge_pr(pr_a["branch"])["merged"] is True
    _revalidate_stale_prs(store, ws, just_merged_pr_id=pr_a["pr_id"])

    after = store.get_pr(pr_b["pr_id"])
    assert after["status"] == "changes_requested"   # demoted out of mergeable
    assert after["tests_passed"] is False           # tests now stale
    assert after["reviewer_approved"] is True        # code review still valid
    # a re-test task was enqueued for B
    assert any(t.role == TESTER and t.pr_id == pr_b["pr_id"]
               and t.title.startswith("re-test")
               for t in store.list_tasks())


def test_revalidate_fails_closed_when_update_raises(tmp_errorta_home: Path) -> None:
    """A workspace/ledger error revalidating one PR must NOT leave it mergeable
    against a moved master, and must NOT abort revalidating the others."""
    store, ws = _workspace("sb-reval-error")
    pr_a = _mergeable_pr(store, ws, "a", file="a.py")
    pr_b = _mergeable_pr(store, ws, "b", file="b.py")
    pr_c = _mergeable_pr(store, ws, "c", file="c.py")
    assert ws.merge_pr(pr_a["branch"])["merged"] is True

    # Make revalidating B blow up; C must still be revalidated.
    real = ws.update_branch_from_base

    def boom(task_id, branch, **kw):
        if branch == pr_b["branch"]:
            raise RuntimeError("worktree exploded")
        return real(task_id, branch, **kw)

    ws.update_branch_from_base = boom  # type: ignore[method-assign]
    _revalidate_stale_prs(store, ws, just_merged_pr_id=pr_a["pr_id"])

    b_after = store.get_pr(pr_b["pr_id"])
    assert b_after["status"] == "changes_requested"   # fail-closed, not mergeable
    assert b_after["tests_passed"] is False
    c_after = store.get_pr(pr_c["pr_id"])
    assert c_after["status"] != "mergeable"           # C still got revalidated
    assert any(t.role == TESTER and t.pr_id == pr_b["pr_id"] for t in store.list_tasks())


def test_revalidate_conflict_routes_to_resolve_task(tmp_errorta_home: Path) -> None:
    store, ws = _workspace("sb-reval-conflict")
    # Seed shared.py so both PRs touch the same file -> integration conflict.
    seed = store.add_task(title="seed", role=DEV)
    seed_branch = ws.start_task_branch(seed.task_id)
    ws.write_file("shared.py", "x = 0\n", task_id=seed.task_id)
    seed_pr = store.record_pr(task_id=seed.task_id, branch=seed_branch,
                              head=ws.branch_head(seed_branch), dev_member="m-dev")
    store.update_pr(seed_pr["pr_id"], status="mergeable")
    assert ws.merge_pr(seed_branch)["merged"] is True

    pr_a = _mergeable_pr_same_file(store, ws, "a", value=1)
    pr_b = _mergeable_pr_same_file(store, ws, "b", value=2)

    assert ws.merge_pr(pr_a["branch"])["merged"] is True
    _revalidate_stale_prs(store, ws, just_merged_pr_id=pr_a["pr_id"])

    after = store.get_pr(pr_b["pr_id"])
    assert after["status"] == "conflict"
    assert after["tests_passed"] is False
    assert any(t.role == DEV and t.title.startswith("resolve conflict")
               for t in store.list_tasks())


def _mergeable_pr_same_file(store: LedgerStore, ws: CodingWorkspace, task_id: str,
                            *, value: int) -> dict:
    dev = store.add_task(title=f"impl {task_id}", role=DEV)
    branch = ws.start_task_branch(dev.task_id, base="master")
    ws.write_file("shared.py", f"x = {value}\n", task_id=dev.task_id)
    pr = store.record_pr(task_id=dev.task_id, branch=branch,
                         head=ws.branch_head(branch), dev_member="m-dev")
    store.update_pr(pr["pr_id"], reviewer_approved=True, reviewed_head=pr["head"],
                    tests_passed=True, status="mergeable")
    return store.get_pr(pr["pr_id"])


def test_revalidate_keeps_pr_already_on_new_base(tmp_errorta_home: Path) -> None:
    store, ws = _workspace("sb-reval-current")
    pr_a = _mergeable_pr(store, ws, "a", file="a.py")
    # B is branched AFTER A merged, so it already contains the new master.
    assert ws.merge_pr(pr_a["branch"])["merged"] is True
    pr_b = _mergeable_pr(store, ws, "b", file="b.py")  # off the new master

    _revalidate_stale_prs(store, ws, just_merged_pr_id="pr-none")
    after = store.get_pr(pr_b["pr_id"])
    assert after["status"] == "mergeable"            # still genuinely mergeable
