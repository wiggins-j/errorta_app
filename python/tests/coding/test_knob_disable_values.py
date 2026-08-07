"""Knob-audit fixes — every autonomy knob's DISABLE value must reproduce prior
behaviour, and a value the docs imply is safe must never brick a run.

Four defects found by the 56-field audit of ``CodingAutonomyPolicy``:

1. ``pm_idle_limit=0`` stopped the run ``no_progress`` on the FIRST detector pass
   (``c.pm_idle >= 0``) instead of disabling the detector, and ``max_iterations=0``
   stopped ``budget_exhausted`` before the first turn. Neither was clamped.
2. F159 hot-file serialization had no off switch at all (``hot_file_threshold`` is
   a sensitivity dial clamped to ``>= 1``; ``1`` is MORE aggressive than the
   default ``2``).
3. The F127 ladder was half-disableable: its two later rungs clamp with
   ``max(0, …)`` but its first rung clamped to ``>= 1``, so it always fired.
4. ``reviewer_repo_read``'s prose claimed it defaults to ``dev_repo_read``; the
   code never carried the dev value across.
"""
from __future__ import annotations

from pathlib import Path

from errorta_council.coding import runner
from errorta_council.coding.autonomy import (
    BUDGET_EXHAUSTED,
    CADENCE_OFF,
    NO_PROGRESS,
    CodingAutonomyPolicy,
    LoopCounters,
    TurnOutcome,
    _handle_unproductive,
    frozen_paths,
    policy_from_dict,
    policy_to_dict,
    run_coding_loop,
    save_policy,
)
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.topology import DEV, PM, REVIEWER, TESTER, Assign, Plan

MEMBERS = [("m-pm", PM), ("m-dev1", DEV), ("m-dev2", DEV),
           ("m-rev", REVIEWER), ("m-test", TESTER)]


def _store(tmp_path: Path, name: str = "knobs") -> LedgerStore:
    s = LedgerStore(name, root=tmp_path)
    s.create_project(north_star="n", definition_of_done="d", target="new",
                     repo_path=None)
    return s


# --------------------------------------------------------------------------- #
# Defect 1 — pm_idle_limit / max_iterations must not brick a run at 0
# --------------------------------------------------------------------------- #

def _idle_run_turn(action, ledger) -> TurnOutcome:
    """A PM that never makes progress — the no-progress detector's own fixture."""
    return TurnOutcome(kind="planned", made_progress=False)


def test_pm_idle_limit_zero_disables_the_no_progress_detector(tmp_path: Path) -> None:
    """0 is the module's own "disabled" value. It used to stop the run on the
    FIRST pass, before a single PM turn had been judged."""
    s = _store(tmp_path, "idle0")
    res = run_coding_loop(
        s, MEMBERS,
        CodingAutonomyPolicy(checkpoint_cadence=CADENCE_OFF, pm_idle_limit=0,
                             max_iterations=3),
        run_turn=_idle_run_turn)
    assert res.stop_reason == BUDGET_EXHAUSTED   # ran the budget out, not no_progress
    assert res.counters.iterations == 3
    assert res.counters.pm_idle >= 1            # the counter still accrues


def test_pm_idle_limit_positive_still_stops(tmp_path: Path) -> None:
    """Regression lock on the other side of the guard: the detector is unchanged
    for every value an operator actually runs with."""
    s = _store(tmp_path, "idle2")
    res = run_coding_loop(
        s, MEMBERS,
        CodingAutonomyPolicy(checkpoint_cadence=CADENCE_OFF, pm_idle_limit=2),
        run_turn=_idle_run_turn)
    assert res.stop_reason == NO_PROGRESS


def test_pm_idle_limit_negative_clamps_to_disabled() -> None:
    assert policy_from_dict({"pm_idle_limit": -5}).pm_idle_limit == 0


def test_max_iterations_zero_clamps_to_one_iteration(tmp_path: Path) -> None:
    """`max_iterations` has NO disable value — it is the run's terminal spend
    backstop — so a `<= 0` operator value clamps up to one iteration instead of
    stopping `budget_exhausted` before any work happens."""
    assert policy_from_dict({"max_iterations": 0}).max_iterations == 1
    assert policy_from_dict({"max_iterations": -9}).max_iterations == 1

    s = _store(tmp_path, "iter0")
    policy = policy_from_dict(
        {**policy_to_dict(CodingAutonomyPolicy()),
         "max_iterations": 0, "checkpoint_cadence": CADENCE_OFF})
    res = run_coding_loop(s, MEMBERS, policy, run_turn=_idle_run_turn)
    assert res.stop_reason == BUDGET_EXHAUSTED
    assert res.counters.iterations == 1  # a turn actually ran


# --------------------------------------------------------------------------- #
# Defect 2 — F159 needs a clean off switch (hot_file_threshold cannot be one)
# --------------------------------------------------------------------------- #

