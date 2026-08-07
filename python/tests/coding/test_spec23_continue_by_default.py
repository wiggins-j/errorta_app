"""SPEC-23 — continue-by-default (the last-word turn).

The thesis: an autonomous run should end for exactly two reasons — the work is
DONE, or a human/budget said STOP. Every other condition is a detector's opinion
that the run is stuck, and the component best placed to choose a different
strategy (the PM) was never asked. Two 2026-07-26 runs died that way, one with
6/6 PRs merged and one with 2 PRs open.

Locked here:

* the HARD / HEURISTIC / terminal taxonomy, as an EXACT partition of the module's
  stop-reason constants (a new reason added without a class fails this file);
* the three outcomes of a last word — actionable / confirming / unheard — and the
  rule that an UNHEARD PM is never read as a consenting one (the Spec 21 lesson);
* the reset map: exactly one counter moves, and the others are asserted untouched
  (a reset-map regression is silent otherwise);
* all three bounds, including the WORST-CASE COST as arithmetic, because the
  constraint governing this whole batch is "do not reintroduce the forever-loop";
* both loop chains — behaviourally AND as a static grep, since a behavioural test
  can pass on one chain by accident of scheduling and a grep cannot;
* the regression locks: hard stops never intervene, `last_word_limit=0` restores
  today's trace, and no stop-reason string moves.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from errorta_council.coding import autonomy
from errorta_council.coding.autonomy import (
    BUDGET_EXHAUSTED,
    CANCELLED,
    DEFINITION_OF_DONE,
    DELIVERY_REVIEW_STALLED,
    DISPATCH_WEDGED,
    ENGINE_FAULT_UNPRODUCTIVE,
    GATE_NOT_IMPROVING,
    HARD_STOP_REASONS,
    HEURISTIC_STOP_REASONS,
    NO_ACTIONABLE_WORK,
    NO_PROGRESS,
    NOT_CONVERGING,
    PLANNING_CHURN,
    QUARANTINED_TASK_NEEDS_INPUT,
    REVISE_LIVELOCK,
    TERMINAL_STOP_REASONS,
    WORKER_UNPRODUCTIVE,
    CodingAutonomyPolicy,
    DetectorEvidence,
    LoopCounters,
    LoopResult,
    TurnOutcome,
    _apply_outcome,
    _intervene,
    _stop_with_evidence,
    counters_from_run_state,
    run_coding_loop,
    window_counters_to_dict,
)
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.topology import DEV, PM, REVIEWER, LastWord, Plan

MEMBERS = [("m-pm", PM), ("m-dev", DEV), ("m-rev", REVIEWER)]
NO_PM = [("m-dev", DEV)]


def _store(tmp_path: Path, name: str = "spec23") -> LedgerStore:
    s = LedgerStore(name, root=tmp_path)
    s.create_project(
        north_star="n", definition_of_done="d", target="new", repo_path=None)
    return s


def _stop(reason: str, c: LoopCounters, **detail) -> LoopResult:
    return _stop_with_evidence(
        reason, c, DetectorEvidence(detector=reason, text=f"{reason} tripped",
                                    value=9, threshold=8), **detail)


class _Recorder:
    """A stub ``run_turn`` that records the actions it was handed and replies with
    a scripted last-word verdict."""

    def __init__(self, verdict=None, kind: str = "planned") -> None:
        self.actions: list = []
        self.verdict = verdict
        self.kind = kind

    def __call__(self, action, ledger) -> TurnOutcome:
        self.actions.append(action)
        return TurnOutcome(kind=self.kind, made_progress=True,
                           last_word=self.verdict)

    @property
    def last_words(self) -> list:
        return [a for a in self.actions if isinstance(a, LastWord)]


def _accepted(rationale: str = "try a different route", ids=("t-new",)):
    return {"outcome": "accepted", "rationale": rationale, "task_ids": list(ids)}


def _decisions(store: LedgerStore, choice: str) -> list:
    return [d for d in store.list_decisions() if d.get("choice") == choice]


# --------------------------------------------------------------------------- #
# Item 1 — the taxonomy.
# --------------------------------------------------------------------------- #

def _module_stop_reasons() -> set[str]:
    """Every stop-reason constant declared in ``autonomy.py``, read from SOURCE —
    the same technique the CLI's own partition lock uses, so a constant added
    without a class fails here instead of silently defaulting to 'terminate'."""
    text = (Path(autonomy.__file__)).read_text("utf-8")
    start = text.index("# --- stop reasons")
    block = text[start:]
    block = block[: block.index("\n# ---", 1)]
    return set(re.findall(r'^[A-Z][A-Z0-9_]*\s*=\s*"([a-z_]+)"', block, re.MULTILINE))


def test_every_stop_reason_has_exactly_one_class() -> None:
    reasons = _module_stop_reasons()
    assert len(reasons) >= 16, reasons
    classes = (HARD_STOP_REASONS, HEURISTIC_STOP_REASONS, TERMINAL_STOP_REASONS)
    for i, a in enumerate(classes):
        for b in classes[i + 1:]:
            assert not (a & b), f"stop reason in two classes: {a & b}"
    union = HARD_STOP_REASONS | HEURISTIC_STOP_REASONS | TERMINAL_STOP_REASONS
    assert union - reasons == set(), f"classed a reason that does not exist: {union - reasons}"
    assert reasons - union == set(), f"unclassed stop reasons: {reasons - union}"


def test_the_taxonomy_matches_the_spec_table() -> None:
    """The spec's table IS the spec — pin it rather than only its shape."""
    assert HARD_STOP_REASONS == {
        "budget_exhausted", "cancelled", "checkpoint", "hard_blocker",
        "member_unhealthy"}
    assert HEURISTIC_STOP_REASONS == {
        "no_progress", "not_converging", "gate_not_improving", "planning_churn",
        "dispatch_wedged", "revise_livelock", "delivery_review_stalled",
        "worker_unproductive", "completion_blocked"}
    assert TERMINAL_STOP_REASONS == {
        DEFINITION_OF_DONE, NO_ACTIONABLE_WORK, QUARANTINED_TASK_NEEDS_INPUT,
    }


