"""F087-19 — PR/test state + episodes in orientation; reconcile; lifecycle; no-op PR."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.orientation import build_orientation_packet
from errorta_council.coding.workspace import CodingWorkspace


def _store(pid: str) -> LedgerStore:
    s = LedgerStore(pid)
    s.create_project(north_star="n", definition_of_done="d", target="new", repo_path=None)
    return s


# --- #1 compact PR/test state in the orientation packet ---------------------


def test_orientation_has_pr_state_and_episodes(tmp_errorta_home: Path) -> None:
    s = _store("ori1")
    pr = s.record_pr(task_id="t1", branch="task-t1", head="aaaa1111", dev_member="m")
    s.update_pr(pr["pr_id"], status="merged", head="bbbb2222")

    class _S:
        command_ids = ["unit"]; results: list = []; unknown_ids: list = []; passed = True
    s.record_test_run(_S(), task_id="t1", head="bbbb2222")
    s.record_episode(title="merged task-t1", summary="add() merged", head="bbbb2222")
    open_pr = s.record_pr(task_id="t2", branch="task-t2", head="cccc3333", dev_member="m")
    s.update_pr(open_pr["pr_id"], status="changes_requested")

    pkt = build_orientation_packet(s, token_budget=10_000).to_dict()
    assert pkt["pr_state"]["counts"]["merged"] == 1
    assert pkt["pr_state"]["counts"]["changes_requested"] == 1
    assert any(o["branch"] == "task-t2" for o in pkt["pr_state"]["open_prs"])
    assert pkt["pr_state"]["latest_green_head"] == "bbbb2222"
    assert pkt["recent_episodes"][-1]["title"] == "merged task-t1"


def test_orientation_lists_conflicted_prs_as_actionable(tmp_errorta_home: Path) -> None:
    s = _store("ori-conflict")
    task = s.add_task(title="pricing", role="dev")
    pr = s.record_pr(task_id=task.task_id, branch="task-pricing", head="h",
                     dev_member="m")
    s.update_pr(pr["pr_id"], status="conflict",
                conflicts=["pricing.py", "test_pricing.py"], resolve_attempts=1)

    pkt = build_orientation_packet(s, token_budget=10_000).to_dict()

    conflicted = pkt["pr_state"]["conflicted_prs"]
    assert conflicted == [{
        "pr_id": pr["pr_id"],
        "branch": "task-pricing",
        "task_id": task.task_id,
        "conflicts": ["pricing.py", "test_pricing.py"],
        "resolve_attempts": 1,
        "action": "PM should redispatch a resolve task or block after retry cap",
    }]


def test_episodes_survive_trim(tmp_errorta_home: Path) -> None:
    # the freshest episode + PR state are durable even under a tiny budget
    s = _store("ori2")
    for i in range(4):
        s.record_episode(title=f"ep{i}", summary="x" * 200, head="h")
    pkt = build_orientation_packet(s, token_budget=80)
    assert len(pkt.recent_episodes) >= 1
    assert pkt.pr_state is not None


# --- #2 stale reconcile + #3 no-op PR skip (via the real runner) ------------


def _ws(pid: str) -> CodingWorkspace:
    w = CodingWorkspace(pid, LedgerStore(pid))
    w.setup(target="new", repo_path=None)
    return w


def test_reconcile_abandons_superseded_pr_and_drops_corrective_task(tmp_errorta_home: Path) -> None:
    from errorta_council.coding.runner import _reconcile_stale
    s = _store("rec")
    ws = _ws("rec")
    # master already has calc.py with add+sub (a later PR merged the whole thing)
    ws.start_task_branch("whole")
    ws.write_file("calc.py", "def add(a,b): return a+b\ndef sub(a,b): return a-b\n", task_id="whole")
    ws.merge_pr(ws.task_branch("whole"))
    # an OLD branch that adds nothing master doesn't already have -> empty diff
    branch = ws.start_task_branch("old", base="master")  # == "task-old"
    pr = s.record_pr(task_id="t-old", branch=branch, head=ws.head(), dev_member="m")
    s.update_pr(pr["pr_id"], status="changes_requested")
    s.add_task(title=f"fix tests: {branch}", role="dev")

    _reconcile_stale(s, ws)
    assert s.get_pr(pr["pr_id"])["status"] == "abandoned"
    titles = {t.title: t.state for t in s.list_tasks()}
    assert titles[f"fix tests: {branch}"] == "dropped"
    assert any(d["choice"] == "pr_superseded" for d in s.list_decisions())


def test_noop_dev_turn_is_unproductive_not_done(tmp_errorta_home: Path) -> None:
    # F139 WS-C supersedes F087-19 #3's auto-close: a dev turn whose branch has no
    # net change vs master is NOT marked `done` "already satisfied" (that let the
    # reddit Navigation-rewritten-100× loop close tasks without producing
    # anything). It is unproductive — re-queued to feed the F127 escalate-up
    # ladder — and records a `superseded_on_master` decision for the PM to confirm.
    from errorta_council.coding.runner import build_run_turn, members_by_coding_role
    from errorta_council.coding.topology import Assign, DEV
    s = _store("noop")
    ws = _ws("noop")
    task = s.add_task(title="impl nothing", role=DEV)

    def caller(member, prompt):
        # a "documentation" task that writes nothing new -> empty branch
        tid = __import__("re").search(r"developer for task id '([^']+)'", prompt).group(1)
        return json.dumps({"schema_version": "coding_turn.v1", "role": "dev",
            "task_id": tid, "intent": {"kind": "tool_plan", "task_type": "documentation",
            "tool_calls": []}})

    rt = build_run_turn(s, ws, members_by_coding_role([
        {"id": "m-dev", "enabled": True, "metadata": {"coding_role": "dev"}}]),
        caller, guardrail_enabled=True)
    out = rt(Assign(member_id="m-dev", task_id=task.task_id, role=DEV), s)
    assert out.kind == "noop" and out.unproductive is True
    assert out.reason == "no_net_change"
    assert s.list_prs() == []                          # no PR opened
    assert not any(t.role == "reviewer" for t in s.list_tasks())  # no review chain
    assert any(d["choice"] == "superseded_on_master" for d in s.list_decisions())
    # the task is re-queued (NOT closed done)
    assert {t.task_id: t.state for t in s.list_tasks()}[task.task_id] == "todo"


# --- #4 direct CodingRunner.run() sets lifecycle ----------------------------


def test_direct_runner_sets_lifecycle(tmp_errorta_home: Path) -> None:
    from errorta_council.coding.runner import CodingRunner
    from errorta_council.coding.autonomy import CodingAutonomyPolicy, CADENCE_OFF
    s = _store("life")

    # a PM that immediately declares done -> the loop completes quickly
    def caller(member, prompt):
        return json.dumps({"schema_version": "coding_turn.v1", "role": "pm",
            "intent": {"kind": "plan", "done": True, "completion_summary": "nothing to do"}})

    members = [{"id": "m-pm", "enabled": True, "metadata": {"coding_role": "pm"}}]
    assert s.get_run_state()["status"] == "idle"
    runner = CodingRunner("life", members, caller, guardrail_enabled=True)
    runner.run(CodingAutonomyPolicy(checkpoint_cadence=CADENCE_OFF, max_iterations=5))
    # lifecycle was written by run() itself (no route involved)
    assert s.get_run_state()["status"] == "stopped"
    assert s.get_run_state()["ended_at"] is not None
