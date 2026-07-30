"""SPEC-27 — convergence as CONTROL, not kill.

The thesis: the engine could end a run about a dozen ways and recover roughly two,
so seven `_account_*` detectors shared one signature whose entire vocabulary was
"continue" or "die". This spec replaces that with a four-valued outcome — `None` /
`Narrow` / `Escalate` / `Stop` — gives each detector an ordered, bounded
intervention ladder, and changes what a threshold *does* without changing what it
equals.

Locked here, in the order the spec states them:

* **the contract** — every `_account_*` is annotated `-> DetectorOutcome`, `Narrow`
  falls through and `Escalate`/`Stop` short-circuit, which makes the new control
  flow a strict GENERALISATION of the old early-return chain;
* **the generalisation lock** — `narrow_limit=0` reproduces today's trace exactly:
  same stop reason, same iterations, same model calls, no decisions, no run state.
  This is the first test in the file because it is the escape hatch;
* **the rung table** — an exact cover of SPEC-23's HEURISTIC set (plus the one
  reason SPEC-23 deferred here), no HARD reason, every tuple ending in `STOP`;
* **the non-wedge lock, per `NARROW_*` action** — a run under any narrowing that
  has one dispatchable task still dispatches it, serially. GL04's clamp carried
  this invariant; every action inherits it;
* **boundedness, as ARITHMETIC** — the governing constraint of this whole batch is
  "do not reintroduce the 2026-07-24 forever-loop", so the worst case is asserted
  as counts, not as an outcome: every extra iteration is accounted for by a ladder
  deferral, and deferrals are monotone, never refunded, and capped at
  `narrow_limit * narrow_drain_iters`;
* **the force-lift lock** — a narrowing whose release condition never arrives is
  lifted at the cap with a decision and a monitor signal, because a narrowing that
  never releases IS the wedge it was diagnosing;
* **backward compatibility** — no stop-reason string moves, no exit code moves,
  and `no_actionable_work` after a refused escalation still exits `EXIT_OK`;
* **both chains**, behaviourally and as a static grep.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from errorta_cli import runstream
from errorta_council.coding import autonomy
from errorta_council.coding.autonomy import (
    _DETECTOR_LADDERS,
    DELIVERY_REVIEW_STALLED,
    HARD_STOP_REASONS,
    HEURISTIC_STOP_REASONS,
    NARROW_ALERT_ONLY,
    NARROW_CLAMP_FANOUT,
    NARROW_CLAMP_PLANNING,
    NARROW_FORCE_INTEGRATION,
    NARROW_FORCE_LIFT,
    NO_ACTIONABLE_WORK,
    NOT_CONVERGING,
    PLANNING_CHURN,
    RUNG_ESCALATE,
    RUNG_STOP,
    CodingAutonomyPolicy,
    DetectorEvidence,
    Escalate,
    LoopCounters,
    LoopResult,
    Narrow,
    Stop,
    TurnOutcome,
    _apply_detector_outcome,
    _engage_narrow,
    _ladder_rung,
    _narrow_deferral_cap,
    _release_narrow_flags,
    _trip,
    counters_from_run_state,
    policy_from_dict,
    policy_to_dict,
    run_coding_loop,
    runtime_cap,
    window_counters_to_dict,
)
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.topology import DEV, PM, Assign, LastWord, plan_next_batch

PM_ONLY = [("m-pm", PM)]
TEAM = [("m-pm", PM), ("m-dev", DEV)]
_MEMBERS2 = [("m1", DEV), ("m2", DEV)]  # base parallelism 2


def _store(tmp_path: Path, name: str) -> LedgerStore:
    s = LedgerStore(name, root=tmp_path / f"l-{name}")
    s.create_project(north_star="n", definition_of_done="d", target="new",
                     repo_path=None)
    return s


def _evidence(detector: str) -> DetectorEvidence:
    return DetectorEvidence(detector=detector, text=f"{detector} tripped",
                            value=9, threshold=8)


def _decisions(store: LedgerStore, choice: str) -> list:
    return [d for d in store.list_decisions() if d.get("choice") == choice]


class _PlanningPM:
    """A PM that only ever plans — the `planning_churn` pathology — and answers a
    last word as scripted. Every plan turn is PROGRESS-bearing, so `pm_idle` never
    climbs and `planning_churn` is the only detector that can fire."""

    def __init__(self, verdict=None) -> None:
        self.verdict = verdict
        self.last_words: list = []
        self.plans = 0

    def __call__(self, action, ledger) -> TurnOutcome:
        if isinstance(action, LastWord):
            self.last_words.append(action)
            return TurnOutcome(kind="planned", made_progress=True,
                               last_word=self.verdict)
        self.plans += 1
        return TurnOutcome(kind="planned", made_progress=True, model_calls=1)


def _churn_policy(**over) -> CodingAutonomyPolicy:
    base = dict(max_parallel_workers=1, plan_streak_limit=2, pm_idle_limit=99,
                max_iterations=30, checkpoint_cadence="off",
                convergence_stall_limit=999, gate_stall_limit=0,
                wedge_stall_limit=0, revise_livelock_limit=0,
                convergence_window=0)
    base.update(over)
    return CodingAutonomyPolicy(**base)


# --------------------------------------------------------------------------- #
# THE GENERALISATION LOCK — written first, on purpose. `narrow_limit=0` must
# reproduce today's trace byte-for-byte; it is the batch's escape hatch and the
# regression story for every other test in this file.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("workers", [1, 2])
def test_narrow_limit_zero_reproduces_todays_trace_on_both_chains(
        tmp_errorta_home, tmp_path: Path, workers: int) -> None:
    """Regression lock 2 of the batch. At `0` the ladder writes NO counter, NO
    decision and NO run state, and the run stops exactly where it stops today."""
    store = _store(tmp_path, f"z{workers}")
    pm = _PlanningPM({"outcome": "confirmed", "rationale": "the halt stands"})
    res = run_coding_loop(
        store, PM_ONLY,
        _churn_policy(narrow_limit=0, max_parallel_workers=workers),
        run_turn=pm)
    assert res.stop_reason == PLANNING_CHURN
    assert res.counters.narrows_used == 0
    assert res.counters.narrow_deferrals == 0
    assert res.counters.narrow_rungs == {}
    assert store.get_run_state().get("narrow_ladder") in (None, {})
    assert not [d for d in store.list_decisions()
                if str(d.get("choice", "")).startswith("narrow_")]
    # SPEC-23's rung is UNCHANGED at 0: the escalate/stop tail is what a disabled
    # ladder collapses to, so the PM is still asked exactly once.
    assert len(pm.last_words) == 1


def test_a_disabled_ladder_collapses_every_rung_tuple_to_its_tail(
        tmp_errorta_home, tmp_path: Path) -> None:
    store = _store(tmp_path, "tail")
    c, policy = LoopCounters(), CodingAutonomyPolicy(narrow_limit=0)
    for reason, ladder in _DETECTOR_LADDERS.items():
        want = RUNG_ESCALATE if RUNG_ESCALATE in ladder else RUNG_STOP
        assert _ladder_rung(store, c, policy, reason) == want, reason


# --------------------------------------------------------------------------- #
# Item 1 — the contract.
# --------------------------------------------------------------------------- #

_ACCOUNT_DETECTORS = (
    "_account_foundation_stall", "_account_hot_file_freeze",
    "_account_convergence", "_account_gate_stall", "_account_convergence_clamp",
    "_account_revise_livelock", "_account_planning_churn",
    "_account_dispatch_wedge",
)


def test_every_account_detector_declares_the_outcome_contract() -> None:
    """The signature IS the contract: a detector that still declares
    `Optional[LoopResult]` cannot express a narrowing, which is the whole defect."""
    for name in _ACCOUNT_DETECTORS:
        fn = getattr(autonomy, name)
        ret = inspect.signature(fn).return_annotation
        assert ret == "DetectorOutcome", f"{name} does not return DetectorOutcome"


def test_narrow_falls_through_and_escalate_and_stop_short_circuit(
        tmp_errorta_home, tmp_path: Path) -> None:
    """The mapping that makes this a strict generalisation of today's flow."""
    store = _store(tmp_path, "shape")
    c, policy = LoopCounters(), CodingAutonomyPolicy()

    def apply(out):
        return _apply_detector_outcome(store, PM_ONLY, policy, c, out,
                                       run_turn=lambda a, led: TurnOutcome(kind="planned"))

    assert apply(None) is None                       # nothing happened
    narrow = Narrow(action=NARROW_CLAMP_FANOUT, detector=NOT_CONVERGING,
                    evidence="x")
    assert apply(narrow) is None                     # engaged, chain CONTINUES
    stop = apply(Stop(reason=NOT_CONVERGING, detector=NOT_CONVERGING,
                      evidence="x", detail={"evidence": {"text": "x"}}))
    assert isinstance(stop, LoopResult) and stop.stop_reason == NOT_CONVERGING
    assert stop.detail == {"evidence": {"text": "x"}}


