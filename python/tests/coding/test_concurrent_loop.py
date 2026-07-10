"""F087 Slice 3 — concurrent dispatch in run_coding_loop.

``max_parallel_workers > 1`` fans the ready worker turns across a thread pool;
``<= 1`` keeps the exact single-action (decide_next) path. These lock the new
behaviours: real concurrency, strict budget under parallel dispatch, failure
isolation, the merge-stays-serial path, and the mid-run downgrade to sequential.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from errorta_council.coding.autonomy import (
    BUDGET_EXHAUSTED,
    CADENCE_OFF,
    DEFINITION_OF_DONE,
    CodingAutonomyPolicy,
    LoopCounters,
    TurnOutcome,
    run_coding_loop,
)
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.topology import DEV, PM, REVIEWER, TESTER, Merge, Plan

# 2 devs + reviewer + tester + PM — the parallel room shape.
MEMBERS = [
    ("m-pm", PM), ("m-dev1", DEV), ("m-dev2", DEV),
    ("m-rev", REVIEWER), ("m-test", TESTER),
]


def _store(tmp_path: Path) -> LedgerStore:
    s = LedgerStore("conc-loop", root=tmp_path)
    s.create_project(north_star="n", definition_of_done="d", target="new",
                     repo_path=None)
    return s


class _ProbeTeam:
    """Task-flow fake that records peak concurrent worker turns. The PM plants
    all dev tasks up front so a batch can fan out across both devs at once."""

    def __init__(self, dev_tasks: int = 2, sleep: float = 0.03) -> None:
        self.to_add = dev_tasks
        self.planted = False
        self.sleep = sleep
        self._lock = threading.Lock()
        self.cur = 0
        self.peak = 0

    def run_turn(self, action, ledger) -> TurnOutcome:
        if isinstance(action, Plan):
            if not self.planted:
                for i in range(self.to_add):
                    ledger.add_task(title=f"impl {i}", role=DEV)
                self.planted = True
                return TurnOutcome(kind="planned", made_progress=True)
            open_tasks = [t for t in ledger.list_tasks()
                          if t.state not in ("done", "dropped")]
            if not open_tasks:
                return TurnOutcome(kind="project_done")
            return TurnOutcome(kind="planned", made_progress=False)

        with self._lock:
            self.cur += 1
            self.peak = max(self.peak, self.cur)
        time.sleep(self.sleep)
        with self._lock:
            self.cur -= 1

        task = next(t for t in ledger.list_tasks() if t.task_id == action.task_id)
        if action.role == DEV:
            return TurnOutcome(kind="task_done", task=task)
        if action.role == REVIEWER:
            return TurnOutcome(kind="review_done", task=task, approved=True,
                               reviewed_task_id=task.task_id,
                               reviewed_title=task.title)
        if action.role == TESTER:
            return TurnOutcome(kind="task_done", task=task)
        return TurnOutcome(kind="noop")


def test_concurrent_dispatch_runs_workers_in_parallel(tmp_path: Path) -> None:
    s = _store(tmp_path)
    team = _ProbeTeam(dev_tasks=2)
    res = run_coding_loop(
        s, MEMBERS,
        CodingAutonomyPolicy(checkpoint_cadence=CADENCE_OFF, max_parallel_workers=3),
        run_turn=team.run_turn,
    )
    assert res.stop_reason == DEFINITION_OF_DONE
    assert team.peak >= 2  # both devs actually ran at the same time


def test_explicit_single_worker_runs_one_at_a_time(tmp_path: Path) -> None:
    s = _store(tmp_path)
    team = _ProbeTeam(dev_tasks=2)
    # An explicit cap of 1 forces the sequential decide_next path.
    res = run_coding_loop(
        s, MEMBERS,
        CodingAutonomyPolicy(checkpoint_cadence=CADENCE_OFF, max_parallel_workers=1),
        run_turn=team.run_turn,
    )
    assert res.stop_reason == DEFINITION_OF_DONE
    assert team.peak == 1  # never more than one turn in flight


def test_auto_parallelism_from_team_size(tmp_path: Path) -> None:
    s = _store(tmp_path)
    team = _ProbeTeam(dev_tasks=2)
    # Default policy (max_parallel_workers=None) auto-sizes to the team's worker
    # members (2 dev + reviewer + tester), so a multi-member room parallelizes
    # WITHOUT any manual config — the bug fix.
    res = run_coding_loop(
        s, MEMBERS, CodingAutonomyPolicy(checkpoint_cadence=CADENCE_OFF),
        run_turn=team.run_turn,
    )
    assert res.stop_reason == DEFINITION_OF_DONE
    assert team.peak >= 2  # both devs ran at once without setting the flag


def test_pipeline_runs_dev_dev_reviewer_and_spreads_members(tmp_path: Path) -> None:
    """The bug: only 2 in flight, and once a dev finished the reviewer ran while
    the devs idled. With the continuous pipeline a dev that finishes picks up the
    next task WHILE the reviewer reviews — so 2 devs + 1 reviewer = 3 in flight,
    and work spreads across BOTH dev members (not just members[0])."""
    s = _store(tmp_path)
    import threading
    import time

    lock = threading.Lock()
    cur = {DEV: 0, REVIEWER: 0, TESTER: 0}
    saw_dev_dev_reviewer = [False]
    dev_members: set[str] = set()
    planted = [False]

    def run_turn(action, ledger) -> TurnOutcome:
        if isinstance(action, Plan):
            if not planted[0]:
                for i in range(6):
                    ledger.add_task(title=f"impl {i}", role=DEV)
                planted[0] = True
                return TurnOutcome(kind="planned", made_progress=True)
            open_tasks = [t for t in ledger.list_tasks()
                          if t.state not in ("done", "dropped")]
            return (TurnOutcome(kind="project_done") if not open_tasks
                    else TurnOutcome(kind="planned", made_progress=False))
        with lock:
            cur[action.role] += 1
            if action.role == DEV:
                dev_members.add(action.member_id)
            if cur[DEV] >= 2 and cur[REVIEWER] >= 1:
                saw_dev_dev_reviewer[0] = True
        time.sleep(0.05)
        with lock:
            cur[action.role] -= 1
        task = next(t for t in ledger.list_tasks() if t.task_id == action.task_id)
        if action.role == DEV:
            return TurnOutcome(kind="task_done", task=task)
        if action.role == REVIEWER:
            return TurnOutcome(kind="review_done", task=task, approved=True,
                               reviewed_task_id=task.task_id, reviewed_title=task.title)
        return TurnOutcome(kind="task_done", task=task)

    res = run_coding_loop(
        s, MEMBERS, CodingAutonomyPolicy(checkpoint_cadence=CADENCE_OFF),
        run_turn=run_turn,
    )
    assert res.stop_reason == DEFINITION_OF_DONE
    assert saw_dev_dev_reviewer[0], "never had 2 devs + reviewer in flight at once"
    assert dev_members == {"m-dev1", "m-dev2"}, f"work didn't spread: {dev_members}"


def test_merge_never_runs_concurrent_with_a_worker(tmp_path: Path) -> None:
    """Integration stays serial: a Merge mutates master + revalidates other PRs'
    worktrees, so it must never overlap a worker turn. A PR is made mergeable
    mid-run (while other dev assigns are in flight) to exercise the defer path."""
    s = _store(tmp_path)
    lock = threading.Lock()
    st = {"assigns": 0, "merging": False, "violation": False, "merged": False}
    pr_made = [False]
    planted = [False]

    def run_turn(action, ledger) -> TurnOutcome:
        if isinstance(action, Plan):
            if not planted[0]:
                for i in range(4):
                    ledger.add_task(title=f"impl {i}", role=DEV)
                planted[0] = True
                return TurnOutcome(kind="planned", made_progress=True)
            open_tasks = [t for t in ledger.list_tasks()
                          if t.state not in ("done", "dropped")]
            return (TurnOutcome(kind="project_done")
                    if not open_tasks and st["merged"]
                    else TurnOutcome(kind="planned", made_progress=False))
        if isinstance(action, Merge):
            with lock:
                st["merging"] = True
                if st["assigns"] > 0:
                    st["violation"] = True
            time.sleep(0.05)
            with lock:
                st["merging"] = False
            ledger.update_pr(action.pr_id, status="merged")
            st["merged"] = True
            return TurnOutcome(kind="pr_merged", model_calls=0)
        # worker assign (dev / reviewer / tester)
        with lock:
            st["assigns"] += 1
            if st["merging"]:
                st["violation"] = True
        time.sleep(0.05)
        with lock:
            st["assigns"] -= 1
        task = next(t for t in ledger.list_tasks() if t.task_id == action.task_id)
        # First dev completion makes a PR mergeable WHILE the other dev still runs.
        if action.role == DEV and not pr_made[0]:
            pr_made[0] = True
            pr = ledger.record_pr(task_id=task.task_id, branch="b", head="h",
                                  dev_member=action.member_id)
            ledger.update_pr(pr["pr_id"], status="mergeable")
        if action.role == REVIEWER:
            return TurnOutcome(kind="review_done", task=task, approved=True,
                               reviewed_task_id=task.task_id, reviewed_title=task.title)
        return TurnOutcome(kind="task_done", task=task)

    res = run_coding_loop(
        s, MEMBERS,
        CodingAutonomyPolicy(checkpoint_cadence=CADENCE_OFF, max_iterations=120),
        run_turn=run_turn,
    )
    assert st["merged"], "the merge never ran"
    assert not st["violation"], "a Merge overlapped a worker turn"
    assert res.stop_reason in (DEFINITION_OF_DONE, "no_progress")


def test_concurrent_strict_model_call_budget(tmp_path: Path) -> None:
    s = _store(tmp_path)

    def run_turn(action, ledger) -> TurnOutcome:
        if isinstance(action, Plan):
            for i in range(10):
                ledger.add_task(title=f"impl {i}", role=DEV)
            return TurnOutcome(kind="planned", made_progress=True)
        task = next(t for t in ledger.list_tasks() if t.task_id == action.task_id)
        return TurnOutcome(kind="task_done", task=task)

    res = run_coding_loop(
        s, MEMBERS,
        CodingAutonomyPolicy(checkpoint_cadence=CADENCE_OFF,
                             max_parallel_workers=4, max_model_calls=3),
        run_turn=run_turn,
    )
    assert res.stop_reason == BUDGET_EXHAUSTED
    assert res.counters.model_calls <= 3  # parallel dispatch never overshoots


def test_concurrent_failure_isolation_requeues_and_self_heals(tmp_path: Path) -> None:
    s = _store(tmp_path)
    done: list[str] = []
    lock = threading.Lock()
    attempts = {"boom": 0}

    def run_turn(action, ledger) -> TurnOutcome:
        if isinstance(action, Plan):
            open_tasks = [t for t in ledger.list_tasks()
                          if t.state not in ("done", "dropped")]
            if not open_tasks and not getattr(run_turn, "planted", False):
                ledger.add_task(title="boom", role=DEV)
                ledger.add_task(title="ok", role=DEV)
                run_turn.planted = True  # type: ignore[attr-defined]
                return TurnOutcome(kind="planned", made_progress=True)
            if not open_tasks:
                return TurnOutcome(kind="project_done")
            return TurnOutcome(kind="planned", made_progress=False)
        task = next(t for t in ledger.list_tasks() if t.task_id == action.task_id)
        if task.title == "boom":
            with lock:
                attempts["boom"] += 1
                first = attempts["boom"] == 1
            if first:
                raise RuntimeError("worker exploded")  # transient failure
        with lock:
            done.append(task.title)
        return TurnOutcome(kind="task_done", task=task)

    # A crashing worker must not tear down the batch: the sibling still finishes,
    # the crashed task is requeued + audited, and the retry self-heals to done.
    res = run_coding_loop(
        s, MEMBERS,
        CodingAutonomyPolicy(checkpoint_cadence=CADENCE_OFF, max_parallel_workers=2),
        run_turn=run_turn,
    )
    assert "ok" in done                       # the healthy sibling completed
    assert attempts["boom"] >= 2              # crashed once, then retried
    assert res.stop_reason == DEFINITION_OF_DONE
    assert any(d.get("choice") == "worker_turn_requeued"
               for d in s.list_decisions())


def test_concurrent_downgrade_to_sequential_midrun(tmp_path: Path) -> None:
    s = _store(tmp_path)
    team = _ProbeTeam(dev_tasks=2)
    calls = {"n": 0}

    def provider() -> CodingAutonomyPolicy:
        # First read starts the concurrent loop; subsequent reads downgrade it to
        # the sequential path, which must finish the run cleanly.
        calls["n"] += 1
        workers = 3 if calls["n"] == 1 else 1
        return CodingAutonomyPolicy(checkpoint_cadence=CADENCE_OFF,
                                    max_parallel_workers=workers)

    res = run_coding_loop(
        s, MEMBERS, CodingAutonomyPolicy(max_parallel_workers=3),
        run_turn=team.run_turn, policy_provider=provider,
    )
    assert res.stop_reason == DEFINITION_OF_DONE


def test_policy_serializes_and_clamps_max_parallel_workers(tmp_path: Path) -> None:
    from errorta_council.coding.autonomy import (
        load_policy,
        policy_from_dict,
        policy_to_dict,
        save_policy,
    )
    # Round-trips through the policy dict (the GET/PUT API surface).
    p = CodingAutonomyPolicy(max_parallel_workers=4)
    assert policy_to_dict(p)["max_parallel_workers"] == 4
    assert policy_from_dict(policy_to_dict(p)).max_parallel_workers == 4
    # Clamped to >= 1 (0 / negative would disable dispatch entirely).
    assert policy_from_dict({"max_parallel_workers": 0}).max_parallel_workers == 1
    assert policy_from_dict({"max_parallel_workers": -3}).max_parallel_workers == 1
    # Unset -> None (AUTO: the loop sizes parallelism to the team's worker count).
    assert policy_from_dict({}).max_parallel_workers is None
    assert policy_from_dict({"max_parallel_workers": None}).max_parallel_workers is None
    # Persists + reloads from the project ledger.
    s = _store(tmp_path)
    save_policy(s, CodingAutonomyPolicy(max_parallel_workers=3))
    assert load_policy(LedgerStore("conc-loop", root=tmp_path)).max_parallel_workers == 3


def test_concurrent_resumes_from_counters(tmp_path: Path) -> None:
    s = _store(tmp_path)
    team = _ProbeTeam(dev_tasks=2)
    seeded = LoopCounters(iterations=1, model_calls=1)
    res = run_coding_loop(
        s, MEMBERS,
        CodingAutonomyPolicy(checkpoint_cadence=CADENCE_OFF, max_parallel_workers=2),
        run_turn=team.run_turn, counters=seeded,
    )
    assert res.stop_reason == DEFINITION_OF_DONE
    assert res.counters.iterations > 1  # carried the seeded counter forward