def test_hot_file_serialization_defaults_on_and_round_trips() -> None:
    assert CodingAutonomyPolicy().hot_file_serialization is True
    assert policy_to_dict(CodingAutonomyPolicy())["hot_file_serialization"] is True
    assert policy_from_dict({"hot_file_serialization": False}).hot_file_serialization is False
    # Absent key -> unchanged default (no behaviour change for existing ledgers).
    assert policy_from_dict({}).hot_file_serialization is True


def test_hot_file_threshold_zero_is_not_an_off_switch() -> None:
    """The audit's premise: 0 clamps to 1, which makes EVERY conflicted path hot
    — the opposite of disabling. Locked so nobody documents 0 as the disable."""
    assert policy_from_dict({"hot_file_threshold": 0}).hot_file_threshold == 1


def test_serialization_off_never_escalates_or_freezes(tmp_path: Path) -> None:
    """With the switch off a conflict is handled exactly as it was pre-F159: no
    centralize owner, no frozen path — not even on the forced (resolve-cap) arm."""
    s = _store(tmp_path, "f159off")
    save_policy(s, CodingAutonomyPolicy(hot_file_serialization=False))
    for i in range(4):  # well past hot_file_escalation_threshold (4)
        t = s.add_task(title=f"t{i}", role=DEV)
        pr = s.record_pr(task_id=t.task_id, branch=f"br-{i}", head="h",
                         dev_member="m-dev1")
        s.update_pr(pr["pr_id"], status="conflict", conflicts=["src/mockData.ts"])

    runner._maybe_escalate_hot_files(s, ["src/mockData.ts"])
    assert frozen_paths(s) == set()
    assert not s.get_run_state().get("contract_owner_task_id")

    runner._maybe_escalate_hot_files(s, ["src/mockData.ts"], force=True)
    assert frozen_paths(s) == set()
    assert not s.get_run_state().get("contract_owner_task_id")
    assert not any(d.get("choice") == "hot_file_escalated" for d in s.list_decisions())


def test_serialization_on_still_escalates_and_freezes(tmp_path: Path) -> None:
    """The other side of the switch — the default path is untouched."""
    s = _store(tmp_path, "f159on")
    save_policy(s, CodingAutonomyPolicy())
    for i in range(4):
        t = s.add_task(title=f"t{i}", role=DEV)
        pr = s.record_pr(task_id=t.task_id, branch=f"br-{i}", head="h",
                         dev_member="m-dev1")
        s.update_pr(pr["pr_id"], status="conflict", conflicts=["src/mockData.ts"])
    runner._maybe_escalate_hot_files(s, ["src/mockData.ts"])
    assert "src/mockData.ts" in frozen_paths(s)


def _dispatch_probe(seen: list[str]):
    def run_turn(action, ledger) -> TurnOutcome:
        if isinstance(action, Plan):
            return TurnOutcome(kind="planned", made_progress=True)
        seen.append(action.task_id)
        return TurnOutcome(kind="noop")
    return run_turn


def _hot_owner_scenario(tmp_path: Path, name: str) -> tuple[LedgerStore, str]:
    """`src/mockData.ts` is hot from two merged conflicts and is currently held by
    a `doing` task with an open PR; a second todo task also declares it."""
    s = _store(tmp_path, name)
    for i in range(2):
        h = s.add_task(title=f"hist{i}", role=DEV)
        pr = s.record_pr(task_id=h.task_id, branch=f"br-h{i}", head="h",
                         dev_member="m-dev1")
        s.update_pr(pr["pr_id"], status="conflict", conflicts=["src/mockData.ts"])
        s.update_task(h.task_id, state="done")
        s.update_pr(pr["pr_id"], status="merged")
    owner = s.add_task(title="owner", role=DEV, target_files=["src/mockData.ts"])
    s.update_task(owner.task_id, state="doing")
    s.record_pr(task_id=owner.task_id, branch="br-own", head="h",
                dev_member="m-dev1")
    second = s.add_task(title="second", role=DEV, target_files=["src/mockData.ts"])
    return s, second.task_id


def _dispatch_policy(**kw) -> CodingAutonomyPolicy:
    # GL05's strict partition would hold the second task on its own, so it is off
    # here to isolate what F159's switch controls.
    return CodingAutonomyPolicy(
        checkpoint_cadence=CADENCE_OFF, max_iterations=4, pm_idle_limit=99,
        max_parallel_workers=2, strict_file_partition=False, **kw)


def test_serialization_on_holds_the_second_toucher_in_the_loop(tmp_path: Path) -> None:
    s, second_id = _hot_owner_scenario(tmp_path, "disp-on")
    seen: list[str] = []
    run_coding_loop(s, MEMBERS, _dispatch_policy(), run_turn=_dispatch_probe(seen))
    assert second_id not in seen  # merge-scoped hold: waits for the owner's PR