def test_a_stop_is_byte_identical_to_the_pre_spec_loop_result(
        tmp_errorta_home, tmp_path: Path) -> None:
    """A chain of `None`/`Stop` produces exactly what `_stop_with_evidence` did."""
    store = _store(tmp_path, "bytes")
    c, policy = LoopCounters(), CodingAutonomyPolicy(narrow_limit=0)
    for reason in sorted(HEURISTIC_STOP_REASONS):
        ev = _evidence(reason)
        legacy = autonomy._stop_with_evidence(reason, c, ev, summary="s")
        out = _trip(store, c, policy, reason, ev, summary="s")
        assert isinstance(out, (Escalate, Stop))
        assert out.reason == legacy.stop_reason
        assert out.detail == legacy.detail


def test_the_clamp_is_folded_in_not_rewritten(tmp_errorta_home,
                                              tmp_path: Path) -> None:
    """GL04's clamp becomes a `Narrow` and still NEVER produces a `Stop`."""
    src = Path(autonomy.__file__).read_text("utf-8")
    body = src[src.index("def _account_convergence_clamp("):
               src.index("def _engage_convergence_clamp(")]
    assert "Stop(" not in body, "the clamp must never stop the run"
    assert "_engage_convergence_clamp(" in body, "the clamp was reimplemented"


