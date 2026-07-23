"""Spec 09 — dependency deadlock & task-state hygiene.

The observed failure: 130 todo tasks, none dispatchable. ``next_task`` returned
``None`` for every role forever, so the scheduler handed the PM a plan turn every
iteration and churned. Four mechanisms conspired, each covered here:

1. a dep in ``dropped`` could never be satisfied (``ledger.next_task``);
2. dropping a task never re-pointed its dependents (``runner`` PM-assist);
3. a ``noop`` turn stranded its task in ``doing`` forever (``autonomy``);
4. same-batch path ownership chained planned siblings into a serial line
   (``runner._materialize_pm_tasks``), amplifying one wedge into the backlog.
"""
from __future__ import annotations

import json
from pathlib import Path

from errorta_council.coding.autonomy import (
    CADENCE_OFF,
    CodingAutonomyPolicy,
    TurnOutcome,
    _requeue_stranded,
    run_coding_loop,
)
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.runner import (
    _repoint_dropped_dependents,
    build_run_turn,
    members_by_coding_role,
)
from errorta_council.coding.topology import (
    DEV,
    PM,
    REVIEWER,
    TESTER,
    Assign,
    Plan,
    PMAssist,
    decide_next,
)

LOOP_MEMBERS = [("m-pm", PM), ("m-dev", DEV)]
MEMBER_DICTS = [
    {"id": "m-pm", "enabled": True, "metadata": {"coding_role": PM}},
    {"id": "m-dev-1", "enabled": True, "metadata": {"coding_role": DEV}},
]


def _store(tmp_path: Path, name: str = "pdead") -> LedgerStore:
    s = LedgerStore(name, root=tmp_path)
    s.create_project(north_star="n", definition_of_done="d", target="new",
                     repo_path=None)
    return s


def _pm_env(tasks: list[dict]) -> str:
    return json.dumps({
        "schema_version": "coding_turn.v1", "role": "pm",
        "intent": {"kind": "plan", "done": False, "tasks": tasks},
    })


def _reload(store: LedgerStore, task_id: str):
    return next(t for t in store.list_tasks() if t.task_id == task_id)


def _sequential_policy(**kw) -> CodingAutonomyPolicy:
    """Force the single-worker loop so the test drives `_run_sequential_loop`."""
    return CodingAutonomyPolicy(
        max_parallel_workers=1, checkpoint_cadence=CADENCE_OFF, **kw)


# --- 1. `dropped` deps are satisfied; `blocked` deps are not -----------------

def test_task_depending_on_dropped_task_is_dispatchable(tmp_path: Path) -> None:
    s = _store(tmp_path)
    head = s.add_task(title="head", role=DEV)
    dependent = s.add_task(title="dependent", role=DEV, depends_on=[head.task_id])

    s.update_task(head.task_id, state="doing")
    assert s.next_task(DEV) is None  # in-flight prerequisite still blocks

    s.update_task(head.task_id, state="dropped")
    got = s.next_task(DEV)
    assert got is not None and got.task_id == dependent.task_id


def test_next_tasks_agrees_with_next_task_on_dropped_deps(tmp_path: Path) -> None:
    """Both readiness predicates must use the same satisfied-dep rule, or the
    concurrent loop and the sequential loop disagree about what is dispatchable."""
    s = _store(tmp_path)
    head = s.add_task(title="head", role=DEV)
    a = s.add_task(title="a", role=DEV, depends_on=[head.task_id])
    b = s.add_task(title="b", role=DEV, depends_on=[head.task_id])

    s.update_task(head.task_id, state="dropped")
    assert {t.task_id for t in s.next_tasks(DEV, 5)} == {a.task_id, b.task_id}


def test_task_depending_on_blocked_task_is_not_dispatchable(tmp_path: Path) -> None:
    """Unchanged behaviour: `blocked` is expected to become unblocked, so it is
    NOT a satisfied dep — admitting it would run work before its prerequisite."""
    s = _store(tmp_path)
    head = s.add_task(title="head", role=DEV)
    s.add_task(title="dependent", role=DEV, depends_on=[head.task_id])

    s.update_task(head.task_id, state="blocked")
    assert s.next_task(DEV) is None
    assert s.next_tasks(DEV, 5) == []


