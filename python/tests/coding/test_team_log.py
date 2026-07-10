"""Team Log — human-readable narrative projection over the ledger."""
from __future__ import annotations

from pathlib import Path

from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.team_log import build_team_log


def _store(tmp: Path, pid: str = "tl") -> LedgerStore:
    s = LedgerStore(pid, root=tmp)
    s.create_project(north_star="Build a calculator", definition_of_done="tests pass",
                     target="new", repo_path=None)
    return s


def _msgs(entries):
    return [e["message"] for e in entries]


def test_empty_project_has_only_north_star(tmp_path):
    s = _store(tmp_path)
    log = build_team_log(s)
    assert len(log) == 1
    # role tag drives the UI badge; member is empty for PM; message carries NO
    # actor prefix (the UI renders the tag + member separately).
    assert log[0]["role"] == "pm" and log[0]["member"] == ""
    assert "actor" not in log[0]
    assert log[0]["message"].startswith("reviewed the North Star")


def test_full_flow_narrative(tmp_path):
    s = _store(tmp_path)
    t = s.add_task(title="Implement add()", role="dev")
    s.record_turn(role="dev", member_id="m-2", task_id=t.task_id, prompt="p",
                  response="r", outcome="pr_opened")
    s.record_decision(title="opened PR", context="c", choice="pr_opened",
                      rationale="done", related_task_ids=[t.task_id])
    s.record_decision(title="ctx", context="c", choice="context_request",
                      rationale="need spec", related_task_ids=[t.task_id])
    s.record_decision(title="review", context="c", choice="review_approved",
                      rationale="lgtm", related_task_ids=[t.task_id])
    s.record_decision(title="test", context="c", choice="tested_pass",
                      rationale="green", related_task_ids=[t.task_id])
    s.record_decision(title="merge", context="c", choice="pr_merged",
                      rationale="ok", related_task_ids=[t.task_id])

    log = build_team_log(s)
    blob = "\n".join(_msgs(log))
    # messages carry NO actor prefix (no doubled "Developer (m-2) Developer (m-2)")
    assert "reviewed the North Star" in blob
    assert "created task: Implement add()" in blob
    assert "completed the work and opened a PR for: Implement add()" in blob
    assert "requested more context for: Implement add()" in blob
    assert "delivered context for: Implement add()" in blob
    assert "reviewed and approved: Implement add()" in blob
    assert "ran the tests for Implement add(): passed" in blob
    assert "merged Implement add() into the project" in blob
    # no message repeats the role label (that would double under the UI tag)
    assert "Developer (" not in blob and "Reviewer (" not in blob

    # member attribution lives in a dedicated field, not the message
    pr = next(e for e in log if e["kind"] == "pr_opened")
    assert pr["role"] == "dev" and pr["member"] == "m-2"
    merged = next(e for e in log if e["kind"] == "pr_merged")
    assert merged["role"] == "pm" and merged["member"] == ""


def test_entries_are_chronological_and_noise_is_omitted(tmp_path):
    s = _store(tmp_path)
    t = s.add_task(title="x", role="dev")
    # noise choices must NOT appear
    for noisy in ("dev_turn_rejected", "worker_turn_requeued", "stale_review_head"):
        s.record_decision(title="n", context="c", choice=noisy, rationale="r",
                          related_task_ids=[t.task_id])
    s.record_decision(title="merge", context="c", choice="pr_merged",
                      rationale="ok", related_task_ids=[t.task_id])
    log = build_team_log(s)
    kinds = {e["kind"] for e in log}
    assert "pr_merged" in kinds
    assert not (kinds & {"dev_turn_rejected", "worker_turn_requeued", "stale_review_head"})
    # sorted oldest-first
    ats = [e["at"] for e in log]
    assert ats == sorted(ats)


def test_reviewer_and_tester_tasks_not_listed_as_created(tmp_path):
    s = _store(tmp_path)
    s.add_task(title="dev work", role="dev")
    s.add_task(title="review PR foo", role="reviewer")
    created = [e["message"] for e in build_team_log(s) if e["kind"] == "task_created"]
    assert any("dev work" in m for m in created)
    assert not any("review PR foo" in m for m in created)


def test_review_and_test_entries_name_source_dev_task(tmp_path):
    s = _store(tmp_path)
    dev = s.add_task(title="Implement shipping cost()", role="dev")
    review = s.add_task(title="review PR: Implement shipping cost()", role="reviewer",
                        pr_id="pr-1", depends_on=[dev.task_id])
    test = s.add_task(title="test PR: task-shipping-cost", role="tester",
                      pr_id="pr-1", depends_on=[review.task_id])
    s.record_turn(role="reviewer", member_id="rev-1", task_id=review.task_id,
                  prompt="p", response="r", outcome="pr_reviewed")
    s.record_turn(role="tester", member_id="test-1", task_id=test.task_id,
                  prompt="p", response="r", outcome="pr_tested")
    s.record_decision(title="review verdict", context="pr pr-1", choice="review_approved",
                      rationale="ok", related_task_ids=[review.task_id, dev.task_id])
    s.record_decision(title="tested PR", context="pr pr-1", choice="tested_pass",
                      rationale="ok", related_task_ids=[test.task_id, dev.task_id])

    log = build_team_log(s)
    blob = "\n".join(_msgs(log))
    assert "reviewed and approved: Implement shipping cost()" in blob
    assert "ran the tests for Implement shipping cost(): passed" in blob
    assert "task-shipping-cost: passed" not in blob
    # the review/test entries carry the member id in the dedicated field
    rev = next(e for e in log if e["kind"] == "review_approved")
    assert rev["role"] == "reviewer" and rev["member"] == "rev-1"
    tst = next(e for e in log if e["kind"] == "tested_pass")
    assert tst["role"] == "tester" and tst["member"] == "test-1"


def test_human_file_edit_renders_as_user_role(tmp_path):
    # F105 Slice E (D2): a human edit is the USER. record_decision stamps the
    # path at top level via extra=; the Team Log emits role "user", member "",
    # message "edited <path>" (no actor prefix, no sha/head/bytes in the prose).
    s = _store(tmp_path)
    s.record_decision(
        title="human edited file: src/app.py", context="project tl",
        choice="human_file_edit", rationale="bytes=12 sha256=deadbeef",
        related_task_ids=[],
        extra={"path": "src/app.py", "content_sha256": "deadbeef", "head": "abc123"})

    log = build_team_log(s)
    edits = [e for e in log if e["kind"] == "human_file_edit"]
    assert len(edits) == 1
    e = edits[0]
    assert e["role"] == "user"
    assert e["member"] == ""
    assert e["message"] == "edited src/app.py"
    # internal save metadata stays out of the prose.
    assert "deadbeef" not in e["message"] and "abc123" not in e["message"]
    assert "bytes" not in e["message"]


def test_route(tmp_errorta_home):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from errorta_app.routes import coding as coding_routes
    s = LedgerStore("tlr")
    s.create_project(north_star="n", definition_of_done="d", target="new", repo_path=None)
    app = FastAPI()
    app.include_router(coding_routes.router)
    c = TestClient(app, headers={"x-errorta-origin": "tauri-ui"})
    r = c.get("/coding/projects/tlr/team-log")
    assert r.status_code == 200
    assert isinstance(r.json()["entries"], list)
    assert c.get("/coding/projects/ghost/team-log").status_code == 404
