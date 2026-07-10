"""F087-3 fix — the runner routes a turn to the scheduler's CHOSEN member.

Bug: ``_member(role)`` always returned ``members[0]``, so every same-role turn
ran as the first member — one dev did all the work while the other sat idle.
The fix honors ``action.member_id``.
"""
from __future__ import annotations

import json
from pathlib import Path

from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.runner import build_run_turn, members_by_coding_role
from errorta_council.coding.topology import DEV, Assign

MEMBERS = [
    {"id": "m-dev1", "enabled": True, "metadata": {"coding_role": "dev"}},
    {"id": "m-dev2", "enabled": True, "metadata": {"coding_role": "dev"}},
    {"id": "m-rev", "enabled": True, "metadata": {"coding_role": "reviewer"}},
]


def _workspace(project_id: str):
    from errorta_council.coding.workspace import CodingWorkspace
    store = LedgerStore(project_id)
    store.create_project(north_star="n", definition_of_done="d", target="new",
                         repo_path=None)
    ws = CodingWorkspace(project_id, store)
    ws.setup(target="new", repo_path=None)
    return store, ws


def test_assign_routes_to_the_chosen_member(tmp_errorta_home: Path) -> None:
    store, ws = _workspace("md")
    task = store.add_task(title="implement", role="dev")
    seen: dict[str, str] = {}

    def caller(member: dict, prompt: str) -> str:
        seen["member_id"] = str(member.get("id"))
        return json.dumps({
            "schema_version": "coding_turn.v1", "role": "dev",
            "task_id": task.task_id,
            "intent": {"kind": "implement", "summary": "done",
                       "files": [{"path": "a.py", "content": "x = 1\n"}]},
        })

    run_turn = build_run_turn(
        store, ws, members_by_coding_role(MEMBERS), caller, guardrail_enabled=True)
    # The scheduler picked the SECOND dev — the runner must use it, not members[0].
    run_turn(Assign(member_id="m-dev2", task_id=task.task_id, role=DEV), store)
    assert seen["member_id"] == "m-dev2"


def test_unknown_member_id_falls_back_to_first(tmp_errorta_home: Path) -> None:
    store, ws = _workspace("md2")
    task = store.add_task(title="implement", role="dev")
    seen: dict[str, str] = {}

    def caller(member: dict, prompt: str) -> str:
        seen["member_id"] = str(member.get("id"))
        return json.dumps({
            "schema_version": "coding_turn.v1", "role": "dev",
            "task_id": task.task_id,
            "intent": {"kind": "implement", "summary": "done",
                       "files": [{"path": "a.py", "content": "x = 1\n"}]},
        })

    run_turn = build_run_turn(
        store, ws, members_by_coding_role(MEMBERS), caller, guardrail_enabled=True)
    run_turn(Assign(member_id="m-ghost", task_id=task.task_id, role=DEV), store)
    assert seen["member_id"] == "m-dev1"  # graceful fallback to the first dev
