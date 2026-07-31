"""SPEC-32 — a grounded reviewer opens the file; it does not reject a truncated diff.

Run 10's delivery reviewer rejected a truncated acceptance test it could have
opened from its mounted worktree, costing a revise cycle. With repo_read on, a
truncated diff is a READ CUE, not a rejection; without a mount, it still fails
closed (the code is genuinely unseeable).
"""
from __future__ import annotations

import json
from pathlib import Path

from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.runner import (
    _REVIEW_DIFF_CAP,
    _delivery_review_prompt,
    _review_pr_prompt,
)

_PR = {"branch": "feat/x", "head": "abc123", "task_id": "t-dev"}


def _tasks(store):
    dtask = store.add_task(title="review PR: Impl feature", role="reviewer",
                           detail="do the thing")
    return dtask


def _truncated() -> str:
    return "X" * (_REVIEW_DIFF_CAP + 5000)


def test_pr_reviewer_with_mount_does_not_reject_on_truncation(tmp_errorta_home: Path) -> None:
    store = LedgerStore("spec32-pr-mount")
    store.create_project(north_star="g", definition_of_done="d", target="new", repo_path=None)
    t = _tasks(store)
    p = _review_pr_prompt(t, _PR, _truncated(), project_context="", scope_task=t,
                          repo_read=True)
    assert '"approved": false' not in p, "mounted reviewer must not auto-reject"
    assert "OPEN the affected files" in p
    assert "native read tools" in p
    example = p.split("Reply with ONLY a coding_turn.v1 envelope: ", 1)[1]
    example = example.split(". If approved=false", 1)[0]
    assert json.loads(example)["intent"]["approved"] is True


def test_pr_reviewer_without_mount_still_fails_closed(tmp_errorta_home: Path) -> None:
    store = LedgerStore("spec32-pr-nomount")
    store.create_project(north_star="g", definition_of_done="d", target="new", repo_path=None)
    t = _tasks(store)
    p = _review_pr_prompt(t, _PR, _truncated(), project_context="", scope_task=t,
                          repo_read=False)
    assert '"approved": false' in p, "no mount -> unseen code cannot be approved"
    assert "split or reduce" in p


def test_pr_reviewer_with_mount_forbids_toolcalls_turn(tmp_errorta_home: Path) -> None:
    store = LedgerStore("spec32-pr-tools")
    store.create_project(north_star="g", definition_of_done="d", target="new", repo_path=None)
    t = _tasks(store)
    p = _review_pr_prompt(t, _PR, "diff\n+x\n", project_context="", scope_task=t,
                          repo_read=True)
    assert "Do NOT emit a tool_plan" in p


def test_delivery_reviewer_with_mount_reads_truncated_file(tmp_errorta_home: Path) -> None:
    store = LedgerStore("spec32-delivery")
    store.create_project(north_star="a game", definition_of_done="renders + tested",
                         target="new", repo_path=None)
    p = _delivery_review_prompt(store, "head-1", _truncated(), repo_read=True)
    assert "OPEN the affected files" in p
    assert "do NOT reject because a file is truncated" in p