def test_completion_blocked_is_heuristic_but_never_intervened() -> None:
    """Item 4: F128 ALREADY re-prompts the PM with the open item set between
    claims. A second last word would ask the same party the same question, so the
    CLASS is recorded and the hook is deliberately not wired."""
    assert "completion_blocked" in HEURISTIC_STOP_REASONS
    assert "completion_blocked" not in autonomy._INTERVENABLE_STOP_REASONS


def test_no_stop_reason_string_changed(tmp_path: Path) -> None:
    """Batch regression lock 1 — the CLI's fail-closed allowlist needs zero edits."""
    from errorta_cli import runstream

    reasons = _module_stop_reasons()
    assert reasons - (runstream.FAILURE_STOP_REASONS | runstream.SUCCESS_STOP_REASONS) == set()


# --------------------------------------------------------------------------- #
# Item 5 / regression lock 3 — HARD stops never intervene.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("reason", sorted(HARD_STOP_REASONS))
def test_hard_stops_return_unchanged_without_a_model_call(
        tmp_path: Path, reason: str) -> None:
    store, c = _store(tmp_path), LoopCounters()
    turn = _Recorder()
    stop = LoopResult(reason, c)
    assert _intervene(store, MEMBERS, CodingAutonomyPolicy(), c, stop,
                      run_turn=turn) is stop
    assert turn.actions == []
    assert c.last_words == 0


def test_engine_fault_worker_unproductive_is_hard(tmp_path: Path) -> None:
    """Item 1's Δ note: `_handle_unproductive`'s except arm is an ENGINE fault, not
    a run condition. Same stop reason (no new reason), but never intervened."""
    store, c = _store(tmp_path), LoopCounters()
    turn = _Recorder()
    stop = autonomy._unproductive_result(
        ENGINE_FAULT_UNPRODUCTIVE, c, LastWord("m-pm", "x"))
    assert stop.stop_reason == WORKER_UNPRODUCTIVE
    assert stop.detail["engine_fault"] is True
    assert _intervene(store, MEMBERS, CodingAutonomyPolicy(), c, stop,
                      run_turn=turn) is stop
    assert turn.actions == []


def test_ladder_exhausted_worker_unproductive_is_heuristic(tmp_path: Path) -> None:
    store, c = _store(tmp_path), LoopCounters()
    turn = _Recorder(_accepted())
    stop = autonomy._unproductive_result(
        WORKER_UNPRODUCTIVE, c, LastWord("m-pm", "x"))
    assert "engine_fault" not in stop.detail
    assert _intervene(store, MEMBERS, CodingAutonomyPolicy(), c, stop,
                      run_turn=turn) is None
    assert len(turn.last_words) == 1


def test_a_cancel_never_waits_on_an_intervention(tmp_path: Path) -> None:
    store, c = _store(tmp_path), LoopCounters()
    turn = _Recorder(_accepted())
    stop = _stop(NO_PROGRESS, c)
    assert _intervene(store, MEMBERS, CodingAutonomyPolicy(), c, stop,
                      run_turn=turn, should_cancel=lambda: True) is stop
    assert turn.actions == []


def test_no_pm_seated_means_no_intervention(tmp_path: Path) -> None:
    store, c = _store(tmp_path), LoopCounters()
    turn = _Recorder(_accepted())
    stop = _stop(NO_PROGRESS, c)
    assert _intervene(store, NO_PM, CodingAutonomyPolicy(), c, stop,
                      run_turn=turn) is stop
    assert turn.actions == []


