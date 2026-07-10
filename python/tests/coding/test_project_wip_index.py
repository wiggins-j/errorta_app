"""F088-05 — WIP operational index: open PRs, failures, findings, ownership.

WIP is current operational state — always lower-authority than merged truth and
excluded from durable-only retrieval.
"""
from __future__ import annotations

from pathlib import Path

from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.workspace import CodingWorkspace
from errorta_project_grounding.memory_store import MemoryQuery, ProjectMemoryStore
from errorta_project_grounding.update_pipeline import sync_from_ledger


class _Fail:
    command_ids = ["unit"]
    results: list = []
    unknown_ids: list = []
    passed = False
    sandbox = "seatbelt"


def _project(tmp: Path, pid: str):
    s = LedgerStore(pid, root=tmp)
    s.create_project(north_star="n", definition_of_done="d", target="new", repo_path=None)
    ws = CodingWorkspace(pid, s)
    ws.setup(target="new", repo_path=None)
    return s, ws


def _open_pr(s, ws, *, title: str, files: dict[str, str]):
    t = s.add_task(title=title, role="dev")
    branch = ws.start_task_branch(t.task_id)
    for path, content in files.items():
        ws.write_file(path, content, task_id=t.task_id)
    pr = s.record_pr(task_id=t.task_id, branch=branch, head=ws.head(), dev_member="m-dev")
    return t, pr


def _mem(tmp: Path, pid: str) -> ProjectMemoryStore:
    return ProjectMemoryStore(pid, root=tmp)


def test_failed_test_is_wip_not_durable(tmp_path: Path) -> None:
    s, ws = _project(tmp_path, "w1")
    t, _pr = _open_pr(s, ws, title="impl", files={"a.py": "x = 1\n"})
    s.record_test_run(_Fail(), task_id=t.task_id, head=ws.head())
    sync_from_ledger(s, workspace=ws)
    mem = _mem(tmp_path, "w1")

    wip = mem.query(MemoryQuery(authorities=("wip",), limit=500))
    assert any(i.source_type == "failed_test" for i in wip)
    durable = mem.query(MemoryQuery(authorities=("durable_truth",), limit=500))
    assert not any(i.source_type in ("failed_test", "test_evidence") for i in durable)


def test_open_pr_and_touched_files_indexed_as_wip(tmp_path: Path) -> None:
    s, ws = _project(tmp_path, "w2")
    _open_pr(s, ws, title="impl", files={"calc.py": "x = 1\n", "util.py": "y = 2\n"})
    sync_from_ledger(s, workspace=ws)
    wip = _mem(tmp_path, "w2").query(MemoryQuery(authorities=("wip",), limit=500))
    types = {i.source_type for i in wip}
    assert "open_pr" in types and "touched_file" in types
    owned = {i.source_ref.path for i in wip if i.source_type == "touched_file"}
    assert owned == {"calc.py", "util.py"}


def test_wip_overlap_discoverable_by_path(tmp_path: Path) -> None:
    s, ws = _project(tmp_path, "w3")
    # two live PRs that both touch calc.py
    _open_pr(s, ws, title="feature A", files={"calc.py": "a = 1\n"})
    _open_pr(s, ws, title="feature B", files={"calc.py": "b = 2\n", "extra.py": "c = 3\n"})
    sync_from_ledger(s, workspace=ws)
    mem = _mem(tmp_path, "w3")
    overlap = mem.query(MemoryQuery(path="calc.py", source_type="touched_file", limit=500))
    branches = {i.metadata.get("branch") for i in overlap}
    assert len(branches) == 2  # both PRs surface as owners of calc.py


def test_wip_excluded_from_default_and_durable_queries(tmp_path: Path) -> None:
    s, ws = _project(tmp_path, "w4")
    _open_pr(s, ws, title="impl", files={"a.py": "x = 1\n"})
    sync_from_ledger(s, workspace=ws)
    mem = _mem(tmp_path, "w4")
    # WIP IS lower-authority but still returned by a default query (after durable);
    # a durable-only query must never include it.
    durable = mem.query(MemoryQuery(authorities=("durable_truth",), limit=500))
    assert not any(i.authority == "wip" for i in durable)
    # and WIP is labeled so retrieval can rank it below merged truth
    wip = mem.query(MemoryQuery(authorities=("wip",), limit=500))
    assert all(i.metadata.get("lower_authority") for i in wip)


def test_review_finding_indexed_as_wip(tmp_path: Path) -> None:
    s, ws = _project(tmp_path, "w5")
    t, pr = _open_pr(s, ws, title="impl", files={"a.py": "x = 1\n"})
    s.record_decision(title="changes requested", context="review",
                      choice="review_rejected", rationale="missing tests",
                      extra={"reviewed_head": ws.head(), "pr_id": pr["pr_id"]},
                      related_task_ids=[t.task_id])
    sync_from_ledger(s, workspace=ws)
    wip = _mem(tmp_path, "w5").query(MemoryQuery(authorities=("wip",), limit=500))
    assert any(i.source_type == "review_finding" for i in wip)
