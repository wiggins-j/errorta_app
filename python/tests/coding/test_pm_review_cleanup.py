"""F087-18 — PM dev-only planning, richer reviewer context, branch cleanup."""
from __future__ import annotations

import json
from pathlib import Path

from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.runner import (
    _filter_generated_from_diff, _review_project_context, build_run_turn,
    members_by_coding_role,
)
from errorta_council.coding.topology import Plan
from errorta_council.coding.workspace import CodingWorkspace

MEMBERS = [
    {"id": "m-pm", "enabled": True, "metadata": {"coding_role": "pm"}},
    {"id": "m-dev", "enabled": True, "metadata": {"coding_role": "dev"}},
    {"id": "m-rev", "enabled": True, "metadata": {"coding_role": "reviewer"}},
    {"id": "m-test", "enabled": True, "metadata": {"coding_role": "tester"}},
]


# --- #1 PM plans dev-only ---------------------------------------------------


def test_pm_tasks_are_coerced_to_dev(tmp_errorta_home: Path) -> None:
    store = LedgerStore("pmdev")
    store.create_project(north_star="x", definition_of_done="d", target="new", repo_path=None)

    def caller(member, prompt):
        # the PM (mistakenly) asks for reviewer/tester tasks
        return json.dumps({"schema_version": "coding_turn.v1", "role": "pm",
            "intent": {"kind": "plan", "done": False, "tasks": [
                {"title": "build", "role": "dev"},
                {"title": "please review", "role": "reviewer"},
                {"title": "please test", "role": "tester"},
            ]}})

    rt = build_run_turn(store, None, members_by_coding_role(MEMBERS), caller,
                        guardrail_enabled=True)
    rt(Plan(member_id="m-pm"), store)
    # every planned task is a DEV task — review/test/merge are auto-driven
    assert {t.role for t in store.list_tasks()} == {"dev"}


def test_pm_prompt_says_dev_only(tmp_errorta_home: Path) -> None:
    store = LedgerStore("pmprompt")
    store.create_project(north_star="x", definition_of_done="d", target="new", repo_path=None)
    seen = []

    def caller(member, prompt):
        seen.append(prompt)
        return json.dumps({"schema_version": "coding_turn.v1", "role": "pm",
            "intent": {"kind": "plan", "done": False,
                       "tasks": [{"title": "build", "role": "dev"}]}})

    rt = build_run_turn(store, None, members_by_coding_role(MEMBERS), caller,
                        guardrail_enabled=True)
    rt(Plan(member_id="m-pm"), store)
    assert "DEV tasks only" in seen[0]
    assert "do NOT create reviewer/tester" in seen[0]


# --- #3 reviewer context + #5 diff filter -----------------------------------


def test_review_context_includes_dod_blockers_and_files(tmp_errorta_home: Path) -> None:
    store = LedgerStore("revctx")
    store.create_project(north_star="Build a calculator",
                         definition_of_done="add+sub+mul+div all tested",
                         target="new", repo_path=None)
    blocked = store.add_task(title="blocked thing", role="dev")
    store.update_task(blocked.task_id, state="blocked")
    ws = CodingWorkspace("revctx", store)
    ws.setup(target="new", repo_path=None)
    ws.start_task_branch("t1")
    ws.write_file("calculator.py", "def add(a,b): return a+b\n", task_id="t1")
    pr = store.record_pr(task_id="t1", branch=ws.task_branch("t1"),
                         head=ws.head(), dev_member="m-dev")
    ctx = _review_project_context(store, ws, pr)
    assert "add+sub+mul+div" in ctx          # Definition of Done
    assert "blocked thing" in ctx            # active blockers
    assert "calculator.py" in ctx            # full API surface (post-merge files)


def test_filter_generated_from_diff_drops_pyc() -> None:
    diff = (
        "diff --git a/calc.py b/calc.py\n"
        "--- a/calc.py\n+++ b/calc.py\n@@ -0,0 +1 @@\n+x = 1\n"
        "diff --git a/__pycache__/calc.cpython-314.pyc b/__pycache__/calc.cpython-314.pyc\n"
        "Binary files differ\n"
    )
    out = _filter_generated_from_diff(diff)
    assert "calc.py" in out
    assert "__pycache__" not in out and ".pyc" not in out


# --- #6 branch cleanup ------------------------------------------------------


def test_merged_branch_is_deleted(tmp_errorta_home: Path) -> None:
    store = LedgerStore("cleanup")
    store.create_project(north_star="x", definition_of_done="d", target="new", repo_path=None)
    ws = CodingWorkspace("cleanup", store)
    ws.setup(target="new", repo_path=None)
    ws.start_task_branch("t1")
    ws.write_file("a.py", "x = 1\n", task_id="t1")
    assert "task-t1" in ws.list_branches()

    res = ws.merge_pr(ws.task_branch("t1"))
    assert res["merged"] is True
    from errorta_council.coding.runner import _prune_dead_branches
    pr = store.record_pr(task_id="t1", branch="task-t1", head=res["head"], dev_member="m")
    store.update_pr(pr["pr_id"], status="merged")
    _prune_dead_branches(store, ws, just_merged="task-t1")
    assert "task-t1" not in ws.list_branches()  # reclaimed
    assert "master" in ws.list_branches()       # integration branch kept


def test_delete_branch_never_drops_master(tmp_errorta_home: Path) -> None:
    store = LedgerStore("nodropmaster")
    store.create_project(north_star="x", definition_of_done="d", target="new", repo_path=None)
    ws = CodingWorkspace("nodropmaster", store)
    ws.setup(target="new", repo_path=None)
    assert ws.delete_branch("master") is False
    assert "master" in ws.list_branches()