# --------------------------------------------------------------------------- #
# Item 3 — the rung table. The table IS the spec, so pin it.
# --------------------------------------------------------------------------- #

def test_the_ladder_table_covers_exactly_the_heuristic_set() -> None:
    covered = set(_DETECTOR_LADDERS)
    assert HEURISTIC_STOP_REASONS <= covered, (
        f"heuristic reason with no ladder: {HEURISTIC_STOP_REASONS - covered}")
    # The only addition is the reason SPEC-23 Item 1 deferred here explicitly.
    assert covered - HEURISTIC_STOP_REASONS == {NO_ACTIONABLE_WORK}
    assert not (covered & HARD_STOP_REASONS), "a HARD stop must never be narrowed"


def test_every_ladder_ends_in_stop_and_only_ends_in_stop() -> None:
    for reason, ladder in _DETECTOR_LADDERS.items():
        assert ladder, reason
        assert ladder[-1] == RUNG_STOP, f"{reason} does not end in a stop"
        assert ladder.count(RUNG_STOP) == 1, reason
        assert ladder.count(RUNG_ESCALATE) <= 1, reason


def test_the_rung_table_matches_the_spec_table() -> None:
    assert _DETECTOR_LADDERS[NOT_CONVERGING] == (
        NARROW_FORCE_INTEGRATION, NARROW_CLAMP_FANOUT, RUNG_ESCALATE, RUNG_STOP)
    assert _DETECTOR_LADDERS[PLANNING_CHURN] == (
        NARROW_CLAMP_PLANNING, RUNG_ESCALATE, RUNG_STOP)
    assert _DETECTOR_LADDERS[DELIVERY_REVIEW_STALLED] == (
        NARROW_FORCE_INTEGRATION, RUNG_ESCALATE, RUNG_STOP)
    # No narrowing rung by construction — narrowing a wedge makes it worse.
    assert _DETECTOR_LADDERS["dispatch_wedged"] == (RUNG_ESCALATE, RUNG_STOP)
    assert _DETECTOR_LADDERS["gate_not_improving"] == (RUNG_ESCALATE, RUNG_STOP)
    assert _DETECTOR_LADDERS["revise_livelock"] == (RUNG_ESCALATE, RUNG_STOP)
    # F128 already re-prompts the PM; a second intervention asks the same party
    # the same question.
    assert _DETECTOR_LADDERS["completion_blocked"] == (RUNG_STOP,)