def test_budget_exhaustion_racing_an_intervention_lands_the_stop(
        tmp_path: Path) -> None:
    store = _store(tmp_path)
    c = LoopCounters(iterations=200)
    turn = _Recorder(_accepted())
    stop = _stop(NOT_CONVERGING, c)
    assert _intervene(store, MEMBERS, CodingAutonomyPolicy(max_iterations=200), c,
                      stop, run_turn=turn) is stop
    assert turn.actions == []


# --------------------------------------------------------------------------- #
# Item 2 — the three outcomes.
# --------------------------------------------------------------------------- #

def test_actionable_response_continues_the_run_and_resets_one_window(
        tmp_path: Path) -> None:
    store = _store(tmp_path)
    c = LoopCounters(iterations=30, pm_idle=2, plan_streak=4, wedge_streak=5,
                     delivery_review_rounds=2, last_gate_iter=1,
                     last_progress_iter=3, last_broken_iter=4)
    turn = _Recorder(_accepted())
    assert _intervene(store, MEMBERS, CodingAutonomyPolicy(), c,
                      _stop(GATE_NOT_IMPROVING, c), run_turn=turn) is None
    action = turn.last_words[0]
    assert isinstance(action, LastWord)
    assert action.member_id == "m-pm" and action.detector == GATE_NOT_IMPROVING
    assert "gate_not_improving tripped" in action.evidence
    # EXACTLY the mapped counter moved; every other window is untouched.
    assert c.last_gate_iter == 31   # anchored at the iteration the turn cost
    assert (c.pm_idle, c.plan_streak, c.wedge_streak, c.delivery_review_rounds) == (
        2, 4, 5, 2)
    assert (c.last_progress_iter, c.last_broken_iter) == (3, 4)
    assert _decisions(store, "last_word_requested")
    assert _decisions(store, "last_word_accepted")


@pytest.mark.parametrize("detector,field,expected", [
    (NO_PROGRESS, "pm_idle", 0),
    (NOT_CONVERGING, "last_progress_iter", 31),
    (GATE_NOT_IMPROVING, "last_gate_iter", 31),
    (PLANNING_CHURN, "plan_streak", 0),
    (DISPATCH_WEDGED, "wedge_streak", 0),
    (REVISE_LIVELOCK, "last_broken_iter", 31),
    (DELIVERY_REVIEW_STALLED, "delivery_review_rounds", 0),
])
def test_the_reset_map_is_the_spec_table(
        tmp_path: Path, detector: str, field: str, expected: int) -> None:
    store = _store(tmp_path)
    c = LoopCounters(iterations=30, pm_idle=9, plan_streak=9, wedge_streak=9,
                     delivery_review_rounds=9, last_gate_iter=0,
                     last_progress_iter=0, last_broken_iter=0, last_gate_best=7)
    assert _intervene(store, MEMBERS, CodingAutonomyPolicy(), c, _stop(detector, c),
                      run_turn=_Recorder(_accepted())) is None
    assert getattr(c, field) == expected
    # The best gate score is a FACT, not a window — it is never cleared.
    assert c.last_gate_best == 7


def test_worker_unproductive_resets_nothing(tmp_path: Path) -> None:
    """The F127 ladder already zeroed its own counter; the PM's replacements are
    new tasks with fresh budgets."""
    store = _store(tmp_path)
    c = LoopCounters(iterations=30, pm_idle=9, plan_streak=9)
    before = (c.pm_idle, c.plan_streak, c.wedge_streak, c.last_gate_iter)
    assert _intervene(store, MEMBERS, CodingAutonomyPolicy(), c,
                      LoopResult(WORKER_UNPRODUCTIVE, c), run_turn=_Recorder(
                          _accepted())) is None
    assert (c.pm_idle, c.plan_streak, c.wedge_streak, c.last_gate_iter) == before


def test_abstention_stops_with_the_original_reason(tmp_path: Path) -> None:
    store, c = _store(tmp_path), LoopCounters()
    turn = _Recorder({"outcome": "confirmed", "rationale": "nothing left to try"})
    out = _intervene(store, MEMBERS, CodingAutonomyPolicy(), c,
                     _stop(PLANNING_CHURN, c), run_turn=turn)
    assert out is not None
    assert out.stop_reason == PLANNING_CHURN          # byte-identical to today
    assert out.detail["last_word"] == {
        "detector": PLANNING_CHURN, "outcome": "confirmed",
        "pm_rationale": "nothing left to try"}
    assert c.plan_streak == 0  # untouched (it was never set)
    confirmed = _decisions(store, "last_word_confirmed")
    assert confirmed and "nothing left to try" in confirmed[0]["rationale"]


