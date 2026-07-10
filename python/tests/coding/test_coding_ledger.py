"""F091 — ledger-level PR supersession storage + summary exclusion."""
from __future__ import annotations

from pathlib import Path

from errorta_council.coding.ledger import LedgerStore


def _store(pid: str) -> LedgerStore:
    s = LedgerStore(pid)
    s.create_project(north_star="n", definition_of_done="d", target="new",
                     repo_path=None)
    return s


def test_record_pr_has_superseded_by_field(tmp_errorta_home: Path) -> None:
    s = _store("sup-rec")
    pr = s.record_pr(task_id="t-1", branch="task-t-1", head="h1", dev_member="m")
    assert "superseded_by_pr_id" in pr
    assert pr["superseded_by_pr_id"] is None


def test_update_pr_superseded(tmp_errorta_home: Path) -> None:
    s = _store("sup-upd")
    pr = s.record_pr(task_id="t-1", branch="task-t-1", head="h1", dev_member="m")
    s.update_pr(pr["pr_id"], status="superseded", superseded_by_pr_id="pr-x")
    reloaded = s.get_pr(pr["pr_id"])
    assert reloaded is not None
    assert reloaded["status"] == "superseded"
    assert reloaded["superseded_by_pr_id"] == "pr-x"


def test_pr_state_summary_excludes_superseded(tmp_errorta_home: Path) -> None:
    s = _store("sup-sum")
    a = s.record_pr(task_id="t-a", branch="task-t-a", head="ha", dev_member="m")
    s.record_pr(task_id="t-b", branch="task-t-b", head="hb", dev_member="m")
    s.update_pr(a["pr_id"], status="superseded", superseded_by_pr_id="pr-b")

    summary = s.pr_state_summary()
    open_branches = [p["branch"] for p in summary["open_prs"]]
    # the superseded PR is no longer "outstanding" in the PM's view
    assert "task-t-a" not in open_branches
    # the still-open PR remains
    assert "task-t-b" in open_branches
    assert summary["counts"].get("superseded") == 1