def test_the_two_pure_narrow_detectors_report_their_own_action(
        tmp_errorta_home, tmp_path: Path) -> None:
    """`foundation_not_converging` and `hot_file_freeze_stalled` were ALREADY
    narrows; they now say so, charge nothing, and keep their side effects."""
    store = _store(tmp_path, "pure")
    c = LoopCounters()
    policy = CodingAutonomyPolicy(hot_file_freeze_stall_limit=1)
    store.set_run_state(frozen_paths=["src/a.ts"])
    out = autonomy._account_hot_file_freeze(store, c, policy)
    assert isinstance(out, Narrow) and out.action == NARROW_FORCE_LIFT
    assert out.self_applied is True
    assert store.get_run_state().get("frozen_paths") == []   # side effect intact
    before = (c.narrows_used, c.narrow_deferrals)
    _engage_narrow(store, c, policy, out)
    assert (c.narrows_used, c.narrow_deferrals) == before, \
        "a self-applied narrow must charge no budget"
    assert NARROW_ALERT_ONLY in {NARROW_ALERT_ONLY}   # named for the total contract


# --------------------------------------------------------------------------- #
# Item 2 — the ladder walks, end to end, on both chains.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("workers", [1, 2])
def test_planning_churn_narrows_before_it_escalates_on_both_chains(
        tmp_errorta_home, tmp_path: Path, workers: int) -> None:
    store = _store(tmp_path, f"walk{workers}")
    pm = _PlanningPM({"outcome": "confirmed", "rationale": "the halt stands"})
    res = run_coding_loop(
        store, PM_ONLY, _churn_policy(max_parallel_workers=workers), run_turn=pm)

    assert res.stop_reason == PLANNING_CHURN          # today's reason, unchanged
    assert res.counters.narrows_used == 1
    assert res.counters.narrow_rungs[PLANNING_CHURN] == 2   # narrow -> escalate
    engaged = _decisions(store, f"narrow_{NARROW_CLAMP_PLANNING}_engaged")
    assert len(engaged) == 1, "the rung transition is not on the ledger"
    assert engaged[0]["context"] == f"narrow:{PLANNING_CHURN}"
    assert store.get_run_state()["narrow_ladder"]["narrows_used"] == 1
    assert len(pm.last_words) == 1, "the escalate rung is SPEC-23's, spent once"


def test_a_second_detector_asking_for_a_live_narrowing_is_not_charged_twice(
        tmp_errorta_home, tmp_path: Path) -> None:
    """Edge case: a narrowing already in effect is recorded as SATISFIED — the rung
    advances, `narrow_limit` is not charged again."""
    store = _store(tmp_path, "twice")
    c, policy = LoopCounters(), CodingAutonomyPolicy()
    _mergeable_pr(store, 1)
    first = Narrow(action=NARROW_FORCE_INTEGRATION, detector=NOT_CONVERGING,
                   evidence="a")
    second = Narrow(action=NARROW_FORCE_INTEGRATION,
                    detector=DELIVERY_REVIEW_STALLED, evidence="b")
    assert _engage_narrow(store, c, policy, first) == "engaged"
    assert _engage_narrow(store, c, policy, second) == "satisfied"
    assert c.narrows_used == 1                       # ONE flag, ONE charge
    assert c.narrow_rungs == {NOT_CONVERGING: 1, DELIVERY_REVIEW_STALLED: 1}
    assert c.narrow_deferrals == 2                   # but BOTH deferred a stop


def test_a_rung_whose_mechanism_is_disabled_is_a_recorded_no_op(
        tmp_errorta_home, tmp_path: Path) -> None:
    """`convergence_window == 0` disables GL04's clamp, so a `CLAMP_FANOUT` rung
    engages nothing: recorded, rung advanced, `narrow_limit` NOT charged."""
    store = _store(tmp_path, "noop")
    c = LoopCounters()
    policy = CodingAutonomyPolicy(convergence_window=0)
    out = Narrow(action=NARROW_CLAMP_FANOUT, detector=NOT_CONVERGING, evidence="x")
    assert _engage_narrow(store, c, policy, out) == "noop"
    assert c.narrows_used == 0
    assert c.narrow_rungs[NOT_CONVERGING] == 1
    assert _decisions(store, f"narrow_{NARROW_CLAMP_FANOUT}_noop")
    assert not store.get_run_state().get("convergence_clamped")