def test_an_unparsed_turn_is_never_read_as_a_confirmation(tmp_path: Path) -> None:
    """THE SPEC 21 LESSON. Three of four PM retries in the 2026-07-26 run died on a
    schema rejection while the model kept re-emitting a reasonable shape, and the
    harness read repeated rejection as PM idleness. An unheard PM is not a
    consenting one — and a synthetic turn the harness initiated must not
    accelerate a different detector either."""
    store = _store(tmp_path)
    c = LoopCounters(pm_idle=1, plan_streak=3, schema_rejects=1)
    c.unproductive_counts[("m-dev", "t-1")] = 1

    def boom(action, ledger):
        raise RuntimeError("the model emitted prose")

    out = _intervene(store, MEMBERS, CodingAutonomyPolicy(), c,
                     _stop(NOT_CONVERGING, c), run_turn=boom)
    assert out is not None and out.stop_reason == NOT_CONVERGING
    assert out.detail["last_word"]["outcome"] == "unparsed"
    assert _decisions(store, "last_word_unparsed")
    assert not _decisions(store, "last_word_confirmed")
    # None of the other detectors were fed by the intervention.
    assert (c.pm_idle, c.plan_streak, c.schema_rejects) == (1, 3, 1)
    assert c.unproductive_counts == {("m-dev", "t-1"): 1}


def test_a_verdictless_turn_is_unheard_not_agreement(tmp_path: Path) -> None:
    """A turn that came back with no classification at all (an old/foreign stub)
    must fail toward 'not heard', never toward 'the PM agreed'."""
    store, c = _store(tmp_path), LoopCounters()
    out = _intervene(store, MEMBERS, CodingAutonomyPolicy(), c,
                     _stop(DISPATCH_WEDGED, c), run_turn=_Recorder(None))
    assert out is not None and out.detail["last_word"]["outcome"] == "unparsed"


def test_a_done_claim_routes_through_the_normal_completion_path(
        tmp_path: Path) -> None:
    """The last word gets no special authority to declare victory — a `done`
    answer continues the loop, and `decide_next` lands `definition_of_done` only
    if the ordinary completion machinery accepts it."""
    store = _store(tmp_path)
    c = LoopCounters()
    turn = _Recorder({"outcome": "done", "rationale": "everything shipped"},
                     kind="project_done")
    from errorta_council.coding.topology import CodingReconciler

    assert _intervene(store, MEMBERS, CodingAutonomyPolicy(), c,
                      _stop(GATE_NOT_IMPROVING, c), run_turn=turn,
                      rec=CodingReconciler(store)) is None
    assert store.get_project().status == "done"
    accepted = _decisions(store, "last_word_accepted")
    assert accepted and accepted[0]["claimed_done"] is True


# --------------------------------------------------------------------------- #
# Item 3 — boundedness. The batch exists because of a livelock; it must not add one.
# --------------------------------------------------------------------------- #

def test_last_word_limit_zero_restores_todays_trace(tmp_path: Path) -> None:
    store, c = _store(tmp_path), LoopCounters()
    turn = _Recorder(_accepted())
    stop = _stop(NO_PROGRESS, c)
    assert _intervene(store, MEMBERS, CodingAutonomyPolicy(last_word_limit=0), c,
                      stop, run_turn=turn) is stop
    assert turn.actions == []
    assert c.last_words == 0
    assert not store.list_decisions()


def test_the_same_detector_is_refused_a_second_turn_without_a_merge(
        tmp_path: Path) -> None:
    """Assert the CALL COUNT, not just the outcome: 'it stopped' is also true of
    the broken implementation that dispatched a turn and ignored the answer."""
    store, c = _store(tmp_path), LoopCounters()
    turn = _Recorder(_accepted())
    policy = CodingAutonomyPolicy(last_word_limit=5)
    assert _intervene(store, MEMBERS, policy, c, _stop(GATE_NOT_IMPROVING, c),
                      run_turn=turn) is None
    assert len(turn.last_words) == 1
    stop = _stop(GATE_NOT_IMPROVING, c)
    assert _intervene(store, MEMBERS, policy, c, stop, run_turn=turn) is stop
    assert len(turn.last_words) == 1     # ZERO additional model calls
    assert c.last_words == 1


