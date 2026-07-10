from pathlib import Path
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.topology import Assign, Plan, PM, DEV, REVIEWER, TESTER
from errorta_council.coding.autonomy import (
    CodingAutonomyPolicy, TurnOutcome, run_coding_loop, LoopCounters,
    CADENCE_OFF, CADENCE_PER_MILESTONE, CADENCE_EVERY_N,
    DEFINITION_OF_DONE, BUDGET_EXHAUSTED, CHECKPOINT, HARD_BLOCKER, CANCELLED,
    NO_PROGRESS,
)

MEMBERS = [("m-pm", PM), ("m-dev", DEV), ("m-rev", REVIEWER), ("m-test", TESTER)]


def _store(tmp_path: Path) -> LedgerStore:
    s = LedgerStore("p", root=tmp_path)
    s.create_project(north_star="n", definition_of_done="d", target="new", repo_path=None)
    return s


class FakeTeam:
    """Simulates one dev unit flowing dev->review->test->done."""
    def __init__(self, dev_tasks: int = 1) -> None:
        self.to_add = dev_tasks
        self.added = 0

    def run_turn(self, action, ledger) -> TurnOutcome:
        if isinstance(action, Plan):
            if self.added < self.to_add:
                ledger.add_task(title=f"impl {self.added}", role=DEV)
                self.added += 1
                return TurnOutcome(kind="planned", made_progress=True)
            open_tasks = [t for t in ledger.list_tasks()
                          if t.state not in ("done", "dropped")]
            if not open_tasks:
                return TurnOutcome(kind="project_done")
            return TurnOutcome(kind="planned", made_progress=False)
        task = next(t for t in ledger.list_tasks() if t.task_id == action.task_id)
        if action.role == DEV:
            return TurnOutcome(kind="task_done", task=task)
        if action.role == REVIEWER:
            return TurnOutcome(kind="review_done", task=task, approved=True,
                               reviewed_task_id=task.task_id, reviewed_title=task.title)
        if action.role == TESTER:
            return TurnOutcome(kind="task_done", task=task)
        return TurnOutcome(kind="noop")


def test_loop_runs_to_definition_of_done(tmp_path: Path) -> None:
    s = _store(tmp_path)
    res = run_coding_loop(s, MEMBERS, CodingAutonomyPolicy(checkpoint_cadence=CADENCE_OFF),
                          run_turn=FakeTeam().run_turn)
    assert res.stop_reason == DEFINITION_OF_DONE
    assert s.get_project().status == "done"
    # the full dev->review->test pipeline ran (review + validate tasks exist done)
    assert any(t.title.startswith("review:") for t in s.list_tasks())
    assert any(t.title.startswith("validate:") for t in s.list_tasks())


def test_loop_stops_on_budget_cap(tmp_path: Path) -> None:
    s = _store(tmp_path)
    res = run_coding_loop(s, MEMBERS,
                          CodingAutonomyPolicy(max_iterations=2, checkpoint_cadence=CADENCE_OFF),
                          run_turn=FakeTeam().run_turn)
    assert res.stop_reason == BUDGET_EXHAUSTED
    assert res.counters.iterations == 2


def test_loop_pauses_at_milestone_checkpoint_then_resumes(tmp_path: Path) -> None:
    s = _store(tmp_path)
    team = FakeTeam()
    res = run_coding_loop(s, MEMBERS,
                          CodingAutonomyPolicy(checkpoint_cadence=CADENCE_PER_MILESTONE),
                          run_turn=team.run_turn)
    assert res.stop_reason == CHECKPOINT          # paused at the tester milestone
    # resume continues to completion (same counters)
    res2 = run_coding_loop(s, MEMBERS,
                           CodingAutonomyPolicy(checkpoint_cadence=CADENCE_PER_MILESTONE),
                           run_turn=team.run_turn, counters=res.counters)
    assert res2.stop_reason == DEFINITION_OF_DONE


def test_loop_every_n_tasks_checkpoint(tmp_path: Path) -> None:
    s = _store(tmp_path)
    res = run_coding_loop(s, MEMBERS,
                          CodingAutonomyPolicy(checkpoint_cadence=CADENCE_EVERY_N, checkpoint_n=1),
                          run_turn=FakeTeam().run_turn)
    assert res.stop_reason == CHECKPOINT          # first completed task triggers it


def test_loop_stops_on_hard_blocker(tmp_path: Path) -> None:
    s = _store(tmp_path)

    def run_turn(action, ledger) -> TurnOutcome:
        if isinstance(action, Plan):
            ledger.add_task(title="needs creds", role=DEV)
            return TurnOutcome(kind="planned", made_progress=True)
        task = next(t for t in ledger.list_tasks() if t.task_id == action.task_id)
        return TurnOutcome(kind="task_blocked", task=task,
                           reason="needs an API key", hard_blocker=True)

    res = run_coding_loop(s, MEMBERS, CodingAutonomyPolicy(checkpoint_cadence=CADENCE_OFF),
                          run_turn=run_turn)
    assert res.stop_reason == HARD_BLOCKER
    assert s.list_tasks(state="blocked")


def test_loop_cancel(tmp_path: Path) -> None:
    s = _store(tmp_path)
    res = run_coding_loop(s, MEMBERS, CodingAutonomyPolicy(),
                          run_turn=FakeTeam().run_turn, should_cancel=lambda: True)
    assert res.stop_reason == CANCELLED


def test_loop_stops_on_pm_no_progress(tmp_path: Path) -> None:
    s = _store(tmp_path)

    def run_turn(action, ledger) -> TurnOutcome:
        # PM never adds work -> no progress
        return TurnOutcome(kind="planned", made_progress=False)

    res = run_coding_loop(s, MEMBERS,
                          CodingAutonomyPolicy(checkpoint_cadence=CADENCE_OFF, pm_idle_limit=2),
                          run_turn=run_turn)
    assert res.stop_reason == NO_PROGRESS


def test_policy_persistence_and_provider(tmp_path: Path) -> None:
    from errorta_council.coding.autonomy import (
        load_policy, save_policy, CodingAutonomyPolicy, CADENCE_EVERY_N,
    )
    s = _store(tmp_path)
    assert load_policy(s).checkpoint_cadence == CADENCE_PER_MILESTONE  # default
    save_policy(s, CodingAutonomyPolicy(checkpoint_cadence=CADENCE_EVERY_N, checkpoint_n=3))
    again = load_policy(LedgerStore("p", root=tmp_path))
    assert again.checkpoint_cadence == CADENCE_EVERY_N and again.checkpoint_n == 3


def test_loop_reads_policy_from_provider_each_iteration(tmp_path: Path) -> None:
    from errorta_council.coding.autonomy import load_policy, save_policy, CodingAutonomyPolicy
    s = _store(tmp_path)
    save_policy(s, CodingAutonomyPolicy(checkpoint_cadence=CADENCE_OFF))
    res = run_coding_loop(s, MEMBERS, CodingAutonomyPolicy(),
                          run_turn=FakeTeam().run_turn,
                          policy_provider=lambda: load_policy(s))
    assert res.stop_reason == DEFINITION_OF_DONE