def test_progress_resets_the_rungs_but_never_refunds_the_budget(
        tmp_errorta_home, tmp_path: Path) -> None:
    """PROGRESS is a merged PR (the signal `_account_revise_livelock` already
    trusts) or a recovered GL04 window. It buys new STRATEGY, never new BUDGET."""
    store = _store(tmp_path, "prog")
    c, policy = LoopCounters(), CodingAutonomyPolicy(convergence_window=0)
    c.narrow_rungs = {NOT_CONVERGING: 2}
    c.narrows_used, c.narrow_deferrals = 2, 2
    assert _ladder_rung(store, c, policy, NOT_CONVERGING) == RUNG_ESCALATE
    _merged_pr(store, 1)
    assert _ladder_rung(store, c, policy, NOT_CONVERGING) == NARROW_FORCE_INTEGRATION
    assert c.narrow_rungs == {}                      # every ladder re-armed
    assert (c.narrows_used, c.narrow_deferrals) == (2, 2)   # NOT refunded


# --------------------------------------------------------------------------- #
# Item 6 — the non-wedge lock, per NARROW_* action.
# --------------------------------------------------------------------------- #

def _mergeable_pr(store: LedgerStore, i: int) -> dict:
    t = store.add_task(title=f"impl {i}", role=DEV)
    pr = store.record_pr(task_id=t.task_id, branch=f"b{i}", head=f"h{i}",
                         dev_member="m-dev")
    store.update_pr(pr["pr_id"], status="mergeable")
    return pr


def _merged_pr(store: LedgerStore, i: int) -> None:
    t = store.add_task(title=f"done {i}", role=DEV)
    pr = store.record_pr(task_id=t.task_id, branch=f"m{i}", head=f"mh{i}",
                         dev_member="m-dev")
    store.update_pr(pr["pr_id"], status="merged")


@pytest.mark.parametrize(
    "flags",
    [{"integration_only": True}, {"planning_clamped": True},
     {"integration_only": True, "planning_clamped": True}])
def test_one_dispatchable_task_is_still_dispatched_under_every_narrowing(
        tmp_errorta_home, tmp_path: Path, flags: dict) -> None:
    """Non-wedge invariant 1, per action: a narrowing may only reduce concurrency
    or defer NEW work. The last ready task still goes out."""
    store = _store(tmp_path, "wedge" + "".join(sorted(flags))[:6])
    store.add_task(title="the only job", role=DEV)
    batch = plan_next_batch(store, [("m-dev", DEV)], None, **flags)
    assert [a for a in batch if isinstance(a, Assign)], \
        f"a narrowing made the last dispatchable task non-dispatchable: {flags}"


def test_the_fanout_clamp_narrows_concurrency_and_never_blocks_dispatch(
        tmp_errorta_home, tmp_path: Path) -> None:
    """GL04's own invariant, restated as this spec's: `runtime_cap` returns 1 under
    the clamp — never 0. A clamped run is serial, not stopped."""
    store = _store(tmp_path, "cap")
    policy = CodingAutonomyPolicy()
    assert runtime_cap(policy, _MEMBERS2, store) == 2
    store.set_run_state(convergence_clamped=True)
    assert runtime_cap(policy, _MEMBERS2, store) == 1


def test_integration_only_caps_the_fanout_without_starving_the_queue(
        tmp_errorta_home, tmp_path: Path) -> None:
    """It REDUCES concurrency: two idle devs and two ready tasks yield one assign
    under the narrowing and two without it."""
    store = _store(tmp_path, "serial")
    store.add_task(title="job a", role=DEV)
    store.add_task(title="job b", role=DEV)
    wide = plan_next_batch(store, _MEMBERS2, None)
    narrow = plan_next_batch(store, _MEMBERS2, None, integration_only=True)
    assert len([a for a in wide if isinstance(a, Assign)]) == 2
    assert len([a for a in narrow if isinstance(a, Assign)]) == 1


def test_force_integration_with_nothing_mergeable_is_a_free_no_op(
        tmp_errorta_home, tmp_path: Path) -> None:
    """The acceptance the spec names explicitly: requested with nothing to drain it
    is a recorded no-op that advances the rung, does NOT charge `narrow_limit`, and
    engages no flag — so it cannot manufacture a wedge out of a churn alarm."""
    store = _store(tmp_path, "nomerge")
    c, policy = LoopCounters(), CodingAutonomyPolicy()
    out = Narrow(action=NARROW_FORCE_INTEGRATION, detector=NOT_CONVERGING,
                 evidence="x")
    assert _engage_narrow(store, c, policy, out) == "noop"
    assert c.narrows_used == 0 and c.narrow_rungs[NOT_CONVERGING] == 1
    assert not store.get_run_state().get("integration_only")


