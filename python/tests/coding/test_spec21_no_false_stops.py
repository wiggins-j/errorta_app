"""Spec 21 — the loop must not stop a HEALTHY run.

Two live gravity-golf runs died with real work in flight and nothing wrong:

* run 1 stopped ``gate_not_improving`` at iteration 22 with 6/6 PRs merged, zero
  revises and zero context requests — because the only gate signal was a runtime
  probe that PASSED. The score is "how many commands pass", so a green gate sits
  at its maximum and can never strictly increase; the detector read that as churn.
* run 2 stopped ``no_progress`` while two PRs were open — because the PM tried
  four times to say "drop these duplicate HUD tasks, add nothing, not done" and
  the schema refused every attempt (``done=false requires at least one task``,
  and ``missing decisions[0].title`` when it emitted the natural ``decision`` key).
  Each rejected turn counted as no-progress until ``pm_idle_limit`` fired.

Both are unsatisfiable-constraint bugs — the same class Spec 15 removed for tasks
no role could perform. These lock the fixes.
"""
from __future__ import annotations

import pytest

from errorta_council.coding.autonomy import (
    GATE_NOT_IMPROVING,
    CodingAutonomyPolicy,
    LoopCounters,
    _account_gate_stall,
    _gate_has_failure,
)
from errorta_council.coding.schemas import PMPlanIntent

# --------------------------------------------------------------------------- #
# 1. The PM must be able to express a no-new-task turn.
# --------------------------------------------------------------------------- #

def test_not_done_with_only_a_decision_is_legal() -> None:
    """The exact turn the gravity-golf PM was locked out of: prune duplicates,
    add no tasks, not done."""
    intent = PMPlanIntent(kind="plan", done=False, tasks=[], decisions=[
        {"title": "Drop duplicate HUD tasks", "choice": "prune",
         "rationale": "t-b246 and t-b391 duplicate t-ea32"}])
    assert intent.done is False and not intent.tasks and len(intent.decisions) == 1


def test_decision_key_is_accepted_as_the_title() -> None:
    """The model emits {"decision": ...}; `extra=ignore` used to drop it, leaving
    `title` missing and failing the whole turn."""
    intent = PMPlanIntent(kind="plan", done=False, tasks=[], decisions=[
        {"decision": "drop_duplicate_hud_tasks",
         "rationale": "t-ea32, t-b246 and t-b391 are identical"}])
    d = intent.decisions[0]
    assert d.title == "drop_duplicate_hud_tasks"
    assert d.choice == "pm_decision"          # defaulted, not a hard failure


def test_a_genuinely_empty_turn_is_still_refused() -> None:
    """The real stall this rule reached for: no tasks, no decisions, not done."""
    with pytest.raises(ValueError, match="at least one task or decision"):
        PMPlanIntent(kind="plan", done=False, tasks=[], decisions=[])


def test_done_still_requires_a_completion_summary() -> None:
    with pytest.raises(ValueError, match="completion_summary"):
        PMPlanIntent(kind="plan", done=True, tasks=[], completion_summary="  ")


# --------------------------------------------------------------------------- #
# 2. A green gate is not a stalled gate.
# --------------------------------------------------------------------------- #

class _Ledger:
    """Minimal ledger stub exposing only what the detector reads."""
    project_id = "p"

    def __init__(self, runs):
        self._runs = runs

    def list_test_runs(self):
        return self._runs

    def list_delivery_reviews(self):
        return []


def _green():   # what the gravity-golf run actually recorded, twice
    return _Ledger([{"head": "h1", "passed": True,
                     "results": [{"command_id": "runtime:launch", "exit_code": 0}]}])


def _red():
    return _Ledger([{"head": "h1", "passed": False,
                     "results": [{"command_id": "acceptance", "exit_code": 1}]}])


def _drive(ledger, policy, iters):
    """Run the detector across `iters` iterations; return the first stop."""
    c = LoopCounters()
    for i in range(iters):
        c.iterations = i
        out = _account_gate_stall(ledger, c, policy)
        if out is not None:
            return out
    return None


def test_green_gate_never_stops_the_run() -> None:
    """THE regression lock: a passing gate cannot 'improve', and must not stop a
    run that is merging PRs. This is what killed gravity-golf run 1."""
    policy = CodingAutonomyPolicy(gate_stall_limit=8)
    assert _drive(_green(), policy, 40) is None


def test_red_gate_still_stops_the_run() -> None:
    """The pathology the detector exists for is preserved: a gate stuck red while
    the head churns still trips."""
    policy = CodingAutonomyPolicy(gate_stall_limit=8)
    out = _drive(_red(), policy, 40)
    assert out is not None and out.reason == GATE_NOT_IMPROVING


def test_gate_has_failure_reads_each_shape() -> None:
    assert _gate_has_failure(_red()) is True
    assert _gate_has_failure(_green()) is False
    # run-level `passed` with no per-command results
    assert _gate_has_failure(_Ledger([{"passed": False, "results": []}])) is True
    assert _gate_has_failure(_Ledger([{"passed": True, "results": []}])) is False
    # no signal at all, and an unreadable ledger, both report "no failure"
    assert _gate_has_failure(_Ledger([])) is False

    class _Broken:
        project_id = "p"

        def list_test_runs(self):
            raise RuntimeError("ledger unavailable")

    assert _gate_has_failure(_Broken()) is False


def test_zero_limit_still_disables_the_detector() -> None:
    assert _drive(_red(), CodingAutonomyPolicy(gate_stall_limit=0), 40) is None
