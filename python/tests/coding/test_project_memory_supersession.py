"""F088-06 — staleness/supersession + WIP lifecycle.

Changed files retire prior chunks (non-destructively); terminal PRs retire their
WIP. Stale rows leave default retrieval but stay in history for audit.
"""
from __future__ import annotations

from pathlib import Path

from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.workspace import CodingWorkspace
from errorta_project_grounding.memory_store import MemoryQuery, ProjectMemoryStore
from errorta_project_grounding.update_pipeline import sync_from_ledger


class _Pass:
    command_ids = ["unit"]
    results: list = []
    unknown_ids: list = []
    passed = True
    sandbox = "seatbelt"


def _project(tmp: Path, pid: str):
    s = LedgerStore(pid, root=tmp)
    s.create_project(north_star="n", definition_of_done="d", target="new", repo_path=None)
    ws = CodingWorkspace(pid, s)
    ws.setup(target="new", repo_path=None)
    return s, ws


def _merge_pr(s, ws, *, title: str, files: dict[str, str]):
    t = s.add_task(title=title, role="dev")
    branch = ws.start_task_branch(t.task_id)
    for path, content in files.items():
        ws.write_file(path, content, task_id=t.task_id)
    head = ws.head()
    pr = s.record_pr(task_id=t.task_id, branch=branch, head=head, dev_member="m-dev")
    s.record_decision(title="review", context="c", choice="review_approved",
                      rationale="ok", extra={"reviewed_head": head, "pr_id": pr["pr_id"]})
    s.record_test_run(_Pass(), task_id=t.task_id, head=head)
    res = ws.merge_pr(branch)
    mhead = res.get("head", head)
    s.update_pr(pr["pr_id"], status="merged", head=mhead,
                reviewer_approved=True, reviewed_head=head,
                tests_passed=True, tested_head=head)
    s.record_episode(title=f"merged {branch}", summary=f"merged {title}",
                     head=mhead, related_task_ids=[t.task_id])
    return t, pr


def test_later_merge_supersedes_old_chunk(tmp_path: Path) -> None:
    s, ws = _project(tmp_path, "s1")
    _merge_pr(s, ws, title="v1", files={"calc.py": "def add(a, b):\n    return a + b\n"})
    sync_from_ledger(s, workspace=ws)
    _merge_pr(s, ws, title="v2",
              files={"calc.py": "def add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return a - b\n"})
    sync_from_ledger(s, workspace=ws)

    mem = ProjectMemoryStore("s1", root=tmp_path)
    active = [i for i in mem.query(MemoryQuery(authorities=("durable_truth",), limit=500))
              if i.source_type == "code_chunk" and i.source_ref.path == "calc.py"]
    assert len(active) == 1  # only the newest chunk stays active

    history = [i for i in mem.query(MemoryQuery(authorities=("durable_truth",),
                                                source_type="code_chunk",
                                                include_history=True, limit=500))
               if i.source_ref.path == "calc.py"]
    assert len(history) == 2  # the old chunk is retained, not deleted
    superseded = [i for i in history if i.valid_until]
    assert len(superseded) == 1
    assert superseded[0].superseded_by == active[0].memory_id


def test_merged_pr_wip_is_retired(tmp_path: Path) -> None:
    s, ws = _project(tmp_path, "s2")
    t = s.add_task(title="impl", role="dev")
    branch = ws.start_task_branch(t.task_id)
    ws.write_file("a.py", "x = 1\n", task_id=t.task_id)
    pr = s.record_pr(task_id=t.task_id, branch=branch, head=ws.head(), dev_member="m-dev")
    sync_from_ledger(s, workspace=ws)
    mem = ProjectMemoryStore("s2", root=tmp_path)
    assert mem.query(MemoryQuery(authorities=("wip",), limit=500))  # WIP present

    # PR merges -> its WIP (open_pr + touched_file) is superseded
    res = ws.merge_pr(branch)
    s.update_pr(pr["pr_id"], status="merged", head=res.get("head", ws.head()))
    sync_from_ledger(s, workspace=ws)
    active_wip = mem.query(MemoryQuery(authorities=("wip",), limit=500))
    assert not any(i.source_type in ("open_pr", "touched_file") for i in active_wip)


def test_abandoned_pr_wip_is_retired(tmp_path: Path) -> None:
    s, ws = _project(tmp_path, "s3")
    t = s.add_task(title="impl", role="dev")
    branch = ws.start_task_branch(t.task_id)
    ws.write_file("a.py", "x = 1\n", task_id=t.task_id)
    pr = s.record_pr(task_id=t.task_id, branch=branch, head=ws.head(), dev_member="m-dev")
    sync_from_ledger(s, workspace=ws)
    s.update_pr(pr["pr_id"], status="abandoned")
    sync_from_ledger(s, workspace=ws)
    mem = ProjectMemoryStore("s3", root=tmp_path)
    active_wip = mem.query(MemoryQuery(authorities=("wip",), limit=500))
    assert not any(i.source_ref.pr_id == pr["pr_id"] for i in active_wip)