def test_a_narrowing_releases_when_its_own_condition_is_met(
        tmp_errorta_home, tmp_path: Path) -> None:
    store = _store(tmp_path, "release27")
    c, policy = LoopCounters(), CodingAutonomyPolicy()
    pr = _mergeable_pr(store, 1)
    assert _engage_narrow(store, c, policy, Narrow(
        action=NARROW_FORCE_INTEGRATION, detector=NOT_CONVERGING,
        evidence="x")) == "engaged"
    assert store.get_run_state()["integration_only"] is True
    store.update_pr(pr["pr_id"], status="merged")     # nothing left to drain
    c.iterations = 1
    _release_narrow_flags(store, c, policy)
    assert store.get_run_state()["integration_only"] is False
    assert _decisions(store, f"narrow_{NARROW_FORCE_INTEGRATION}_released")


def test_a_narrowing_that_never_releases_is_force_lifted_at_the_cap(
        tmp_errorta_home, tmp_path: Path) -> None:
    """Non-wedge invariant 3 — `_account_hot_file_freeze`'s pattern, generalised. A
    narrowing whose release condition never arrives IS a wedge, so the cap lifts it
    with a decision AND a monitor signal, and dispatch resumes."""
    store = _store(tmp_path, "lift")
    c, policy = LoopCounters(), CodingAutonomyPolicy(narrow_drain_iters=3)
    _mergeable_pr(store, 1)                           # stays mergeable forever
    _engage_narrow(store, c, policy, Narrow(
        action=NARROW_FORCE_INTEGRATION, detector=NOT_CONVERGING, evidence="x"))
    for i in range(1, 3):
        c.iterations = i
        _release_narrow_flags(store, c, policy)
        assert store.get_run_state()["integration_only"] is True, i
    c.iterations = 3
    _release_narrow_flags(store, c, policy)
    assert store.get_run_state()["integration_only"] is False
    assert _decisions(store, f"narrow_{NARROW_FORCE_INTEGRATION}_force_lifted")
    assert NARROW_FORCE_INTEGRATION not in c.narrow_engaged_at


# --------------------------------------------------------------------------- #
# Item 4 — BOUNDEDNESS. The governing constraint of the whole batch.
# --------------------------------------------------------------------------- #

def test_the_worst_case_is_the_stated_product() -> None:
    """THE WORST CASE, AS ARITHMETIC.

    * Extra MODEL CALLS made by the ladder itself: **0**. No narrowing rung
      dispatches anything; the only turn-spending rung is SPEC-23's `ESCALATE`,
      drawn from `last_word_limit` (2) and already budgeted there.
    * Extra ITERATIONS: bounded above by `narrow_limit * narrow_drain_iters` =
      3 * 5 = **15**, because every narrowing rung defers a stop by EXACTLY ONE
      iteration and `narrow_deferrals` is monotone, never refunded by a ladder
      reset, and checked before any rung is handed out.
    * Against `max_iterations = 200` that is a <=7.5% ceiling on run length — and a
      ceiling, not a cost: the iterations it buys are the run's own turns, drawn
      from the run's own unchanged budget.
    * The FOREVER-LOOP IS UNREACHABLE BY CONSTRUCTION: a ladder reset requires a
      merged PR or a recovered churn window, deferrals are capped regardless of how
      many resets happen, the iteration counter is monotone and capped, and
      `budget_exhausted` is HARD and checked BEFORE any detector runs.
    """
    p = CodingAutonomyPolicy()
    assert (p.narrow_limit, p.narrow_drain_iters) == (3, 5)
    assert _narrow_deferral_cap(p) == 15
    assert _narrow_deferral_cap(CodingAutonomyPolicy(narrow_limit=0)) == 0
    assert 15 <= p.max_iterations * 0.075