def test_a_merge_re_arms_the_same_detector(tmp_path: Path) -> None:
    """Bound 2 keys on the SAME progress signal `_account_revise_livelock` already
    trusts — any merge anywhere — so it introduces no new notion of progress."""
    store, c = _store(tmp_path), LoopCounters()
    turn = _Recorder(_accepted())
    policy = CodingAutonomyPolicy(last_word_limit=5)
    assert _intervene(store, MEMBERS, policy, c, _stop(NOT_CONVERGING, c),
                      run_turn=turn) is None
    pr = store.record_pr(task_id="t-1", branch="b", head="h",
                         dev_member="m-dev")
    store.update_pr(pr["pr_id"], status="merged")
    assert _intervene(store, MEMBERS, policy, c, _stop(NOT_CONVERGING, c),
                      run_turn=turn) is None
    assert len(turn.last_words) == 2


def test_the_run_budget_caps_distinct_detectors(tmp_path: Path) -> None:
    store, c = _store(tmp_path), LoopCounters()
    turn = _Recorder(_accepted())
    policy = CodingAutonomyPolicy(last_word_limit=2)
    assert _intervene(store, MEMBERS, policy, c, _stop(NO_PROGRESS, c),
                      run_turn=turn) is None
    assert _intervene(store, MEMBERS, policy, c, _stop(PLANNING_CHURN, c),
                      run_turn=turn) is None
    third = _stop(DISPATCH_WEDGED, c)
    assert _intervene(store, MEMBERS, policy, c, third, run_turn=turn) is third
    assert len(turn.last_words) == 2
    assert c.last_words == 2


def test_worst_case_intervention_cost(tmp_path: Path) -> None:
    """THE BOUND, as arithmetic. The batch's governing constraint is "do not
    reintroduce the forever-loop", so the cost is stated in `_intervene`'s header
    comment and asserted here: at most `last_word_limit` extra PM turns per run —
    1 iteration + 1 model call each — against a default `max_iterations` of 200."""
    store = _store(tmp_path)
    c = LoopCounters()
    turn = _Recorder(_accepted())
    policy = CodingAutonomyPolicy(last_word_limit=2)
    for detector in (NO_PROGRESS, PLANNING_CHURN, DISPATCH_WEDGED, NOT_CONVERGING,
                     REVISE_LIVELOCK, GATE_NOT_IMPROVING):
        _intervene(store, MEMBERS, policy, c, _stop(detector, c), run_turn=turn)
    assert len(turn.last_words) == policy.last_word_limit == 2
    assert c.iterations == 2                      # 1 iteration per intervention
    assert c.model_calls == 2                     # 1 model call per intervention
    assert c.iterations <= 0.01 * CodingAutonomyPolicy().max_iterations * 100


def test_a_last_word_turn_is_excluded_from_detector_accounting() -> None:
    """Bound 3 — non-recursion. An intervention can never manufacture the
    condition for another intervention."""
    from errorta_council.coding.topology import CodingReconciler

    c = LoopCounters(pm_idle=1, plan_streak=2)
    rec = object.__new__(CodingReconciler)
    idle = TurnOutcome(kind="planned", made_progress=False)
    _apply_outcome(rec, None, LastWord("m-pm", NO_PROGRESS), idle, c)
    assert (c.pm_idle, c.plan_streak) == (1, 2)
    # ... while the SAME outcome on an ordinary Plan turn still counts.
    _apply_outcome(rec, None, Plan("m-pm"), idle, c)
    assert (c.pm_idle, c.plan_streak) == (2, 3)


def test_the_budget_survives_a_continue(tmp_path: Path) -> None:
    """Without this, N `errorta continue`s buy N*limit interventions and Item 3's
    bound is fiction. Rides the P0.2 window-carry seam."""
    c = LoopCounters(iterations=12, last_words=2)
    persisted = window_counters_to_dict(c)
    assert persisted["last_words"] == 2
    restored = counters_from_run_state({"counters": persisted})
    assert restored is not None and restored.last_words == 2


# --------------------------------------------------------------------------- #
# Item 5 — both chains, or it is dead code.
# --------------------------------------------------------------------------- #

def test_both_loop_chains_contain_an_intervene_call() -> None:
    """The static half of the both-chains lock (the `test_spec12_18_prep` grep
    style). A behavioural test can pass on one chain by accident of scheduling;
    this cannot. Spec 16 shipped the one-chain bug once already."""
    text = Path(autonomy.__file__).read_text("utf-8")
    seq = text.index("def _run_sequential_loop(")
    conc = text.index("def _run_concurrent_loop(")
    sequential_body = text[seq:conc]
    concurrent_body = text[conc:text.index("def _apply_outcome(", conc)]
    # SPEC-27 generalised this seam: both chains now route every detector outcome
    # through `_apply_detector_outcome`, which is the ONLY caller of `_intervene`.
    # The lock is unchanged in substance — a chain missing the apply point is a
    # chain with no intervention hook.
    assert "_apply_detector_outcome(" in sequential_body, \
        "no intervention hook in the sequential loop"
    assert "_apply_detector_outcome(" in concurrent_body, \
        "no intervention hook in the concurrent loop"
    # Both staged-`pending_stop` return points in the concurrent loop are hooked.
    assert concurrent_body.count("_last_word(pending_stop)") == 2, (
        "a staged stop can escape un-intervened: hook BOTH pending_stop returns")


