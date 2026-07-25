"""Spec 16 — revise-chain circuit breaker.

Phase 1: lineage identity (depth walk + finding_class), unit-tested first.
"""
from __future__ import annotations

from pathlib import Path

from errorta_council.coding import runner
from errorta_council.coding.ledger import LedgerStore


def _store(pid: str, tmp_path: Path) -> LedgerStore:
    s = LedgerStore(pid, root=tmp_path / f"l-{pid}")
    s.create_project(north_star="n", definition_of_done="d", target="new",
                     repo_path=None)
    return s


def _revise(s, *, branch, prev_pr, depth):
    """A revise task whose pr_id back-links the PR it supersedes, + its own PR."""
    t = s.add_task(title=f"revise: {branch}", role="dev",
                   pr_id=prev_pr["pr_id"], revise_depth=depth,
                   finding_class=["stale", "gate"])
    pr = s.record_pr(task_id=t.task_id, branch=branch, head=f"h-{branch}",
                     dev_member="m")
    return t, pr


# --------------------------------------------------------------------------- #
# Phase 1a — the depth walk.
# --------------------------------------------------------------------------- #

def test_depth_over_a_three_deep_lineage(tmp_errorta_home, tmp_path) -> None:
    s = _store("d1", tmp_path)
    t0 = s.add_task(title="impl", role="dev")
    p0 = s.record_pr(task_id=t0.task_id, branch="b0", head="h0", dev_member="m")
    r1, p1 = _revise(s, branch="b1", prev_pr=p0, depth=1)
    r2, p2 = _revise(s, branch="b2", prev_pr=p1, depth=2)
    r3, _p3 = _revise(s, branch="b3", prev_pr=p2, depth=3)

    assert runner._revise_lineage_depth(s, t0) == 0   # original task
    assert runner._revise_lineage_depth(s, r1) == 1
    assert runner._revise_lineage_depth(s, r2) == 2
    assert runner._revise_lineage_depth(s, r3) == 3


def test_independent_pr_starts_at_one(tmp_errorta_home, tmp_path) -> None:
    s = _store("d2", tmp_path)
    t0 = s.add_task(title="impl", role="dev")
    p0 = s.record_pr(task_id=t0.task_id, branch="b0", head="h0", dev_member="m")
    r1, _ = _revise(s, branch="b1", prev_pr=p0, depth=1)
    assert runner._revise_lineage_depth(s, r1) == 1  # not swept into another lineage


def test_cycle_guard_terminates(tmp_errorta_home, tmp_path) -> None:
    # A task whose pr_id points at a PR whose task is itself -> the seen-guard
    # breaks the walk instead of looping forever.
    s = _store("d3", tmp_path)
    t = s.add_task(title="impl", role="dev")
    pr = s.record_pr(task_id=t.task_id, branch="b", head="h", dev_member="m")
    # Re-point the task's pr_id at its own PR (a synthetic self-cycle).
    s.update_task(t.task_id, pr_id=pr["pr_id"])
    t2 = next(x for x in s.list_tasks() if x.task_id == t.task_id)
    assert runner._revise_lineage_depth(s, t2) == 1  # one hop, then guard stops it


def test_blocked_ancestor_stops_the_walk(tmp_errorta_home, tmp_path) -> None:
    s = _store("d4", tmp_path)
    t0 = s.add_task(title="impl", role="dev")
    p0 = s.record_pr(task_id=t0.task_id, branch="b0", head="h0", dev_member="m")
    r1, p1 = _revise(s, branch="b1", prev_pr=p0, depth=1)
    r2, _ = _revise(s, branch="b2", prev_pr=p1, depth=2)
    s.update_pr(p1["pr_id"], status="blocked")   # Phase 2 retires a lineage this way
    assert runner._revise_lineage_depth(s, r2) == 0  # walk stops at the blocked P1


# --------------------------------------------------------------------------- #
# Phase 1b — finding_class normalization.
# --------------------------------------------------------------------------- #

def _fc(*findings):
    return runner._finding_class(list(findings))


def test_finding_class_collapses_case_and_punctuation() -> None:
    a = _fc({"title": "No evidence the tests were RUN!", "blocking": True})
    b = _fc({"title": "no evidence the tests were run", "blocking": True})
    assert a == b


def test_finding_class_separates_genuinely_different_findings() -> None:
    a = _fc({"title": "null deref in Render.init", "body": "canvas is 0x0"})
    b = _fc({"title": "level 3 is solvable in zero strokes", "body": ""})
    assert a != b


def test_finding_class_empty_equals_empty() -> None:
    # A run of contentless rejections SHOULD break the chain -> empty == empty.
    assert _fc() == _fc()
    assert _fc({"title": "", "body": ""}) == _fc()


def test_finding_class_spans_all_findings_not_just_the_first() -> None:
    # Spec 14/15 suppress/flag findings, so the class must derive from all of them.
    one = _fc({"title": "a", "blocking": True})
    two = _fc({"title": "a", "blocking": True}, {"title": "b", "blocking": False})
    assert one != two