def test_done_dep_still_satisfies(tmp_path: Path) -> None:
    s = _store(tmp_path)
    head = s.add_task(title="head", role=DEV)
    dependent = s.add_task(title="dependent", role=DEV, depends_on=[head.task_id])

    s.update_task(head.task_id, state="done")
    assert s.next_task(DEV).task_id == dependent.task_id


# --- 2. dropping a task re-points its dependents -----------------------------

def _arm_pm_assist(store: LedgerStore, task_id: str) -> None:
    store.update_task(
        task_id,
        excluded_member_ids=["m-dev-1"],
        excluded_member_routes={"m-dev-1": "claude_cli.haiku"},
        pm_assist_pending=True,
        pm_assist_limit=1,
    )


def test_drop_repoints_dependents_onto_superseding_tasks(tmp_path: Path) -> None:
    s = _store(tmp_path)
    stuck = s.add_task(title="impl X", role=DEV, detail="Change src/x.py")
    dependent = s.add_task(title="ship X", role=DEV, depends_on=[stuck.task_id])
    _arm_pm_assist(s, stuck.task_id)

    def caller(_member, _prompt):
        return _pm_env([
            {"title": "Define X contract", "role": "dev",
             "detail": "Acceptance: contract documented.", "depends_on": []},
            {"title": "Implement X against the contract", "role": "dev",
             "detail": "Acceptance: X implemented.", "depends_on": []},
        ])

    run_turn = build_run_turn(s, None, members_by_coding_role(MEMBER_DICTS),
                              caller, guardrail_enabled=True)
    outcome = run_turn(PMAssist("m-pm", stuck.task_id), s)

    assert outcome.kind == "planned"
    assert _reload(s, stuck.task_id).state == "dropped"
    children = [t for t in s.list_tasks() if t.parent_task_id == stuck.task_id]
    assert len(children) == 2

    got = _reload(s, dependent.task_id)
    assert got.depends_on == [c.task_id for c in children]
    assert dependent.task_id not in got.depends_on  # no self-cycle
    assert stuck.task_id not in got.depends_on      # the dead edge is gone


def test_drop_never_creates_a_cycle_when_repointing(tmp_path: Path) -> None:
    """The replacement can inherit a path dep on the very task being re-pointed
    (both name ``src/ship.py``). Re-pointing would close a cycle, so the edge is
    dropped instead — the dependent is freed, the graph stays acyclic."""
    s = _store(tmp_path)
    stuck = s.add_task(title="impl X", role=DEV, detail="Change src/x.py")
    dependent = s.add_task(title="ship X", role=DEV, detail="Change src/ship.py",
                           depends_on=[stuck.task_id])
    _arm_pm_assist(s, stuck.task_id)

    def caller(_member, _prompt):
        return _pm_env([
            {"title": "Rework shipping", "role": "dev",
             "detail": "Acceptance: shipping reworked. File: src/ship.py",
             "depends_on": []},
        ])

    run_turn = build_run_turn(s, None, members_by_coding_role(MEMBER_DICTS),
                              caller, guardrail_enabled=True)
    run_turn(PMAssist("m-pm", stuck.task_id), s)

    child = next(t for t in s.list_tasks() if t.parent_task_id == stuck.task_id)
    # the replacement inherited the path dep on the dependent...
    assert dependent.task_id in child.depends_on
    # ...so the dependent must NOT be re-pointed onto it (that is the cycle).
    got = _reload(s, dependent.task_id)
    assert child.task_id not in got.depends_on
    assert got.depends_on == []
    # and the dependent is dispatchable again
    assert s.next_task(DEV).task_id == dependent.task_id


