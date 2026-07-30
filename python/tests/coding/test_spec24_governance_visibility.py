"""SPEC-24 — governance visibility (the PM can see the countdown).

The thesis: you cannot course-correct against a threshold you cannot observe. An
operator handed the 2026-07-26 trace diagnosed those stops instantly; the PM,
which is the same model in the same room, could not — because nothing in its
prompt said the gate had been flat for six iterations. The difference is
information, not intelligence.

Locked here:

* **the absence rule** — nothing near a threshold ⇒ no `run_state` key, no prompt
  segment, and prompt bytes identical to a run without this feature. This is the
  SPEC-12 Item 3 discipline, and `test_prompt_segments_golden.py` passing
  unmodified in shape is the other half of it;
* **presence and accuracy** — the exact counters, the LIVE thresholds, and
  nothing about a detector still below its trigger;
* **the proximity table** as data, including the `threshold - 1` clamp that gives
  a small window a warning band at all, and the `0.0` kill switch;
* **both loop chains**, as a static grep — a behavioural test can pass on one
  chain by accident of scheduling, a grep cannot (Spec 16 shipped that bug once);
* **the framing** — a phrase blacklist over a matrix of near-reading combinations,
  because a PM told it is about to be punished for not finishing has an obvious
  cheap escape: claim done. That is the single failure mode Item 4 exists to
  prevent;
* **the drift canary** — every stop reason is either published or named in an
  explicit not-rendered map with a stated reason. Invisibility is how this gap
  happened the first time;
* **cost control** — the expensive ledger enrichments are asserted un-called for a
  quiet run, and an unchanged snapshot is asserted to elide its write;
* **the shared renderer** (Item 6) — SPEC-23's `_last_word_prompt` calls this
  renderer instead of growing a second copy of the same numbers.
"""
from __future__ import annotations

import inspect
import math
from pathlib import Path

import pytest

from errorta_council.coding import attention, autonomy, detector_state, runner
from errorta_council.coding.autonomy import (
    BUDGET_EXHAUSTED,
    COMPLETION_BLOCKED,
    DELIVERY_REVIEW_STALLED,
    DISPATCH_WEDGED,
    GATE_NOT_IMPROVING,
    HARD_STOP_REASONS,
    HEURISTIC_STOP_REASONS,
    MEMBER_UNHEALTHY,
    NO_PROGRESS,
    NOT_CONVERGING,
    PLANNING_CHURN,
    REVISE_LIVELOCK,
    TERMINAL_STOP_REASONS,
    CodingAutonomyPolicy,
    LoopCounters,
    _detector_snapshot,
    publish_detector_state,
)
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.runner import _pm_prompt, _pm_prompt_segments

# Item 4's blacklist: every phrase that turns a measurement into a threat or an
# instruction. None of these may appear in the rendered block, ever.
BLACKLIST = (
    "you should", "you must", "declare done", "stop now", "give up",
    "before the run is killed",
)


def _store(tmp_path: Path, name: str = "spec24") -> LedgerStore:
    s = LedgerStore(name, root=tmp_path)
    s.create_project(
        north_star="n", definition_of_done="d", target="new", repo_path=None)
    return s


def _publish(store: LedgerStore, c: LoopCounters,
             policy: CodingAutonomyPolicy | None = None) -> str:
    publish_detector_state(store, c, policy or CodingAutonomyPolicy())
    return detector_state.prompt_text(store)