def test_serialization_off_restores_pre_f159_dispatch(tmp_path: Path) -> None:
    s, second_id = _hot_owner_scenario(tmp_path, "disp-off")
    seen: list[str] = []
    run_coding_loop(s, MEMBERS, _dispatch_policy(hot_file_serialization=False),
                    run_turn=_dispatch_probe(seen))
    assert second_id in seen  # no hot set computed -> dispatched as pre-F159


# --------------------------------------------------------------------------- #
# Defect 3 — the F127 ladder's first rung must be disableable like its siblings
# --------------------------------------------------------------------------- #

_LADDER_MEMBERS = [("m-dev-1", DEV), ("m-dev-2", DEV), ("m-rev", REVIEWER),
                   ("m-pm", PM)]


def _unproductive(member_id: str = "m-dev-1") -> TurnOutcome:
    return TurnOutcome(kind="noop", unproductive=True, member_id=member_id,
                       member_role=DEV, member_route="claude_cli.haiku",
                       reason="turn_tool_markup_only")


def test_worker_unproductive_limit_zero_clamps_like_its_siblings() -> None:
    assert policy_from_dict({"worker_unproductive_limit": 0}).worker_unproductive_limit == 0
    assert policy_from_dict({"model_escalation_limit": 0}).model_escalation_limit == 0
    assert policy_from_dict({"task_reassignment_limit": 0}).task_reassignment_limit == 0
    assert policy_from_dict({"worker_unproductive_limit": -4}).worker_unproductive_limit == 0


def test_worker_unproductive_limit_zero_disables_the_ladder(tmp_path: Path) -> None:
    """0 must mean "never fire", like the two rungs below it. It used to clamp to
    1, so the first rung fired on the very first unusable turn."""
    s = _store(tmp_path, "ladder0")
    task = s.add_task(title="impl X", role=DEV)
    c = LoopCounters()
    act = Assign(member_id="m-dev-1", task_id=task.task_id, role=DEV)
    policy = CodingAutonomyPolicy(worker_unproductive_limit=0)

    for _ in range(5):
        assert _handle_unproductive(s, act, _unproductive(), c, policy,
                                    _LADDER_MEMBERS) is None
    t = next(x for x in s.list_tasks() if x.task_id == task.task_id)
    assert (t._extras or {}).get("excluded_member_ids") in (None, [])
    assert not any(d["choice"] in ("worker_excluded", "task_model_escalated")
                   for d in s.list_decisions())


def test_worker_unproductive_limit_one_still_fires_on_the_first_turn(tmp_path: Path) -> None:
    """The value that USED to be the floor keeps its exact old meaning."""
    s = _store(tmp_path, "ladder1")
    task = s.add_task(title="impl X", role=DEV)
    c = LoopCounters()
    act = Assign(member_id="m-dev-1", task_id=task.task_id, role=DEV)
    _handle_unproductive(s, act, _unproductive(), c,
                         CodingAutonomyPolicy(worker_unproductive_limit=1),
                         _LADDER_MEMBERS)
    assert s.list_decisions()  # a ladder rung fired


# --------------------------------------------------------------------------- #
# Defect 4 — reviewer_repo_read's prose said it follows dev_repo_read
# --------------------------------------------------------------------------- #

def test_reviewer_repo_read_inherits_dev_repo_read_when_absent() -> None:
    """Mirrors `test_spec12_18_prep.py`'s drift lock: the field's comment says the
    two are ONE capability decision, so `dev_repo_read: true` alone must not land
    the capability half-on."""
    p = policy_from_dict({"dev_repo_read": True})
    assert p.dev_repo_read is True
    assert p.reviewer_repo_read is True, (
        "reviewer_repo_read's prose says it defaults to dev_repo_read; an absent "
        "key must carry the dev decision across")


def test_reviewer_repo_read_explicit_value_still_wins() -> None:
    assert policy_from_dict(
        {"dev_repo_read": True, "reviewer_repo_read": False}).reviewer_repo_read is False
    assert policy_from_dict(
        {"dev_repo_read": False, "reviewer_repo_read": True}).reviewer_repo_read is True


def test_reviewer_repo_read_prose_and_code_agree() -> None:
    """Textual drift lock in the shape `test_spec12_18_prep.py` uses: nothing near
    the field may claim an inheritance the loader does not implement."""
    import inspect

    from errorta_council.coding import autonomy

    src = inspect.getsource(autonomy)
    claims_inheritance = "Defaults to `dev_repo_read` deliberately" in src
    if claims_inheritance:
        assert policy_from_dict({"dev_repo_read": True}).reviewer_repo_read is True, (
            "autonomy.py claims reviewer_repo_read defaults to dev_repo_read but "
            "policy_from_dict does not carry it")
    assert (CodingAutonomyPolicy().reviewer_repo_read
            == CodingAutonomyPolicy().dev_repo_read)