def test_repoint_removes_the_edge_when_there_is_no_replacement(tmp_path: Path) -> None:
    s = _store(tmp_path)
    stuck = s.add_task(title="impl X", role=DEV)
    other = s.add_task(title="other", role=DEV)
    dependent = s.add_task(title="dependent", role=DEV,
                           depends_on=[other.task_id, stuck.task_id])
    s.update_task(stuck.task_id, state="dropped")

    assert _repoint_dropped_dependents(s, stuck.task_id, []) == [dependent.task_id]
    assert _reload(s, dependent.task_id).depends_on == [other.task_id]


# --- 3. stale-`doing` reaper -------------------------------------------------

def test_sequential_loop_returns_a_noop_task_to_todo(tmp_path: Path) -> None:
    """`rec.assign` marks the task `doing` BEFORE the turn runs. A `noop` outcome
    used to leave it there forever: invisible to `next_task`, yet blocking every
    dependent. It must come back to the queue."""
    s = _store(tmp_path)
    task = s.add_task(title="impl X", role=DEV)
    dispatched: list[str] = []

    def run_turn(action, ledger):
        if isinstance(action, Plan):
            return TurnOutcome(kind="planned", made_progress=False)
        dispatched.append(action.task_id)
        # mid-turn the task really is `doing` (the reconciler assigned it).
        assert _reload(ledger, action.task_id).state == "doing"
        return TurnOutcome(kind="noop")

    run_coding_loop(s, LOOP_MEMBERS, _sequential_policy(max_iterations=3),
                    run_turn=run_turn)

    got = _reload(s, task.task_id)
    assert got.state == "todo"
    assert got.assignee_member_id is None
    # re-dispatchable rather than stranded: it was handed out more than once.
    assert dispatched.count(task.task_id) >= 2
    assert any(d["choice"] == "stale_doing_requeued" for d in s.list_decisions())


def test_noop_task_no_longer_strands_its_dependents(tmp_path: Path) -> None:
    s = _store(tmp_path)
    head = s.add_task(title="head", role=DEV)
    s.add_task(title="dependent", role=DEV, depends_on=[head.task_id])

    def run_turn(action, ledger):
        if isinstance(action, Plan):
            return TurnOutcome(kind="planned", made_progress=False)
        return TurnOutcome(kind="noop")

    run_coding_loop(s, LOOP_MEMBERS, _sequential_policy(max_iterations=2),
                    run_turn=run_turn)

    # the head is back in the queue, so the graph is still live: decide_next has
    # real work to hand out instead of falling through to a PM plan turn.
    assert _reload(s, head.task_id).state == "todo"
    assert s.next_task(DEV).task_id == head.task_id


def test_reaper_leaves_a_task_the_turn_already_moved_alone(tmp_path: Path) -> None:
    s = _store(tmp_path)
    task = s.add_task(title="impl X", role=DEV)

    def run_turn(action, ledger):
        if isinstance(action, Plan):
            return TurnOutcome(kind="planned", made_progress=False)
        # a `noop` path that already parked the task itself (e.g. no PR to review)
        ledger.update_task(action.task_id, state="done")
        return TurnOutcome(kind="noop")

    run_coding_loop(s, LOOP_MEMBERS, _sequential_policy(max_iterations=2),
                    run_turn=run_turn)

    assert _reload(s, task.task_id).state == "done"


def test_reaper_skips_a_task_owned_by_a_different_member(tmp_path: Path) -> None:
    """Liveness belt: if the ledger says somebody else now holds the row, the
    finished turn must not yank it back to the queue."""
    s = _store(tmp_path)
    task = s.add_task(title="impl X", role=DEV)
    s.update_task(task.task_id, state="doing", assignee_member_id="m-dev-2")

    action = Assign(member_id="m-dev-1", task_id=task.task_id, role=DEV)
    assert _requeue_stranded(s, action, TurnOutcome(kind="noop")) is False
    got = _reload(s, task.task_id)
    assert got.state == "doing" and got.assignee_member_id == "m-dev-2"