# --------------------------------------------------------------------------- #
# Item 3 — the proximity rule.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("threshold,expected", [
    (2, 1),      # pm_idle_limit — the `threshold - 1` clamp IS the warning band
    (3, 2),      # delivery_review_round_limit
    (5, 3),      # wedge_stall_limit / revise_livelock_limit
    (6, 4),      # plan_streak_limit
    (8, 5),      # gate_stall_limit
    (20, 12),    # convergence_stall_limit
    (200, 120),  # max_iterations
    (1, 0),      # a threshold of exactly 1 has NO band — opted out, not a bug
    (0, 0),      # a disabled detector is never rendered
    (-4, 0),
])
def test_the_proximity_table_is_the_specs_worked_column(threshold, expected):
    """Item 3's worked table, as data.

    Without the `threshold - 1` clamp, `ceil(0.6 * 2) == 2` would mean the PM is
    first told about idleness in the same iteration the run stops on it — which is
    worthless, and is why the clamp is load-bearing rather than defensive."""
    assert detector_state.trigger(threshold, 0.6) == expected


@pytest.mark.parametrize("threshold", [2, 3, 5, 6, 8, 20, 200])
def test_trigger_never_reaches_its_own_threshold(threshold):
    """A trigger that equalled the threshold would render exactly once: in the
    iteration the detector already fired. Every band must open strictly earlier."""
    t = detector_state.trigger(threshold, 0.6)
    assert 1 <= t < threshold
    assert t <= math.ceil(0.6 * threshold)


def test_zero_ratio_is_the_kill_switch():
    assert detector_state.trigger(8, 0.0) == 0
    assert not detector_state.is_near(7, 8, 0.0)


def test_is_near_is_inclusive_at_the_trigger():
    assert not detector_state.is_near(4, 8, 0.6)
    assert detector_state.is_near(5, 8, 0.6)
    assert detector_state.is_near(9, 8, 0.6)


# --------------------------------------------------------------------------- #
# Item 3 — the ABSENCE rule (the important one).
# --------------------------------------------------------------------------- #
def test_a_quiet_run_publishes_nothing_and_renders_nothing(tmp_path: Path):
    """Nothing near, no clamp, no signals ⇒ no key, no text, no segment. This is
    verbatim the contract Spec 12 established for `gate_output`, and it is why a
    healthy run's PM prompt does not change by one byte."""
    store = _store(tmp_path)
    c = LoopCounters(iterations=3, model_calls=3, last_progress_iter=3)
    assert _detector_snapshot(store, c, CodingAutonomyPolicy()) is None
    assert _publish(store, c) == ""
    assert store.get_run_state().get("detector_state") is None


def test_a_quiet_run_omits_the_segment_entirely_not_an_empty_one(tmp_path: Path):
    """OMITTED, not empty. `_composition_from_segments` would skip an empty-text
    segment anyway, but the contract is omission — so assert the contract."""
    store = _store(tmp_path)
    _publish(store, LoopCounters(iterations=3, last_progress_iter=3))
    segs = _pm_prompt_segments(store, pin="", done_gate="")
    assert not [s for s in segs if s.class_ == "governance_state"]
    assert "GOVERNANCE STATE" not in _pm_prompt(store)


def test_the_kill_switch_reproduces_todays_prompt_bytes(tmp_path: Path):
    """`governance_proximity=0.0` restores today's bytes for EVERY run, near or
    not — the batch's escape hatch and its regression story."""
    store = _store(tmp_path)
    hot = LoopCounters(iterations=41, model_calls=96, pm_idle=1, plan_streak=4,
                       last_gate_best=6, last_gate_iter=36, last_progress_iter=41)
    quiet_bytes = _pm_prompt(store)

    publish_detector_state(store, hot, CodingAutonomyPolicy(governance_proximity=0.0))
    assert store.get_run_state().get("detector_state") is None
    assert _pm_prompt(store) == quiet_bytes

    # ...and the same counters at the default ratio DO change it, so the assertion
    # above is a real lock and not a tautology.
    publish_detector_state(store, hot, CodingAutonomyPolicy())
    assert _pm_prompt(store) != quiet_bytes


def test_a_disabled_detector_is_never_rendered(tmp_path: Path):
    """Telling a PM about a window that cannot fire is pure noise."""
    store = _store(tmp_path)
    c = LoopCounters(iterations=41, plan_streak=40, last_progress_iter=41)
    policy = CodingAutonomyPolicy(plan_streak_limit=0)
    text = _publish(store, c, policy)
    assert "planning_churn" not in text