class _StuckPM:
    """A PM that never makes progress, then answers the last word as scripted."""

    def __init__(self, verdict=None) -> None:
        self.verdict = verdict
        self.last_words: list = []
        self.plans = 0

    def __call__(self, action, ledger) -> TurnOutcome:
        if isinstance(action, LastWord):
            self.last_words.append(action)
            if self.verdict and self.verdict["outcome"] == "accepted":
                ledger.add_task(title=f"lw {len(self.last_words)}", role=DEV)
            return TurnOutcome(kind="planned", made_progress=True,
                               last_word=self.verdict)
        self.plans += 1
        return TurnOutcome(kind="planned", made_progress=False)


@pytest.mark.parametrize("workers", [1, 2])
def test_the_same_no_progress_stop_is_intervened_on_both_chains(
        tmp_path: Path, workers: int) -> None:
    """The behavioural half: the SAME scenario driven through the sequential and
    the concurrent loop must produce the same intervention. Real fanned-out runs
    live on the concurrent chain, which is exactly where a one-chain hook is dead
    code."""
    store = _store(tmp_path, f"chain{workers}")
    pm = _StuckPM({"outcome": "confirmed", "rationale": "we are done trying"})
    policy = CodingAutonomyPolicy(max_parallel_workers=workers, pm_idle_limit=2,
                                  max_iterations=25, checkpoint_cadence="off")
    # The parametrization is only meaningful if it actually selects the two
    # chains — assert the dispatch rather than trusting it.
    assert (autonomy.runtime_cap(policy, MEMBERS, store) > 1) is (workers > 1)
    res = run_coding_loop(store, MEMBERS, policy, run_turn=pm)
    assert res.stop_reason == NO_PROGRESS
    assert len(pm.last_words) == 1
    assert pm.last_words[0].detector == NO_PROGRESS
    assert res.counters.last_words == 1
    assert store.get_run_state()["last_words"]["outcome"] == "confirmed"


@pytest.mark.parametrize("workers", [1, 2])
def test_limit_zero_reproduces_todays_trace_on_both_chains(
        tmp_path: Path, workers: int) -> None:
    """Regression lock 2 — the knob at 0 restores today's behaviour exactly: same
    stop reason, same iteration count, and NOT ONE extra model call."""
    traces = {}
    for limit in (0, 2):
        store = _store(tmp_path, f"z{workers}{limit}")
        pm = _StuckPM(None)
        res = run_coding_loop(
            store, MEMBERS,
            CodingAutonomyPolicy(max_parallel_workers=workers, pm_idle_limit=2,
                                 max_iterations=25, checkpoint_cadence="off",
                                 last_word_limit=limit),
            run_turn=pm)
        traces[limit] = (res.stop_reason, res.counters.iterations,
                         res.counters.model_calls, len(pm.last_words),
                         len(_decisions(store, "last_word_requested")),
                         store.get_run_state().get("last_words"))
    off_reason, off_iters, off_calls, *_ = traces[0]
    assert traces[0] == (NO_PROGRESS, off_iters, off_calls, 0, 0, None)
    # The ONLY difference the feature makes is the one turn it spent asking.
    assert traces[2][:5] == (NO_PROGRESS, off_iters + 1, off_calls + 1, 1, 1)


# --------------------------------------------------------------------------- #
# The two runs that opened this spec, as named regressions.
# --------------------------------------------------------------------------- #

def test_replay_run_2_no_progress_with_open_prs_becomes_a_continuation(
        tmp_path: Path) -> None:
    """2026-07-26 run #2 stopped `no_progress` with 2 PRs still open. The specific
    schema bug is Spec 21's; the CLASS is this spec's — the run should have been
    asked, and an actionable answer should keep it alive."""
    store = _store(tmp_path, "run2")
    for i in (1, 2):
        store.record_pr(task_id=f"t-{i}", branch=f"b{i}", head=f"h{i}",
                        dev_member="m-dev")
    pm = _StuckPM(_accepted("prune the duplicates and finish the open PRs"))
    res = run_coding_loop(
        store, MEMBERS,
        CodingAutonomyPolicy(max_parallel_workers=1, pm_idle_limit=2,
                             max_iterations=12, checkpoint_cadence="off"),
        run_turn=pm)
    assert pm.last_words, "the run died on a heuristic stop without asking the PM"
    assert res.stop_reason != NO_PROGRESS or res.counters.last_words > 0
    assert _decisions(store, "last_word_accepted")
    assert [t for t in store.list_tasks() if t.title.startswith("lw ")]