def test_reaper_ignores_non_assign_actions(tmp_path: Path) -> None:
    s = _store(tmp_path)
    task = s.add_task(title="impl X", role=DEV)
    s.update_task(task.task_id, state="doing", assignee_member_id="m-dev-1")

    assert _requeue_stranded(
        s, PMAssist("m-pm", task.task_id), TurnOutcome(kind="noop")) is False
    assert _reload(s, task.task_id).state == "doing"


def test_concurrent_loop_does_not_reap_an_in_flight_task(tmp_path: Path) -> None:
    """The reaper only ever runs for the action whose turn just returned, so a
    sibling still executing in the pool keeps its `doing` row."""
    import threading
    import time

    s = _store(tmp_path)
    slow = s.add_task(title="slow", role=DEV)
    fast = s.add_task(title="fast", role=REVIEWER)
    members = [("m-pm", PM), ("m-dev", DEV), ("m-rev", REVIEWER)]
    seen_states: list[str] = []
    released = threading.Event()

    def run_turn(action, ledger):
        if isinstance(action, Plan):
            released.set()
            return TurnOutcome(kind="planned", made_progress=False)
        task = _reload(ledger, action.task_id)
        if task.task_id == slow.task_id:
            released.wait(timeout=5)
            time.sleep(0.05)  # let the main thread apply the fast turn's noop
            # our row must still be `doing` — never reaped from under a live turn
            seen_states.append(_reload(ledger, slow.task_id).state)
            return TurnOutcome(kind="task_done", task=task)
        released.set()
        return TurnOutcome(kind="noop")

    run_coding_loop(
        s, members,
        CodingAutonomyPolicy(max_parallel_workers=2, checkpoint_cadence=CADENCE_OFF,
                             max_iterations=6),
        run_turn=run_turn)

    assert seen_states and all(state == "doing" for state in seen_states)
    assert _reload(s, fast.task_id).state == "todo"  # the noop turn WAS requeued


# --- 4. the path-owner chaining amplifier ------------------------------------

def test_batch_naming_the_same_file_does_not_chain_serially(tmp_path: Path) -> None:
    """Three planned tasks all naming ``pricing.py`` must hang off ONE owner, not
    form 1 -> 2 -> 3. A serial line is what turned a single wedged head into a
    130-task backlog wedge."""
    s = _store(tmp_path)

    def caller(_member, _prompt):
        return _pm_env([
            {"title": "update pricing", "role": "dev", "detail": "Change pricing.py"},
            {"title": "cover pricing", "role": "dev", "detail": "Test pricing.py"},
            {"title": "document pricing", "role": "dev", "detail": "Doc pricing.py"},
        ])

    run_turn = build_run_turn(s, None, members_by_coding_role(MEMBER_DICTS),
                              caller, guardrail_enabled=True)
    run_turn(Plan(member_id="m-pm"), s)

    tasks = s.list_tasks(role=DEV)
    assert len(tasks) == 3
    head, second, third = tasks
    assert head.depends_on == []
    assert second.depends_on == [head.task_id]
    # the amplifier: this used to be [second.task_id] — a serial line.
    assert third.depends_on == [head.task_id]
    assert second.task_id not in third.depends_on

    # clearing the single head frees BOTH followers at once (max chain depth 1).
    s.update_task(head.task_id, state="done")
    assert {t.task_id for t in s.next_tasks(DEV, 5)} == {second.task_id, third.task_id}


def test_dropping_the_batch_head_frees_the_whole_batch(tmp_path: Path) -> None:
    """The two fixes compose: a bounded (depth-1) fan-out plus `dropped`-as-
    satisfied means a wedged head can never hold the batch hostage."""
    s = _store(tmp_path)

    def caller(_member, _prompt):
        return _pm_env([
            {"title": "a", "role": "dev", "detail": "Change pricing.py"},
            {"title": "b", "role": "dev", "detail": "Change pricing.py"},
            {"title": "c", "role": "dev", "detail": "Change pricing.py"},
        ])

    run_turn = build_run_turn(s, None, members_by_coding_role(MEMBER_DICTS),
                              caller, guardrail_enabled=True)
    run_turn(Plan(member_id="m-pm"), s)

    tasks = s.list_tasks(role=DEV)
    s.update_task(tasks[0].task_id, state="dropped")
    ready = {t.task_id for t in s.next_tasks(DEV, 5)}
    assert ready == {tasks[1].task_id, tasks[2].task_id}