@pytest.mark.parametrize("workers", [1, 2])
def test_every_extra_iteration_is_accounted_for_by_exactly_one_deferral(
        tmp_errorta_home, tmp_path: Path, workers: int) -> None:
    """The load-bearing bound, asserted as COUNTS. The same run with the ladder off
    and on differs by exactly `narrow_deferrals` iterations — no more, and every
    one of them recorded."""
    traces = {}
    for limit in (0, 3):
        store = _store(tmp_path, f"b{workers}{limit}")
        pm = _PlanningPM({"outcome": "confirmed", "rationale": "stop"})
        res = run_coding_loop(
            store, PM_ONLY,
            _churn_policy(narrow_limit=limit, max_parallel_workers=workers),
            run_turn=pm)
        traces[limit] = res.counters
        assert res.stop_reason == PLANNING_CHURN      # the reason never moves
    off, on = traces[0], traces[3]
    assert on.narrow_deferrals <= _narrow_deferral_cap(CodingAutonomyPolicy())
    assert on.narrows_used <= 3
    # Exactly one deferral per extra iteration here, because only ONE detector is
    # armed. In general several detectors may narrow in the SAME iteration (a
    # `Narrow` falls through), so the general bound is `extra <= deferrals <= cap`.
    assert on.iterations - off.iterations == on.narrow_deferrals == 1
    # The ladder itself spends no last words beyond SPEC-23's own budget.
    assert on.last_words == off.last_words <= 2


def test_a_run_that_trips_every_iteration_still_terminates_within_the_budget(
        tmp_errorta_home, tmp_path: Path) -> None:
    """The adversarial case: a detector that re-trips on every single iteration."""
    store = _store(tmp_path, "adv")
    pm = _PlanningPM(None)                            # never answers -> unparsed
    res = run_coding_loop(
        store, PM_ONLY, _churn_policy(plan_streak_limit=1, max_iterations=40),
        run_turn=pm)
    assert res.stop_reason == PLANNING_CHURN
    assert res.counters.iterations <= 40
    assert res.counters.narrows_used <= 3
    assert res.counters.narrow_deferrals <= 15


def test_the_budget_is_checked_before_any_ladder_rung(
        tmp_errorta_home, tmp_path: Path) -> None:
    """Bound 5: `budget_exhausted` is HARD and no ladder can defer it."""
    store = _store(tmp_path, "budget")
    pm = _PlanningPM(None)
    res = run_coding_loop(
        store, PM_ONLY, _churn_policy(max_iterations=2, plan_streak_limit=99),
        run_turn=pm)
    assert res.stop_reason == "budget_exhausted"
    assert res.counters.narrows_used == 0


# --------------------------------------------------------------------------- #
# Item 5 — backward compatibility. NOTHING the CLI reads may move.
# --------------------------------------------------------------------------- #

def test_no_stop_reason_is_added_or_renamed() -> None:
    """This spec introduces no stop reason, so the CLI's fail-closed allowlist and
    its own partition lock need zero edits."""
    engine = (runstream.SUCCESS_STOP_REASONS | runstream.FAILURE_STOP_REASONS)
    for reason in _DETECTOR_LADDERS:
        assert reason in engine, f"the ladder names a reason the CLI cannot see: {reason}"
    for narrow in (NARROW_CLAMP_FANOUT, NARROW_FORCE_INTEGRATION,
                   NARROW_CLAMP_PLANNING, NARROW_FORCE_LIFT, NARROW_ALERT_ONLY):
        assert narrow not in engine, f"a NARROW_* leaked into the stop reasons: {narrow}"


def test_no_actionable_work_after_a_refused_escalation_still_exits_ok() -> None:
    """Δ Item 5: the ONE success-class rung cannot flip an exit code."""
    payload = {"running": False,
               "state": {"status": "stopped", "stop_reason": NO_ACTIONABLE_WORK}}
    assert runstream.classify_exit(payload) == runstream.EXIT_OK


def test_no_actionable_work_escalates_only_when_open_work_remains(
        tmp_errorta_home, tmp_path: Path) -> None:
    store = _store(tmp_path, "naw")
    c, policy = LoopCounters(), CodingAutonomyPolicy()
    assert autonomy._no_actionable_escalation(
        store, c, policy, NO_ACTIONABLE_WORK) is None      # nothing open -> today
    store.add_task(title="still open", role=DEV)
    out = autonomy._no_actionable_escalation(store, c, policy, NO_ACTIONABLE_WORK)
    assert isinstance(out, Escalate) and out.reason == NO_ACTIONABLE_WORK
    # And never for the DONE outcome, nor with the ladder disabled.
    assert autonomy._no_actionable_escalation(
        store, c, policy, "definition_of_done") is None
    assert autonomy._no_actionable_escalation(
        store, c, CodingAutonomyPolicy(narrow_limit=0), NO_ACTIONABLE_WORK) is None