def test_replay_run_1_gate_not_improving_is_asked_before_it_stops(
        tmp_path: Path) -> None:
    """2026-07-26 run #1 stopped `gate_not_improving` at iteration 22 with 6/6 PRs
    merged — the healthiest trace this project has produced — on a detector whose
    own evidence was wrong. It is asked now, and its answer is on the record."""
    store, c = _store(tmp_path, "run1"), LoopCounters(iterations=22)
    pm = _Recorder(_accepted("the gate is green; ship the remaining slice"))
    out = _intervene(store, MEMBERS, CodingAutonomyPolicy(), c,
                     _stop(GATE_NOT_IMPROVING, c), run_turn=pm)
    assert out is None, "a healthy run was discarded without asking anybody"
    assert c.last_gate_iter == c.iterations == 23
    requested = _decisions(store, "last_word_requested")
    assert requested and requested[0]["detector"] == GATE_NOT_IMPROVING


# --------------------------------------------------------------------------- #
# Item 2 — the runner half: how a real PM answer is CLASSIFIED.
# --------------------------------------------------------------------------- #

_RUNNER_MEMBERS = [
    {"id": "m-pm", "enabled": True, "metadata": {"coding_role": "pm"}},
    {"id": "m-dev", "enabled": True, "metadata": {"coding_role": "dev"}},
]


def _plan_envelope(tasks=(), decisions=(), done=False, summary="") -> str:
    import json

    intent = {"kind": "plan", "done": done,
              "tasks": [{"title": t, "role": "dev", "detail": f"Acceptance: {t}."}
                        for t in tasks],
              "decisions": [{"title": t, "rationale": r} for t, r in decisions]}
    if done:
        intent["completion_summary"] = summary
    return json.dumps({"schema_version": "coding_turn.v1", "role": "pm",
                       "intent": intent})


def _last_word_turn(store: LedgerStore, response: str, *,
                    detector: str = GATE_NOT_IMPROVING,
                    evidence: str = "the gate has been red for 8 iterations"):
    from errorta_council.coding.runner import build_run_turn, members_by_coding_role

    prompts: list[str] = []

    def caller(member, prompt):
        prompts.append(prompt)
        return response

    rt = build_run_turn(store, None, members_by_coding_role(_RUNNER_MEMBERS),
                        caller, guardrail_enabled=False)
    outcome = rt(LastWord(member_id="m-pm", detector=detector,
                          evidence=evidence), store)
    return outcome, prompts


def test_the_prompt_carries_the_detector_its_evidence_and_the_binary_demand(
        tmp_path: Path) -> None:
    store = _store(tmp_path, "lwprompt")
    _outcome, prompts = _last_word_turn(store, _plan_envelope(tasks=["do x"]))
    assert prompts, "the PM was never called"
    prompt = prompts[0]
    assert GATE_NOT_IMPROVING in prompt
    assert "the gate has been red for 8 iterations" in prompt
    assert "PROPOSE A CONCRETE NEXT ACTION" in prompt
    assert "CONFIRM THE HALT" in prompt
    assert "coding_turn.v1" in prompt


def test_a_materialized_task_is_actionable(tmp_path: Path) -> None:
    store = _store(tmp_path, "lwok")
    outcome, _p = _last_word_turn(
        store, _plan_envelope(tasks=["add a smoke test for the renderer"]))
    assert outcome.last_word["outcome"] == "accepted"
    assert outcome.last_word["task_ids"]
    assert [t for t in store.list_tasks()
            if t.title == "add a smoke test for the renderer"]


def test_a_duplicate_proposal_reads_as_an_abstention(tmp_path: Path) -> None:
    """Δ note: the reset condition must be something the ENGINE can act on, or the
    intervention is a licence to loop. Handled with NO new code — the Spec 08
    dedupe gate rejects the duplicate, zero rows materialize, and the turn reads
    as an abstention automatically."""
    store = _store(tmp_path, "lwdup")
    store.add_task(title="add a smoke test for the renderer", role=DEV,
                   detail="already queued")
    outcome, _p = _last_word_turn(
        store, _plan_envelope(tasks=["add a smoke test for the renderer"]))
    assert outcome.last_word["outcome"] == "confirmed"
    assert not outcome.last_word.get("task_ids")


def test_a_decisions_only_answer_is_an_abstention_with_its_reason_kept(
        tmp_path: Path) -> None:
    """Deliberately STRICTER than Spec 21's rule: a decisions-only turn is legal
    for a routine PM turn, but not sufficient to reset a detector window."""
    store = _store(tmp_path, "lwdec")
    outcome, _p = _last_word_turn(store, _plan_envelope(
        decisions=[("stop here", "the remaining work needs a human")]))
    assert outcome.last_word["outcome"] == "confirmed"
    assert "needs a human" in outcome.last_word["rationale"]
    assert _decisions(store, "pm_decision")