def test_pm_declared_dependencies_still_serialize_a_batch(tmp_path: Path) -> None:
    """Bounding the *path* heuristic must not weaken an EXPLICIT `depends_on`."""
    s = _store(tmp_path)

    def caller(_member, _prompt):
        return _pm_env([
            {"title": "one", "role": "dev", "detail": "Change pricing.py"},
            {"title": "two", "role": "dev", "detail": "Change pricing.py",
             "depends_on": ["one"]},
            {"title": "three", "role": "dev", "detail": "Change pricing.py",
             "depends_on": ["two"]},
        ])

    run_turn = build_run_turn(s, None, members_by_coding_role(MEMBER_DICTS),
                              caller, guardrail_enabled=True)
    run_turn(Plan(member_id="m-pm"), s)

    one, two, three = s.list_tasks(role=DEV)
    assert one.depends_on == []
    assert two.depends_on == [one.task_id]
    # explicit chain preserved (plus the depth-1 path dep on the batch head)
    assert three.depends_on[0] == two.task_id
    assert set(three.depends_on) == {two.task_id, one.task_id}
    assert s.next_task(DEV).task_id == one.task_id
    s.update_task(one.task_id, state="done")
    assert s.next_task(DEV).task_id == two.task_id  # three still waits on two


# --- regression: a genuine dependency chain still executes in order ----------

def test_genuine_dependency_chain_executes_in_order(tmp_path: Path) -> None:
    s = _store(tmp_path)
    a = s.add_task(title="a", role=DEV)
    b = s.add_task(title="b", role=DEV, depends_on=[a.task_id])
    c = s.add_task(title="c", role=DEV, depends_on=[b.task_id])

    # ledger level
    assert s.next_task(DEV).task_id == a.task_id
    s.update_task(a.task_id, state="done")
    assert s.next_task(DEV).task_id == b.task_id
    s.update_task(b.task_id, state="done")
    assert s.next_task(DEV).task_id == c.task_id

    # loop level (fresh chain, driven end to end)
    s2 = _store(tmp_path / "two", name="pchain")
    x = s2.add_task(title="x", role=DEV)
    y = s2.add_task(title="y", role=DEV, depends_on=[x.task_id])
    z = s2.add_task(title="z", role=DEV, depends_on=[y.task_id])
    order: list[str] = []

    def run_turn(action, ledger):
        if isinstance(action, Plan):
            return TurnOutcome(kind="planned", made_progress=False)
        task = _reload(ledger, action.task_id)
        order.append(task.title)
        ledger.update_task(task.task_id, state="done")
        return TurnOutcome(kind="task_done", task=task)

    run_coding_loop(s2, LOOP_MEMBERS, _sequential_policy(max_iterations=12),
                    run_turn=run_turn)

    assert order[:3] == ["x", "y", "z"]
    assert [_reload(s2, t.task_id).state for t in (x, y, z)] == ["done"] * 3


def test_decide_next_dispatches_work_freed_by_a_drop(tmp_path: Path) -> None:
    """End of the wedge: with the head dropped, the scheduler hands out real work
    instead of falling through to yet another PM plan turn."""
    s = _store(tmp_path)
    head = s.add_task(title="head", role=DEV)
    dependent = s.add_task(title="dependent", role=DEV, depends_on=[head.task_id])
    s.update_task(head.task_id, state="dropped")

    action = decide_next(s, [("m-pm", PM), ("m-dev", DEV), ("m-test", TESTER)])
    assert getattr(action, "task_id", None) == dependent.task_id
