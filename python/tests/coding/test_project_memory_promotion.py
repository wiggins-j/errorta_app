"""F088-04 — durable-truth promotion: PM decisions + reviewed/tested/merged PRs.

Durable truth is evidence-backed only; raw prose never auto-promotes, and a
broad task title never promotes files it did not actually touch.
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
    ep = s.record_episode(title=f"merged {branch}", summary=f"merged {title}",
                          head=mhead, related_task_ids=[t.task_id])
    return t, pr, ep


def _durable(tmp: Path, pid: str):
    mem = ProjectMemoryStore(pid, root=tmp)
    return mem.query(MemoryQuery(authorities=("durable_truth",), limit=500))


def test_pm_decision_admitted_as_durable_truth(tmp_errorta_home: Path, tmp_path: Path) -> None:
    s, _ws = _project(tmp_path, "p1")
    s.record_decision(title="use sqlite", context="pm_decision",
                      choice="pm_decision", rationale="durable + simple + local")
    sync_from_ledger(s)
    durable = _durable(tmp_path, "p1")
    pm = [i for i in durable if i.source_type == "pm_decision"]
    assert len(pm) == 1
    assert "sqlite" in pm[0].content
    assert pm[0].authority == "durable_truth"
    assert pm[0].source_ref.has_provenance()


def test_merged_pr_promotes_chunks_evidence_and_episode(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    s, ws = _project(tmp_path, "p2")
    _t, _pr, ep = _merge_pr(s, ws, title="implement calculator",
                            files={"calc.py": "def add(a, b):\n    return a + b\n"})
    sync_from_ledger(s, workspace=ws)
    durable = _durable(tmp_path, "p2")
    by_type = {i.source_type for i in durable}
    assert {"code_chunk", "test_evidence", "merge_episode"} <= by_type

    chunk = next(i for i in durable if i.source_type == "code_chunk")
    assert chunk.source_ref.path == "calc.py"
    assert chunk.source_ref.commit and chunk.source_ref.head  # bound to a head

    # the episode is a derived summary — it MUST carry source_ids linking its
    # evidence (chunk + test-evidence ids), per the authority model.
    episode = next(i for i in durable if i.source_type == "merge_episode")
    assert episode.source_ids
    assert episode.metadata.get("episode_id") == ep["episode_id"]
    chunk_and_ev = {i.memory_id for i in durable
                    if i.source_type in ("code_chunk", "test_evidence")}
    assert set(episode.source_ids) <= chunk_and_ev


def test_live_tester_task_run_counts_as_merge_test_evidence(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    s, ws = _project(tmp_path, "p2b")
    dev = s.add_task(title="impl", role="dev")
    branch = ws.start_task_branch(dev.task_id)
    ws.write_file("calc.py", "def add(a, b):\n    return a + b\n", task_id=dev.task_id)
    reviewed_head = ws.head()
    pr = s.record_pr(task_id=dev.task_id, branch=branch, head=reviewed_head,
                     dev_member="m-dev")
    s.update_pr(pr["pr_id"], reviewer_approved=True, reviewed_head=reviewed_head)
    tester = s.add_task(title=f"test PR: {branch}", role="tester", pr_id=pr["pr_id"])
    s.record_test_run(_Pass(), task_id=tester.task_id, head=reviewed_head)
    res = ws.merge_pr(branch)
    merge_head = res.get("head", reviewed_head)
    s.update_pr(pr["pr_id"], status="merged", head=merge_head,
                reviewer_approved=True, reviewed_head=reviewed_head,
                tests_passed=True, tested_head=reviewed_head)
    s.record_episode(title=f"merged {branch}", summary="merged impl",
                     head=merge_head, related_task_ids=[dev.task_id])

    sync_from_ledger(s, workspace=ws)

    durable = _durable(tmp_path, "p2b")
    evidence = [i for i in durable if i.source_type == "test_evidence"]
    assert len(evidence) == 1
    assert evidence[0].source_ref.task_id == tester.task_id
    assert any(i.source_type == "code_chunk" for i in durable)


def test_merged_pr_without_bound_passing_tests_is_not_durable(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    s, ws = _project(tmp_path, "p2c")
    dev = s.add_task(title="impl", role="dev")
    branch = ws.start_task_branch(dev.task_id)
    ws.write_file("calc.py", "def add(a, b):\n    return a + b\n", task_id=dev.task_id)
    reviewed_head = ws.head()
    pr = s.record_pr(task_id=dev.task_id, branch=branch, head=reviewed_head,
                     dev_member="m-dev")
    res = ws.merge_pr(branch)
    merge_head = res.get("head", reviewed_head)
    s.update_pr(pr["pr_id"], status="merged", head=merge_head,
                reviewer_approved=True, reviewed_head=reviewed_head,
                tests_passed=True, tested_head=reviewed_head)
    s.record_episode(title=f"merged {branch}", summary="merged without test evidence",
                     head=merge_head, related_task_ids=[dev.task_id])

    sync_from_ledger(s, workspace=ws)

    durable = _durable(tmp_path, "p2c")
    assert not any(i.source_type == "code_chunk" for i in durable)
    assert not any(i.source_type == "test_evidence" for i in durable)


def test_promotion_uses_touched_files_not_task_title(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    s, ws = _project(tmp_path, "p3")
    # a broad-titled merged PR that ONLY touches calc.py
    _merge_pr(s, ws, title="build the entire application end to end",
              files={"calc.py": "def add(a, b):\n    return a + b\n"})
    # an unrelated file written by a DIFFERENT, unmerged task
    other = s.add_task(title="docs", role="dev")
    ws.start_task_branch(other.task_id)
    ws.write_file("unrelated.py", "x = 1\n", task_id=other.task_id)

    sync_from_ledger(s, workspace=ws)
    chunk_paths = {i.source_ref.path for i in _durable(tmp_path, "p3")
                   if i.source_type == "code_chunk"}
    assert chunk_paths == {"calc.py"}  # the unmerged unrelated.py is NOT promoted


def test_sync_is_idempotent(tmp_errorta_home: Path, tmp_path: Path) -> None:
    s, ws = _project(tmp_path, "p4")
    s.record_decision(title="d", context="pm_decision", choice="pm_decision",
                      rationale="r")
    _merge_pr(s, ws, title="impl", files={"a.py": "x = 1\n"})
    sync_from_ledger(s, workspace=ws)
    first = len(_durable(tmp_path, "p4"))
    sync_from_ledger(s, workspace=ws)
    sync_from_ledger(s, workspace=ws)
    assert len(_durable(tmp_path, "p4")) == first  # no duplicate rows
