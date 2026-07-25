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


# --------------------------------------------------------------------------- #
# Phase 2 — the breaker.
# --------------------------------------------------------------------------- #

def _three_deep(s):
    """Build a depth-3 revise lineage; r3 carries finding_class {gate, stale}."""
    t0 = s.add_task(title="impl", role="dev")
    p0 = s.record_pr(task_id=t0.task_id, branch="b0", head="h0", dev_member="m")
    _r1, p1 = _revise(s, branch="b1", prev_pr=p0, depth=1)
    _r2, p2 = _revise(s, branch="b2", prev_pr=p1, depth=2)
    r3, p3 = _revise(s, branch="b3", prev_pr=p2, depth=3)
    return r3, p3


def test_three_same_class_rejections_break_the_chain(
        tmp_errorta_home, tmp_path) -> None:
    s = _store("b1", tmp_path)
    r3, p3 = _three_deep(s)
    # Reject the depth-3 PR with the SAME finding class r3 was created to address.
    same = [{"blocking": True, "title": "stale gate", "body": ""}]
    runner._handle_review_rejection(s, None, pr=p3, task=r3,
                                    findings=same, source="reviewer")
    revises = [t for t in s.list_tasks() if t.title.startswith("revise:")]
    assert len(revises) == 3                       # r1,r2,r3 only — NO 4th revise
    assert any(t.role == "pm" and t.title.startswith("revise chain broken")
               for t in s.list_tasks())            # one PM re-plan task
    assert any(d["choice"] == "revise_chain_broken" for d in s.list_decisions())
    assert s.get_pr(p3["pr_id"])["status"] == "blocked"   # PR blocked (terminal)


def test_different_class_at_the_cap_does_not_break(
        tmp_errorta_home, tmp_path) -> None:
    # The real-progress lock: a lineage working through DISTINCT defects is healthy
    # and must not be broken, even at the depth cap.
    s = _store("b2", tmp_path)
    r3, p3 = _three_deep(s)
    diff = [{"blocking": True, "title": "brand new defect in the solver", "body": ""}]
    runner._handle_review_rejection(s, None, pr=p3, task=r3,
                                    findings=diff, source="reviewer")
    revises = [t for t in s.list_tasks() if t.title.startswith("revise:")]
    assert len(revises) == 4                        # a 4th revise DID spawn
    assert not any(d["choice"] == "revise_chain_broken" for d in s.list_decisions())
    assert s.get_pr(p3["pr_id"])["status"] != "blocked"


def test_blocked_pr_from_a_broken_chain_is_never_mergeable(
        tmp_errorta_home, tmp_path) -> None:
    s = _store("b3", tmp_path)
    r3, p3 = _three_deep(s)
    runner._handle_review_rejection(
        s, None, pr=p3, task=r3,
        findings=[{"blocking": True, "title": "stale gate"}], source="reviewer")
    # blocked is in _set_mergeable_if_ready's terminal exclusion set.
    assert s.get_pr(p3["pr_id"])["status"] == "blocked"


def test_pm_review_arm_is_guarded_identically(tmp_errorta_home, tmp_path) -> None:
    # Both the reviewer and strict-mode PM-review arms route through the one seam,
    # so source="pm" breaks the chain exactly the same way.
    s = _store("b4", tmp_path)
    r3, p3 = _three_deep(s)
    runner._handle_review_rejection(
        s, None, pr=p3, task=r3,
        findings=[{"blocking": True, "title": "stale gate"}], source="pm")
    assert s.get_pr(p3["pr_id"])["status"] == "blocked"
    revises = [t for t in s.list_tasks() if t.title.startswith("revise:")]
    assert len(revises) == 3  # no 4th revise on the PM arm either


# --------------------------------------------------------------------------- #
# Phase 3 — the livelock detector.
# --------------------------------------------------------------------------- #

from errorta_council.coding.autonomy import (  # noqa: E402
    REVISE_LIVELOCK,
    CodingAutonomyPolicy,
    LoopCounters,
    _account_revise_livelock,
)


def _broke(s):
    s.record_decision(title="broke", context="pr p", choice="revise_chain_broken",
                      rationale="r")


def test_livelock_stops_after_limit_with_no_recovery(
        tmp_errorta_home, tmp_path) -> None:
    s = _store("lv1", tmp_path)
    _broke(s)
    policy = CodingAutonomyPolicy()               # revise_livelock_limit == 5
    c = LoopCounters()
    result = None
    for i in range(policy.revise_livelock_limit + 3):
        c.iterations = i
        result = _account_revise_livelock(s, c, policy)
        if result is not None:
            break
    assert result is not None and result.stop_reason == REVISE_LIVELOCK


def test_a_merge_resets_the_livelock_window(tmp_errorta_home, tmp_path) -> None:
    s = _store("lv2", tmp_path)
    _broke(s)
    policy = CodingAutonomyPolicy()
    c = LoopCounters()
    for i in range(policy.revise_livelock_limit):      # up to the edge, no stop
        c.iterations = i
        assert _account_revise_livelock(s, c, policy) is None
    # A merge lands somewhere (progress) -> window resets -> still no stop.
    t = s.add_task(title="x", role="dev")
    pr = s.record_pr(task_id=t.task_id, branch="bm", head="hm", dev_member="m")
    s.update_pr(pr["pr_id"], status="merged")
    c.iterations = policy.revise_livelock_limit
    assert _account_revise_livelock(s, c, policy) is None


def test_livelock_zero_disables(tmp_errorta_home, tmp_path) -> None:
    s = _store("lv3", tmp_path)
    _broke(s)
    policy = CodingAutonomyPolicy(revise_livelock_limit=0)
    c = LoopCounters()
    for i in range(20):
        c.iterations = i
        assert _account_revise_livelock(s, c, policy) is None


def test_livelock_detector_wired_into_both_loops() -> None:
    # The dead-code lock: the detector must be called in BOTH the sequential and
    # concurrent loops (Spec 13 lifts the clamp -> real runs go concurrent).
    import inspect

    from errorta_council.coding import autonomy
    src = inspect.getsource(autonomy)
    assert src.count("_account_revise_livelock(ledger, c, policy)") >= 2


# --------------------------------------------------------------------------- #
# Phase 4 — stop-reason contract (four sites).
# --------------------------------------------------------------------------- #

def test_stop_reason_contract_carries_revise_livelock() -> None:
    from errorta_cli import runstream
    from errorta_cli.errors import EXIT_RUN_FAILED
    from errorta_cli.render import status
    assert "revise_livelock" in runstream.FAILURE_STOP_REASONS
    assert "revise_livelock" in runstream.STOP_REASON_GLOSS
    assert "revise_livelock" in status._TERMINAL_BAD
    assert "revise_livelock" not in runstream.SUCCESS_STOP_REASONS
    # classify_exit is a fail-closed allowlist -> a failure reason is EXIT_RUN_FAILED.
    assert runstream.classify_exit(
        {"state": {"stop_reason": "revise_livelock"}}) == EXIT_RUN_FAILED
