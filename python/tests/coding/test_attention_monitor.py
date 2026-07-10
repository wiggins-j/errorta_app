"""F117-03 — Progress Monitor producer: stuck governed runs raise a Problem."""
from __future__ import annotations

from pathlib import Path

import pytest

from errorta_council.coding import attention, autonomy
from errorta_council.coding.autonomy import (
    CADENCE_OFF,
    HARD_BLOCKER,
    NO_PROGRESS,
    CodingAutonomyPolicy,
    TurnOutcome,
    run_coding_loop,
)
from errorta_council.coding.governance import GovernanceState, GovernanceStore
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.topology import DEV, PM, REVIEWER, TESTER, Plan

MEMBERS = [("m-pm", PM), ("m-dev", DEV), ("m-rev", REVIEWER), ("m-test", TESTER)]


def _governed_store(tmp_path: Path, phase="brainstorming") -> LedgerStore:
    s = LedgerStore("mon", root=tmp_path)
    s.create_project(north_star="n", definition_of_done="d", target="new", repo_path=None)
    GovernanceStore.for_ledger(s).save_state(GovernanceState(mode="light", phase=phase))
    return s


# --- store-level helper -----------------------------------------------------
def test_raise_monitor_problem_canned_and_dedup(tmp_path: Path):
    s = LedgerStore("mp", root=tmp_path)
    sig = attention.raise_monitor_problem(
        "mp", stage="drafting_spec", detector="hard_blocker", reason="needs key", store=s)
    assert sig is not None
    assert sig.kind == "problem" and sig.blocking is True and sig.source == "monitor"
    assert sig.pm_evaluation and len(sig.suggestions) == 3
    # dedup: same detector+stage while open → None
    assert attention.raise_monitor_problem(
        "mp", stage="drafting_spec", detector="hard_blocker", reason="x", store=s) is None
    # a different detector is not deduped
    assert attention.raise_monitor_problem(
        "mp", stage="drafting_spec", detector="no_progress", reason="x", store=s) is not None


# --- the producer integration the loop calls --------------------------------
def test_maybe_raise_monitor_governed_raises_and_dedups(tmp_path: Path):
    s = _governed_store(tmp_path)
    autonomy._maybe_raise_monitor(s, "hard_blocker", "boom")
    open_problems = attention.list_open("mon", store=s)
    assert any(p.source == "monitor" and p.stage == "brainstorming"
               and p.title == "Stuck: hard_blocker" for p in open_problems)
    # idempotent on a repeat trip
    autonomy._maybe_raise_monitor(s, "hard_blocker", "boom")
    assert len([p for p in attention.list_open("mon", store=s)
                if p.title == "Stuck: hard_blocker"]) == 1


def test_maybe_raise_monitor_skips_ungoverned(tmp_path: Path):
    s = LedgerStore("off", root=tmp_path)
    s.create_project(north_star="n", definition_of_done="d", target="new", repo_path=None)
    # mode defaults to "off" → no stage to key on → no signal raised
    autonomy._maybe_raise_monitor(s, "hard_blocker", "boom")
    assert attention.list_open("off", store=s) == []


def test_maybe_raise_monitor_block_off_auto_resolves_and_shows(tmp_path: Path):
    s = _governed_store(tmp_path)
    GovernanceStore.for_ledger(s).save_state(
        GovernanceState(mode="light", phase="brainstorming", block_on_problems=False)
    )
    autonomy._maybe_raise_monitor(s, "hard_blocker", "boom")

    assert attention.list_open("mon", store=s) == []
    signals = attention.list_all("mon", store=s)
    assert len(signals) == 1
    assert signals[0].state == "auto_resolved"
    task_id = signals[0].resolution["created_task_id"]
    task = next(t for t in s.list_tasks() if t.task_id == task_id)
    assert task.role == "pm"
    assert task._extras["source_signal_id"] == signals[0].id


def test_maybe_raise_monitor_never_raises(tmp_path: Path):
    # A broken ledger must not propagate out of the producer (best-effort).
    autonomy._maybe_raise_monitor(object(), "hard_blocker", "boom")  # no exception


# --- loop wiring: the producer fires at the real stop sites ------------------
def _spy(monkeypatch):
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(autonomy, "_maybe_raise_monitor",
                        lambda ledger, detector, reason: calls.append((detector, reason)))
    return calls


@pytest.mark.parametrize("max_parallel_workers", [1, 2])
def test_loop_fires_producer_on_hard_blocker(
    tmp_path: Path, monkeypatch, max_parallel_workers: int,
):
    calls = _spy(monkeypatch)
    s = LedgerStore("p", root=tmp_path)
    s.create_project(north_star="n", definition_of_done="d", target="new", repo_path=None)

    def run_turn(action, ledger) -> TurnOutcome:
        if isinstance(action, Plan):
            ledger.add_task(title="needs creds", role=DEV)
            return TurnOutcome(kind="planned", made_progress=True)
        task = next(t for t in ledger.list_tasks() if t.task_id == action.task_id)
        return TurnOutcome(kind="task_blocked", task=task,
                           reason="needs an API key", hard_blocker=True)

    res = run_coding_loop(
        s, MEMBERS,
        CodingAutonomyPolicy(
            checkpoint_cadence=CADENCE_OFF,
            max_parallel_workers=max_parallel_workers,
        ),
        run_turn=run_turn,
    )
    assert res.stop_reason == HARD_BLOCKER
    assert any(d == "hard_blocker" for d, _ in calls)


@pytest.mark.parametrize("max_parallel_workers", [1, 2])
def test_loop_fires_producer_on_no_progress(
    tmp_path: Path, monkeypatch, max_parallel_workers: int,
):
    calls = _spy(monkeypatch)
    s = LedgerStore("p", root=tmp_path)
    s.create_project(north_star="n", definition_of_done="d", target="new", repo_path=None)

    def run_turn(action, ledger) -> TurnOutcome:
        return TurnOutcome(kind="planned", made_progress=False)

    res = run_coding_loop(
        s, MEMBERS,
        CodingAutonomyPolicy(
            checkpoint_cadence=CADENCE_OFF,
            pm_idle_limit=2,
            max_parallel_workers=max_parallel_workers,
        ),
        run_turn=run_turn)
    assert res.stop_reason == NO_PROGRESS
    assert any(d == "no_progress" for d, _ in calls)


def test_healthy_run_raises_no_monitor_problem(tmp_path: Path):
    s = _governed_store(tmp_path)

    def run_turn(action, ledger) -> TurnOutcome:
        return TurnOutcome(kind="project_done")

    res = run_coding_loop(
        s, MEMBERS,
        CodingAutonomyPolicy(checkpoint_cadence=CADENCE_OFF, max_parallel_workers=1),
        run_turn=run_turn,
    )
    assert res.stop_reason == "definition_of_done"
    assert attention.list_all("mon", store=s) == []