# --------------------------------------------------------------------------- #
# Items 1-2 — presence and accuracy.
# --------------------------------------------------------------------------- #
def test_the_snapshot_carries_exactly_the_near_readings(tmp_path: Path):
    """Item 2's acceptance, verbatim: pm_idle=1, plan_streak=4, gate flat 5 ⇒
    exactly those three entries with those values and their LIVE thresholds, and a
    `wedge_streak=0` produces no entry at all."""
    store = _store(tmp_path)
    c = LoopCounters(iterations=41, model_calls=96, pm_idle=1, plan_streak=4,
                     last_gate_best=6, last_gate_iter=36, last_progress_iter=41,
                     wedge_streak=0)
    publish_detector_state(store, c, CodingAutonomyPolicy())
    snap = store.get_run_state()["detector_state"]

    rows = {r["detector"]: r for r in snap["near"]}
    assert set(rows) == {NO_PROGRESS, PLANNING_CHURN, GATE_NOT_IMPROVING}
    assert (rows[NO_PROGRESS]["current"], rows[NO_PROGRESS]["threshold"]) == (1, 2)
    assert (rows[PLANNING_CHURN]["current"],
            rows[PLANNING_CHURN]["threshold"]) == (4, 6)
    assert (rows[GATE_NOT_IMPROVING]["current"],
            rows[GATE_NOT_IMPROVING]["threshold"]) == (5, 8)
    assert snap["iteration"] == 41
    assert snap["budget"] == {"iterations": 41, "max_iterations": 200,
                              "model_calls": 96, "max_model_calls": None}

    text = detector_state.prompt_text(store)
    for fragment in ("the no_progress window is 2", "the planning_churn window is 6",
                     "the gate_not_improving window is 8", "iteration 41 of 200"):
        assert fragment in text
    assert DISPATCH_WEDGED not in text  # below its trigger — nowhere in the string


def test_live_thresholds_not_hardcoded_ones(tmp_path: Path):
    """The prompt must quote the threshold the detector is ACTUALLY enforcing —
    the whole point of publishing rather than re-deriving."""
    store = _store(tmp_path)
    policy = CodingAutonomyPolicy(gate_stall_limit=30)
    c = LoopCounters(iterations=41, last_gate_best=2, last_gate_iter=21,
                     last_progress_iter=41)
    text = _publish(store, c, policy)
    assert "the gate_not_improving window is 30 iterations" in text


@pytest.mark.parametrize("detector,counters,expected_fragment", [
    (NO_PROGRESS, {"pm_idle": 1}, "the no_progress window is 2"),
    (NOT_CONVERGING, {"iterations": 13, "last_progress_iter": 0},
     "the not_converging window is 20"),
    (GATE_NOT_IMPROVING, {"iterations": 10, "last_gate_best": 4, "last_gate_iter": 5},
     "the gate_not_improving window is 8"),
    (PLANNING_CHURN, {"plan_streak": 4}, "the planning_churn window is 6"),
    (DISPATCH_WEDGED, {"wedge_streak": 3}, "the dispatch_wedged window is 5"),
    (REVISE_LIVELOCK,
     {"iterations": 4, "last_broken_count": 2, "last_broken_iter": 0},
     "the revise_livelock window is 5"),
    (DELIVERY_REVIEW_STALLED, {"delivery_review_rounds": 2},
     "the delivery_review_stalled window is 3"),
    (COMPLETION_BLOCKED, {"false_done_streak": 1},
     "the completion_blocked window is 2"),
    (MEMBER_UNHEALTHY, {"member_fail_counts": {"m-dev": 2}},
     "the member_unhealthy window is 3"),
])
def test_the_seam_round_trips_every_published_window(
        tmp_path: Path, detector, counters, expected_fragment):
    """Set the counter, publish, assert `run_state.detector_state` carries the
    value AND that `prompt_text` renders it.

    This is what catches a counter that was renamed on `LoopCounters` and silently
    stopped being published — the failure mode the whole spec exists to prevent,
    applied to the spec's own machinery."""
    store = _store(tmp_path)
    base = {"iterations": 5, "last_progress_iter": 5}
    base.update(counters)
    c = LoopCounters(**base)
    text = _publish(store, c)
    snap = store.get_run_state()["detector_state"]
    assert detector in {r["detector"] for r in snap["near"]}, snap
    assert expected_fragment in text