def test_an_unparsable_answer_is_classified_unheard(tmp_path: Path) -> None:
    store = _store(tmp_path, "lwbad")
    outcome, prompts = _last_word_turn(store, "I think we should keep going!")
    assert outcome.last_word["outcome"] == "unparsed"
    assert len(prompts) == 2, "the existing single repair retry must still apply"
    assert _decisions(store, "pm_turn_rejected")


def test_a_blocked_answer_is_an_honest_abstention(tmp_path: Path) -> None:
    """Spec 25's typed blocked turn. The PM has no proposal, so the halt stands —
    but with its reason on the record instead of nothing."""
    import json

    store = _store(tmp_path, "lwblocked")
    outcome, _p = _last_word_turn(store, json.dumps({
        "schema_version": "coding_turn.v1", "role": "pm",
        "intent": {"kind": "blocked", "reason": "missing_capability",
                   "detail": "no role can run the acceptance suite"}}))
    assert outcome.last_word["outcome"] == "confirmed"
    assert "no role can run the acceptance suite" in outcome.last_word["rationale"]


def test_a_done_claim_is_judged_by_the_ordinary_completion_gate(
        tmp_path: Path) -> None:
    """No special authority to declare victory: with open work the claim is
    refused exactly as any other done claim is, and reads as an abstention."""
    store = _store(tmp_path, "lwdone")
    store.add_task(title="unfinished work", role=DEV, detail="still open")
    outcome, _p = _last_word_turn(
        store, _plan_envelope(done=True, summary="all shipped"))
    assert outcome.last_word["outcome"] == "confirmed"
    assert "open work remains" in outcome.last_word["rationale"]
    assert store.get_project().status != "done"

    clean = _store(tmp_path, "lwdone2")
    outcome2, _p2 = _last_word_turn(
        clean, _plan_envelope(done=True, summary="all shipped"))
    assert outcome2.last_word["outcome"] == "done"
    assert outcome2.kind == "project_done"


# --------------------------------------------------------------------------- #
# Item 6 — observability.
# --------------------------------------------------------------------------- #

def test_every_intervention_leaves_exactly_two_decisions(tmp_path: Path) -> None:
    store, c = _store(tmp_path), LoopCounters()
    _intervene(store, MEMBERS, CodingAutonomyPolicy(), c, _stop(NO_PROGRESS, c),
               run_turn=_Recorder(_accepted()))
    lw = [d for d in store.list_decisions()
          if str(d.get("choice", "")).startswith("last_word_")]
    assert [d["choice"] for d in lw] == ["last_word_requested", "last_word_accepted"]


def test_the_stop_summary_distinguishes_agreement_from_silence() -> None:
    """An operator MUST be able to tell "the PM agreed" from "we could not hear
    the PM" — reading the second as the first is the mistake this spec exists to
    stop repeating."""
    from errorta_cli import runstream

    def payload(**lw):
        return {"running": False,
                "state": {"status": "stopped", "stop_reason": NO_PROGRESS,
                          "last_words": lw}}

    confirmed = runstream.last_word_note(
        payload(detector=NO_PROGRESS, outcome="confirmed", rationale="nothing left"))
    unheard = runstream.last_word_note(
        payload(detector=NO_PROGRESS, outcome="unparsed", rationale="bad_json"))
    assert "confirmed the halt" in confirmed
    assert "could not be read" in unheard and "NOT confirmed" in unheard
    assert confirmed != unheard
    # No intervention -> no extra line, so today's output is byte-identical.
    assert runstream.last_word_note(
        {"running": False, "state": {"status": "stopped",
                                     "stop_reason": NO_PROGRESS}}) == ""


def test_the_stop_gloss_and_exit_codes_are_untouched() -> None:
    """Regression lock 1, from the CLI side: a run that exhausts its interventions
    stops and exits exactly as it does today."""
    from errorta_cli import runstream
    from errorta_cli.errors import EXIT_RUN_FAILED

    payload = {"running": False,
               "state": {"status": "stopped", "stop_reason": GATE_NOT_IMPROVING,
                         "last_words": {"detector": GATE_NOT_IMPROVING,
                                        "outcome": "confirmed", "rationale": "r"}}}
    assert runstream.classify_exit(payload) == EXIT_RUN_FAILED
    assert runstream.gloss(GATE_NOT_IMPROVING).startswith("the acceptance gate")
    assert runstream.gloss(BUDGET_EXHAUSTED) == "budget (iterations / model-calls) exhausted"
    assert runstream.gloss(CANCELLED) == "cancelled by request"