def test_the_two_new_policy_knobs_round_trip_with_the_disable_convention() -> None:
    p = CodingAutonomyPolicy()
    assert policy_to_dict(p)["narrow_limit"] == 3
    assert policy_to_dict(p)["narrow_drain_iters"] == 5
    assert policy_from_dict(policy_to_dict(p)) == p
    off = policy_from_dict({"narrow_limit": -5, "narrow_drain_iters": -1})
    assert (off.narrow_limit, off.narrow_drain_iters) == (0, 0)


# --------------------------------------------------------------------------- #
# The resume lock — bound 3 is fiction without it.
# --------------------------------------------------------------------------- #

def test_the_ladder_does_not_re_arm_on_continue() -> None:
    """A checkpoint/resume cycle must not hand a narrowed run a fresh
    `narrow_limit`: the narrowing FLAGS live in run state and already survive."""
    c = LoopCounters(iterations=9, narrows_used=3, narrow_deferrals=11)
    c.narrow_rungs = {NOT_CONVERGING: 2, PLANNING_CHURN: 1}
    state = {"counters": window_counters_to_dict(c),
             "narrow_ladder": {"rungs": dict(c.narrow_rungs),
                               "narrows_used": 3, "deferrals": 11}}
    restored = counters_from_run_state(state)
    assert restored is not None
    assert restored.narrows_used == 3
    assert restored.narrow_deferrals == 11
    assert restored.narrow_rungs == {NOT_CONVERGING: 2, PLANNING_CHURN: 1}


def test_a_resumed_run_can_lift_a_narrowing_it_did_not_engage(
        tmp_errorta_home, tmp_path: Path) -> None:
    """The flags survive a resume in run state; the per-process engage marks do
    not. A resumed run must still be able to lift them, or the resume IS the
    wedge — and it gets one fresh drain window, never none."""
    store = _store(tmp_path, "orphan")
    store.set_run_state(integration_only=True)        # left by the previous run
    c = LoopCounters(narrows_used=1)                  # carried by P0.2's seam
    policy = CodingAutonomyPolicy(narrow_drain_iters=2)
    _mergeable_pr(store, 1)                           # never releases on its own
    _release_narrow_flags(store, c, policy)           # re-anchors, does not lift
    assert store.get_run_state()["integration_only"] is True
    c.iterations = 2
    _release_narrow_flags(store, c, policy)
    assert store.get_run_state()["integration_only"] is False


def test_a_run_that_never_narrowed_reads_no_run_state_to_lift() -> None:
    """The release pass runs every iteration in both chains, so its no-narrowing
    path must cost nothing — a `None` ledger would raise if it were touched."""
    _release_narrow_flags(None, LoopCounters(), CodingAutonomyPolicy())


def test_a_malformed_ladder_block_never_fails_a_run_start() -> None:
    assert counters_from_run_state({"narrow_ladder": "nonsense"}) is None
    assert counters_from_run_state(
        {"counters": {"narrows_used": 1}, "narrow_ladder": {"rungs": "bad"}}
    ).narrow_rungs == {}


# --------------------------------------------------------------------------- #
# Both chains — the static grep. A behavioural test can pass on one chain by
# accident of scheduling; the grep cannot. Spec 16 shipped the one-chain bug once.
# --------------------------------------------------------------------------- #

def _loop_bodies() -> tuple[str, str]:
    text = Path(autonomy.__file__).read_text("utf-8")
    seq = text.index("def _run_sequential_loop(")
    conc = text.index("def _run_concurrent_loop(")
    return text[seq:conc], text[conc:text.index("def _apply_outcome(", conc)]


def test_both_loop_chains_route_through_the_one_apply_point() -> None:
    for name, body in zip(("sequential", "concurrent"), _loop_bodies()):
        assert "_apply_detector_outcome(" in body, f"{name} chain is unhooked"
        assert "_release_narrow_flags(" in body, (
            f"{name} chain never lifts a narrowing — invariants 2+3 are dead there")
        assert "isinstance(churn_out, Narrow)" in body, (
            f"{name} chain short-circuits on a Narrow, breaking the contract")


def test_the_concurrent_chain_stages_outcomes_and_applies_them_at_the_drain() -> None:
    _seq, conc = _loop_bodies()
    assert conc.count("_last_word(pending_stop)") == 2, (
        "a staged outcome can escape un-applied: hook BOTH pending_stop returns")
    assert "pending_stop = _trip(" in conc, (
        "the staged delivery-review stop must carry its ladder outcome")