def test_worker_unproductive_reads_the_worst_task_and_the_ladder_spend(
        tmp_path: Path):
    store = _store(tmp_path)
    c = LoopCounters(iterations=5, last_progress_iter=5,
                     unproductive_counts={("m-dev", "t1"): 1, ("m-dev", "t2"): 0},
                     task_reassignments=1, model_escalations=0, pm_assists=0)
    text = _publish(store, c)
    assert "the worker_unproductive window is 2" in text
    assert "Ladder spent: 1 of 2 reassignment(s)" in text


def test_the_gate_row_is_silent_until_the_gate_has_a_signal(tmp_path: Path):
    """`last_gate_best == -1` is the no-signal sentinel `_account_gate_stall`
    refuses to trip on; there is no window to report either."""
    store = _store(tmp_path)
    c = LoopCounters(iterations=41, last_gate_best=-1, last_gate_iter=0,
                     last_progress_iter=41, pm_idle=1)
    text = _publish(store, c)
    assert GATE_NOT_IMPROVING not in text


def test_open_attention_signals_summon_the_block_on_their_own(tmp_path: Path):
    """The last line of Item 2's table: the signals a detector already raised at a
    HUMAN are also part of what the PM can see."""
    store = _store(tmp_path)
    attention.raise_monitor_problem(
        store.project_id, stage="development", detector="gate_not_improving",
        reason="the gate has been flat", store=store)
    c = LoopCounters(iterations=3, last_progress_iter=3)
    text = _publish(store, c)
    assert "open attention signals" in text


def test_the_convergence_clamp_renders_because_it_is_a_state_not_a_countdown(
        tmp_path: Path):
    """GL04 does not stop the run, it CHANGES HOW THE RUN BEHAVES (forced serial
    integration). A PM planning a wide batch into a clamped run is planning against
    a machine it cannot see — so an engaged clamp renders regardless of proximity.
    """
    store = _store(tmp_path)
    store.set_run_state(convergence_clamped=True)
    c = LoopCounters(iterations=3, last_progress_iter=3)
    text = _publish(store, c)
    assert "convergence clamp: ENGAGED" in text
    assert "forced serial" in text


# --------------------------------------------------------------------------- #
# Item 1 — both loop chains (the dead-code lock).
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("loop", ["_run_sequential_loop", "_run_concurrent_loop"])
def test_both_loop_chains_publish(loop):
    """A publisher in one chain only is dead code exactly where it matters: the
    concurrent loop is where real fanned-out runs live once Spec 13 lifts the
    foundation clamp. A behavioural test can pass on one chain by accident of
    scheduling; this grep cannot."""
    src = inspect.getsource(getattr(autonomy, loop))
    assert "publish_detector_state(" in src


def test_a_real_loop_iteration_publishes(tmp_path: Path):
    """The behavioural half: run the sequential loop to its budget and assert the
    snapshot on `run_state` was written by the loop, not by a test helper."""
    from errorta_council.coding.autonomy import TurnOutcome, run_coding_loop
    from errorta_council.coding.topology import DEV, PM

    store = _store(tmp_path, "spec24loop")
    store.add_task(title="t", role="dev", detail="d")

    def run_turn(action, ledger):
        return TurnOutcome(kind="noop", made_progress=False)

    policy = CodingAutonomyPolicy(max_iterations=6, last_word_limit=0,
                                  pm_idle_limit=99, checkpoint_cadence="off")
    run_coding_loop(store, [("m-pm", PM), ("m-dev", DEV)], policy,
                    run_turn=run_turn)
    snap = store.get_run_state().get("detector_state")
    assert isinstance(snap, dict) and snap["iteration"] >= 1


