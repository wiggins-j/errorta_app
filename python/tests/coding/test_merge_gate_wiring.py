"""F087-13 WS-1 — the evidence merge gate is wired into preview + accept."""
from __future__ import annotations

from pathlib import Path

import pytest

from errorta_council.coding.evidence import gather_merge_evidence, merge_review
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.workspace import CodingWorkspace


def _project(tmp_path: Path, pid: str) -> tuple[LedgerStore, CodingWorkspace]:
    s = LedgerStore(pid, root=tmp_path)
    s.create_project(north_star="n", definition_of_done="d", target="new", repo_path=None)
    # F146 Slice D: register a test command so the tests gate is REQUIRED here
    # (these gate tests assert `tests_missing`). A genuinely test-less,
    # non-runnable project now vacuously satisfies that gate — covered separately
    # in test_f146_gate_consistency.py.
    s.set_test_commands({"unit": {"argv": ["python", "-c", "pass"], "cwd": ".",
                                  "timeout_seconds": 30}})
    ws = CodingWorkspace(pid, s)
    ws.setup(target="new", repo_path=None)
    return s, ws


def test_evidence_reflects_ledger_state(tmp_path: Path) -> None:
    s, ws = _project(tmp_path, "ev")
    t = s.add_task(title="impl", role="dev")
    ws.write_file("a.py", "x = 1\n", task_id=t.task_id)
    ev = gather_merge_evidence(s, ws)
    # an open task, no review, no test -> all the corresponding signals are unset
    assert any(x["state"] not in ("done", "dropped") for x in ev["tasks"])
    assert ev["reviewed_approved"] is None
    assert ev["tests_passed"] is None
    assert ev["definition_of_done_met"] is False


def test_gate_blocks_incomplete_then_allows_when_clear(tmp_path: Path) -> None:
    s, ws = _project(tmp_path, "gate")
    t = s.add_task(title="impl", role="dev")
    ws.write_file("a.py", "x = 1\n", task_id=t.task_id)

    blocked = merge_review(s, ws)["_gate"]
    codes = {b.code for b in blocked.blockers}
    assert not blocked.allowed
    assert "open_tasks" in codes and "unreviewed_changes" in codes
    assert "tests_missing" in codes and "definition_of_done" in codes

    # satisfy every condition — F087-15 H1: review/test must be bound to the
    # CURRENT head, so stamp the worktree head on both.
    head = ws.head()
    s.update_task(t.task_id, state="done")
    s.record_decision(title="r", context="c", choice="review_approved", rationale="ok",
                      extra={"reviewed_head": head})

    class _Session:
        command_ids = ["unit"]
        results: list = []
        unknown_ids: list = []
        passed = True
    s.record_test_run(_Session(), task_id=t.task_id, head=head)
    raw = s.get_project().to_dict()
    raw["status"] = "done"
    from errorta_council.coding.ledger import _atomic_write_json
    _atomic_write_json(s._project_path, raw)

    cleared = merge_review(s, ws)["_gate"]
    assert cleared.allowed is True
    assert cleared.blockers == []


def test_review_rejected_blocks(tmp_path: Path) -> None:
    s, ws = _project(tmp_path, "rej")
    t = s.add_task(title="impl", role="dev")
    s.update_task(t.task_id, state="done")
    ws.write_file("a.py", "x = 1\n", task_id=t.task_id)
    # an approval then a later rejection (both bound to the current head) ->
    # the latest verdict wins (rejected)
    head = ws.head()
    s.record_decision(title="r", context="c", choice="review_approved", rationale="ok",
                      extra={"reviewed_head": head})
    s.record_decision(title="r", context="c", choice="review_rejected", rationale="no",
                      extra={"reviewed_head": head})
    gate = merge_review(s, ws)["_gate"]
    assert "review_rejected" in {b.code for b in gate.blockers}


def test_structured_file_diffs_present(tmp_path: Path) -> None:
    s, ws = _project(tmp_path, "diff")
    t = s.add_task(title="impl", role="dev")
    ws.write_file("a.py", "x = 1\n", task_id=t.task_id)
    review = merge_review(s, ws)
    # the cumulative diff is parsed into per-file structured entries
    assert any(fd["path"].endswith("a.py") for fd in review["file_diffs"])
    assert all("changeType" in fd for fd in review["file_diffs"])


# --- route-level enforcement ------------------------------------------------


def _client(tmp_errorta_home):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from errorta_app.routes import coding as coding_routes
    app = FastAPI()
    app.include_router(coding_routes.router)
    return TestClient(app, headers={"x-errorta-origin": "tauri-ui"})


def _seed_existing_project(c, pid: str):
    # a 'new'-target project so the worktree exists; write a file to make a diff.
    c.post("/coding/projects", json={"project_id": pid, "north_star": "n",
           "definition_of_done": "d", "target": "new"})
    from errorta_council.coding.ledger import LedgerStore
    from errorta_council.coding.workspace import CodingWorkspace
    s = LedgerStore(pid)
    t = s.add_task(title="impl", role="dev")
    ws = CodingWorkspace(pid, s)
    ws.setup(target="new", repo_path=None)
    ws.write_file("a.py", "x = 1\n", task_id=t.task_id)
    return s, t


def test_accept_blocked_by_gate_returns_409(tmp_errorta_home) -> None:
    c = _client(tmp_errorta_home)
    _seed_existing_project(c, "racc")
    r = c.post("/coding/projects/racc/worktree/accept", json={"confirm": True})
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "merge_gate_blocked"
    assert any(b["code"] == "open_tasks" for b in r.json()["detail"]["gate"]["blockers"])


def test_accept_override_bypasses_gate(tmp_errorta_home) -> None:
    c = _client(tmp_errorta_home)
    s, _t = _seed_existing_project(c, "rovr")
    # confirm alone is refused; an explicit separate override:true merges (new
    # target -> returns the worktree as deliverable) and records the override.
    blocked = c.post("/coding/projects/rovr/worktree/accept", json={"confirm": True})
    assert blocked.status_code == 409
    ok = c.post("/coding/projects/rovr/worktree/accept",
                json={"confirm": True, "override": True})
    assert ok.status_code == 200
    assert any(d["choice"] == "merge_gate_override" for d in s.list_decisions())


def test_preview_carries_file_diffs_and_gate(tmp_errorta_home) -> None:
    c = _client(tmp_errorta_home)
    _seed_existing_project(c, "rprev")
    r = c.get("/coding/projects/rprev/worktree")
    assert r.status_code == 200
    body = r.json()
    assert "file_diffs" in body and "gate" in body
    assert body["gate"]["allowed"] is False
