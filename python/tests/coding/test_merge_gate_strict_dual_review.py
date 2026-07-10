"""F100 PR-B — strict governance requires reviewer AND PM to approve a code PR.

off/light keep today's single-reviewer merge gate; strict adds the PM as a second
required reviewer of the PR head. Locks both the pure gate and the wired
``merge_review`` path, plus the head-scoped PM-verdict lookup.
"""
from __future__ import annotations

from pathlib import Path

from errorta_council.coding.diff_review import evaluate_merge_gate
from errorta_council.coding.evidence import (
    _pm_reviewed_approved_for_head,
    gather_merge_evidence,
    merge_review,
)
from errorta_council.coding.governance import GovernanceStore
from errorta_council.coding.ledger import LedgerStore, _atomic_write_json
from errorta_council.coding.workspace import CodingWorkspace

# --- pure gate -------------------------------------------------------------

_CLEAR = dict(
    tasks=[{"state": "done"}],
    reviewed_approved=True,
    tests_passed=True,
    conflicts=[],
    definition_of_done_met=True,
    preview_ok=True,
)


def test_gate_ignores_pm_review_when_not_required() -> None:
    # off/light: require_pm_review=False -> PM verdict (even None) is ignored.
    gate = evaluate_merge_gate(**_CLEAR, require_pm_review=False, pm_reviewed_approved=None)
    assert gate.allowed is True and gate.blockers == []


def test_gate_blocks_when_pm_review_required_but_missing() -> None:
    gate = evaluate_merge_gate(**_CLEAR, require_pm_review=True, pm_reviewed_approved=None)
    codes = {b.code for b in gate.blockers}
    assert gate.allowed is False and "pm_unreviewed_changes" in codes


def test_gate_blocks_when_pm_rejected() -> None:
    gate = evaluate_merge_gate(**_CLEAR, require_pm_review=True, pm_reviewed_approved=False)
    codes = {b.code for b in gate.blockers}
    assert gate.allowed is False and "pm_review_rejected" in codes


def test_gate_allows_when_pm_approved() -> None:
    gate = evaluate_merge_gate(**_CLEAR, require_pm_review=True, pm_reviewed_approved=True)
    assert gate.allowed is True and gate.blockers == []


# --- wired merge_review across modes ---------------------------------------

def _ready_project(tmp_path: Path, pid: str) -> tuple[LedgerStore, CodingWorkspace, str]:
    """A project whose single task is done + reviewer-approved + tested + DoD met.
    Returns (store, workspace, head). Without a PM review it clears in off/light
    but not in strict."""
    s = LedgerStore(pid, root=tmp_path)
    s.create_project(north_star="n", definition_of_done="d", target="new", repo_path=None)
    ws = CodingWorkspace(pid, s)
    ws.setup(target="new", repo_path=None)
    t = s.add_task(title="impl", role="dev")
    ws.write_file("a.py", "x = 1\n", task_id=t.task_id)
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
    _atomic_write_json(s._project_path, raw)
    return s, ws, head


def test_off_and_light_do_not_require_pm_review(tmp_path: Path) -> None:
    for mode in ("off", "light"):
        s, ws, _ = _ready_project(tmp_path, f"m-{mode}")
        GovernanceStore.for_ledger(s).update_state(mode=mode)
        gate = merge_review(s, ws)["_gate"]
        assert gate.allowed is True, f"{mode} should not require a PM review"
        assert gather_merge_evidence(s, ws)["require_pm_review"] is False


def test_strict_blocks_without_pm_then_clears_with_pm_approval(tmp_path: Path) -> None:
    s, ws, head = _ready_project(tmp_path, "strict-ok")
    GovernanceStore.for_ledger(s).update_state(mode="strict")

    blocked = merge_review(s, ws)["_gate"]
    assert blocked.allowed is False
    assert "pm_unreviewed_changes" in {b.code for b in blocked.blockers}
    assert gather_merge_evidence(s, ws)["require_pm_review"] is True

    s.record_decision(title="pm", context="c", choice="pm_review_approved",
                      rationale="ok", extra={"reviewed_head": head})
    cleared = merge_review(s, ws)["_gate"]
    assert cleared.allowed is True and cleared.blockers == []


def test_strict_pm_rejection_blocks(tmp_path: Path) -> None:
    s, ws, head = _ready_project(tmp_path, "strict-rej")
    GovernanceStore.for_ledger(s).update_state(mode="strict")
    s.record_decision(title="pm", context="c", choice="pm_review_rejected",
                      rationale="nope", extra={"reviewed_head": head})
    gate = merge_review(s, ws)["_gate"]
    assert gate.allowed is False
    assert "pm_review_rejected" in {b.code for b in gate.blockers}


def test_pm_verdict_is_head_scoped(tmp_path: Path) -> None:
    s, ws, head = _ready_project(tmp_path, "strict-stale")
    GovernanceStore.for_ledger(s).update_state(mode="strict")
    # a PM approval bound to a DIFFERENT (earlier) head does not count for `head`.
    s.record_decision(title="pm", context="c", choice="pm_review_approved",
                      rationale="ok", extra={"reviewed_head": "deadbeef"})
    assert _pm_reviewed_approved_for_head(s, head) is None
    assert merge_review(s, ws)["_gate"].allowed is False
    # binding the approval to the real head clears it.
    s.record_decision(title="pm", context="c", choice="pm_review_approved",
                      rationale="ok", extra={"reviewed_head": head})
    assert _pm_reviewed_approved_for_head(s, head) is True