# --------------------------------------------------------------------------- #
# Item 1 — write elision and cost control.
# --------------------------------------------------------------------------- #
def test_an_unchanged_snapshot_is_not_rewritten(tmp_path: Path):
    """A quiet 200-iteration run must not rewrite `run_state.json` 200 times."""
    store = _store(tmp_path)
    writes: list = []
    real = store.set_run_state
    store.set_run_state = lambda **kw: (writes.append(kw), real(**kw))[1]

    c = LoopCounters(iterations=41, pm_idle=1, last_progress_iter=41)
    publish_detector_state(store, c, CodingAutonomyPolicy())
    publish_detector_state(store, c, CodingAutonomyPolicy())
    publish_detector_state(store, c, CodingAutonomyPolicy())
    assert len(writes) == 1


def test_a_quiet_run_writes_nothing_at_all(tmp_path: Path):
    store = _store(tmp_path)
    writes: list = []
    real = store.set_run_state
    store.set_run_state = lambda **kw: (writes.append(kw), real(**kw))[1]
    for i in range(5):
        publish_detector_state(store, LoopCounters(iterations=i,
                                                   last_progress_iter=i),
                               CodingAutonomyPolicy())
    assert writes == []


def test_expensive_ledger_reads_only_happen_for_near_readings(
        tmp_path: Path, monkeypatch):
    """Cost control ASSERTED, not assumed: `_dispatch_wedge_culprits` walks the
    whole `depends_on` closure, so it may only run for a reading that is already
    going to be rendered."""
    store = _store(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(autonomy, "_dispatch_wedge_culprits",
                        lambda *a, **k: calls.append("wedge") or "w")
    monkeypatch.setattr(autonomy, "_open_backlog_shape",
                        lambda *a, **k: calls.append("backlog") or (0, 0))

    publish_detector_state(store, LoopCounters(iterations=3, last_progress_iter=3),
                           CodingAutonomyPolicy())
    assert calls == []

    publish_detector_state(
        store, LoopCounters(iterations=3, last_progress_iter=3, plan_streak=4,
                            wedge_streak=3),
        CodingAutonomyPolicy())
    assert sorted(calls) == ["backlog", "wedge"]


# --------------------------------------------------------------------------- #
# Item 4 — the framing (observed state, not an instruction, not a threat).
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("counters", [
    {"pm_idle": 1},
    {"plan_streak": 4},
    {"wedge_streak": 3},
    {"delivery_review_rounds": 2},
    {"false_done_streak": 1},
    {"member_fail_counts": {"m": 2}},
    {"pm_idle": 1, "plan_streak": 5, "wedge_streak": 4, "iterations": 190},
    {"iterations": 199, "last_progress_iter": 0, "last_gate_best": 0,
     "last_gate_iter": 0, "delivery_review_rounds": 3},
])
def test_the_block_never_threatens_and_never_instructs(tmp_path: Path, counters):
    """A model told it is about to be PUNISHED for not finishing has an obvious
    cheap escape — claim done. That is the single failure mode Item 4 exists to
    prevent, so the blacklist is checked over a matrix of near-reading
    combinations, not one happy path."""
    store = _store(tmp_path, "frame")
    base = {"iterations": 41, "last_progress_iter": 41}
    base.update(counters)
    text = _publish(store, LoopCounters(**base))
    assert text, "fixture must actually render something"
    lowered = text.lower()
    for phrase in BLACKLIST:
        assert phrase not in lowered, phrase
    assert detector_state.ANTI_DONE_SENTENCE in text
    assert "not an instruction" in text


def test_the_closing_line_tells_the_truth_about_the_intervention(tmp_path: Path):
    """Item 4, rule 4: telling a PM it will be consulted when it will not be is
    worse than telling it the truth."""
    store = _store(tmp_path)
    c = LoopCounters(iterations=41, pm_idle=1, last_progress_iter=41)

    live = _publish(store, c, CodingAutonomyPolicy(last_word_limit=2))
    assert "you are asked first to propose a concrete next action" in live
    assert "ends the run with that reason recorded" not in live

    spent = _publish(store, c, CodingAutonomyPolicy(last_word_limit=0))
    assert "ends the run with that reason recorded" in spent
    assert "propose a concrete next action" not in spent


def test_the_block_is_stable_under_re_render(tmp_path: Path):
    """No timestamps, no ordering nondeterminism — a re-render of unchanged inputs
    is byte-identical, so the PM never sees a diff that isn't a real change."""
    store = _store(tmp_path)
    c = LoopCounters(iterations=41, pm_idle=1, plan_streak=4, last_progress_iter=41)
    first = _publish(store, c)
    assert first == _publish(store, c) == detector_state.prompt_text(store)


def test_the_readings_section_is_bounded(tmp_path: Path):
    store = _store(tmp_path)
    for i in range(30):
        attention.raise_review_alert(
            store.project_id, stage="development", title=f"alert {i} " + "x" * 400,
            summary="s", store=store)
    text = _publish(store, LoopCounters(iterations=3, last_progress_iter=3))
    # Header + capped readings + closing: the readings section cannot crowd out
    # the framing, which is what makes the block safe.
    assert detector_state.ANTI_DONE_SENTENCE in text
    assert len(text) < detector_state.READINGS_CAP + 1200


# --------------------------------------------------------------------------- #
# Item 5 — where the segment goes.
# --------------------------------------------------------------------------- #
def test_the_segment_lands_at_the_reserved_position_exactly_once(tmp_path: Path):
    store = _store(tmp_path)
    _publish(store, LoopCounters(iterations=41, pm_idle=1, last_progress_iter=41))
    classes = [s.class_ for s in _pm_prompt_segments(store, pin="", done_gate="")]
    assert classes.count("governance_state") == 1
    # The P0.4 tail order: governance_state -> tool_guidance -> role_instructions.
    i = classes.index("governance_state")
    assert classes[i + 1] == "tool_guidance"
    assert classes[-1] == "role_instructions"
    assert _pm_prompt(store).count("GOVERNANCE STATE") == 1


def test_the_composition_attributes_the_block_to_its_own_class(tmp_path: Path):
    from errorta_council.coding.runner import _composition_from_segments

    store = _store(tmp_path)
    _publish(store, LoopCounters(iterations=41, pm_idle=1, last_progress_iter=41))
    segs = _pm_prompt_segments(store, pin="", done_gate="")
    comp = _composition_from_segments(segs)
    assert any(cat["class"] == "governance_state" and cat["tokens"] > 0
               for cat in comp["categories"])


def test_governance_state_is_declared_in_the_taxonomy():
    assert "governance_state" in runner._COMPOSITION_CLASSES


def test_a_dev_prompt_never_carries_run_level_telemetry(tmp_path: Path):
    """Non-goal, asserted: a dev turn cannot act on `plan_streak` and would only
    pay tokens for it."""
    from errorta_council.coding.runner import _dev_prompt

    store = _store(tmp_path)
    task = store.add_task(title="X", role="dev", detail="Y")
    _publish(store, LoopCounters(iterations=41, pm_idle=1, last_progress_iter=41))
    assert "GOVERNANCE STATE" not in _dev_prompt(task, store)


def test_a_pm_assist_prompt_stays_task_scoped(tmp_path: Path):
    from errorta_council.coding.runner import _pm_assist_prompt

    store = _store(tmp_path)
    task = store.add_task(title="X", role="dev", detail="Y")
    _publish(store, LoopCounters(iterations=41, pm_idle=1, last_progress_iter=41))
    assert "GOVERNANCE STATE" not in _pm_assist_prompt(store, task)


# --------------------------------------------------------------------------- #
# Item 6 — ONE renderer, reused by SPEC-23's last-word turn.
# --------------------------------------------------------------------------- #
def test_the_last_word_prompt_calls_this_renderer_and_owns_no_arithmetic():
    """The anti-drift lock. A second evidence renderer beside this one would
    guarantee that the numbers in the standing prompt and the numbers in the
    intervention prompt eventually disagree — the exact duplication this batch
    keeps paying for."""
    src = inspect.getsource(runner._last_word_prompt)
    assert "_detector_state.prompt_text(" in src
    # Only the executable body counts: the docstring and the comments EXPLAIN the
    # delegation, and forbidding them from naming it would be perverse.
    body = "\n".join(
        line for line in src.split('"""')[-1].splitlines()
        if not line.strip().startswith("#"))
    for phrase in ("window is", "threshold", "trigger(", "policy."):
        assert phrase not in body, phrase


def test_the_focused_render_puts_the_tripped_detector_first(tmp_path: Path):
    """A PM asked to propose an alternative should see the REST of the board —
    that is precisely the 'same model, radically less information' defect."""
    store = _store(tmp_path)
    c = LoopCounters(iterations=41, pm_idle=1, plan_streak=4, last_gate_best=6,
                     last_gate_iter=33, last_progress_iter=41)
    publish_detector_state(store, c, CodingAutonomyPolicy())
    focused = detector_state.prompt_text(
        store, focus=GATE_NOT_IMPROVING, focus_evidence="the gate is flat")
    lines = [ln for ln in focused.splitlines() if ln.startswith("- ")]
    assert lines[0].startswith("- acceptance gate:")
    assert any("planning" in ln for ln in lines[1:])
    assert "reached its window" in focused
    # Rules 1-3 survive the header swap.
    for phrase in BLACKLIST:
        assert phrase not in focused.lower()
    assert detector_state.ANTI_DONE_SENTENCE in focused


def test_a_focused_render_needs_no_published_snapshot(tmp_path: Path):
    """The trip can happen before any quiescent publish (or after a ledger
    hiccup); the tripped reading rides on the action itself, so the last-word turn
    is never sent without naming what tripped."""
    store = _store(tmp_path)
    text = detector_state.prompt_text(
        store, focus=NOT_CONVERGING, focus_evidence="nothing moved for 20 iterations")
    assert "- not_converging: nothing moved for 20 iterations." in text


def test_the_last_word_prompt_carries_the_focused_render_verbatim(tmp_path: Path):
    """Item 6's acceptance: the evidence a last-word turn carries is byte-identical
    to the focused render of the same snapshot."""
    from errorta_council.coding.topology import LastWord

    store = _store(tmp_path)
    publish_detector_state(
        store, LoopCounters(iterations=41, pm_idle=1, last_gate_best=6,
                            last_gate_iter=33, last_progress_iter=41),
        CodingAutonomyPolicy())
    evidence = "acceptance gate has not improved for 8 iterations (score=6)"
    expected = detector_state.prompt_text(
        store, focus=GATE_NOT_IMPROVING, focus_evidence=evidence)
    assert expected
    prompt = runner._last_word_prompt(
        store, LastWord("m-pm", GATE_NOT_IMPROVING, evidence=evidence))
    assert expected in prompt


# --------------------------------------------------------------------------- #
# The drift canary.
# --------------------------------------------------------------------------- #
def test_every_stop_reason_is_published_or_explicitly_not_rendered():
    """A detector added later cannot silently become invisible.

    Invisibility is how this gap happened the first time, so a new stop reason
    added without a decision fails the build rather than quietly never appearing
    in the PM's prompt."""
    published = set(autonomy._SNAPSHOT_DETECTORS)
    excluded = set(autonomy._SNAPSHOT_NOT_RENDERED)
    assert not published & excluded, "a reason cannot be both"
    all_reasons = HEURISTIC_STOP_REASONS | HARD_STOP_REASONS | TERMINAL_STOP_REASONS
    undecided = all_reasons - published - excluded
    assert not undecided, f"stop reasons with no visibility decision: {undecided}"
    # Every heuristic reason — a detector's opinion the PM could act on — is
    # PUBLISHED, not excluded. Only events and the budget are excluded.
    assert HEURISTIC_STOP_REASONS <= published
    assert BUDGET_EXHAUSTED in excluded
    for reason, why in autonomy._SNAPSHOT_NOT_RENDERED.items():
        assert why.strip(), f"{reason} is excluded without a stated reason"


def test_every_published_detector_can_actually_render(tmp_path: Path):
    """The other half of the canary: a row declared in `_SNAPSHOT_DETECTORS` but
    never emitted by the builder would pass the set test above and still be
    invisible."""
    store = _store(tmp_path)
    c = LoopCounters(
        iterations=199, model_calls=1, pm_idle=9, plan_streak=9, wedge_streak=9,
        last_progress_iter=0, last_gate_best=3, last_gate_iter=0,
        last_broken_count=2, last_broken_iter=0, delivery_review_rounds=9,
        false_done_streak=9, unproductive_counts={("m", "t"): 9},
        member_fail_counts={"m": 9})
    publish_detector_state(store, c, CodingAutonomyPolicy())
    snap = store.get_run_state()["detector_state"]
    assert ({r["detector"] for r in snap["near"]}
            == set(autonomy._SNAPSHOT_DETECTORS))


# --------------------------------------------------------------------------- #
# Guards and hygiene.
# --------------------------------------------------------------------------- #
def test_a_ledger_hiccup_never_breaks_the_loop_or_a_turn():
    class Broken:
        project_id = "broken"

        def get_run_state(self):
            raise RuntimeError("boom")

        def set_run_state(self, **kw):
            raise RuntimeError("boom")

        def list_prs(self):
            raise RuntimeError("boom")

    broken = Broken()
    publish_detector_state(broken, LoopCounters(pm_idle=1), CodingAutonomyPolicy())
    assert detector_state.read(broken) is None
    assert detector_state.prompt_text(broken) == ""
    detector_state.clear(broken)


def test_a_corrupt_snapshot_degrades_to_todays_prompt(tmp_path: Path):
    store = _store(tmp_path)
    store.set_run_state(detector_state="not a dict")
    assert detector_state.prompt_text(store) == ""
    assert "GOVERNANCE STATE" not in _pm_prompt(store)


def test_run_start_clears_a_previous_runs_snapshot(tmp_path: Path):
    """A resumed run must never read the PREVIOUS run's windows as live: every
    detector window re-arms on `errorta continue`."""
    store = _store(tmp_path)
    publish_detector_state(
        store, LoopCounters(iterations=41, pm_idle=1, last_progress_iter=41),
        CodingAutonomyPolicy())
    assert store.get_run_state().get("detector_state") is not None
    detector_state.clear(store)
    assert store.get_run_state().get("detector_state") is None
    assert "_detector_state.clear(" in inspect.getsource(runner.CodingRunner.run)


def test_the_module_imports_neither_autonomy_nor_runner():
    """The `gate_state.py` circular-import discipline: this module is imported BY
    both, which is what lets `autonomy` publish and `runner` render with no cycle.
    """
    src = inspect.getsource(detector_state)
    assert "import autonomy" not in src
    assert "from .autonomy" not in src
    assert "import runner" not in src
    assert "from .runner" not in src
