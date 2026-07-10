"""F128 slices 2-3 — the runner refuses a PM done=true claim while open work
remains, and the PM prompt tells the PM exactly what's open."""
from __future__ import annotations

import json
from pathlib import Path

from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.runner import (
    _pm_prompt,
    build_run_turn,
    members_by_coding_role,
)
from errorta_council.coding.topology import DEV, Plan

MEMBERS = [
    {"id": "m-pm", "enabled": True, "metadata": {"coding_role": "pm"}},
    {"id": "m-dev", "enabled": True, "metadata": {"coding_role": "dev"}},
    {"id": "m-rev", "enabled": True, "metadata": {"coding_role": "reviewer"}},
]


def _store(name: str) -> LedgerStore:
    s = LedgerStore(name)
    s.create_project(north_star="n", definition_of_done="d", target="new", repo_path=None)
    return s


def _pm_done() -> str:
    return json.dumps({
        "schema_version": "coding_turn.v1", "role": "pm",
        "intent": {"kind": "plan", "done": True, "completion_summary": "all done"},
    })


def _run_pm_plan(store: LedgerStore, name: str):
    rt = build_run_turn(store, _ws(name, store),
                        members_by_coding_role(MEMBERS),
                        lambda member, prompt: _pm_done(), guardrail_enabled=True)
    return rt(Plan(member_id="m-pm"), store)


def _ws(name: str, store: LedgerStore):
    from errorta_council.coding.workspace import CodingWorkspace
    ws = CodingWorkspace(name, store)
    ws.setup(target="new", repo_path=None)
    return ws


def test_done_refused_while_a_todo_task_is_open(tmp_errorta_home: Path) -> None:
    store = _store("refuse-todo")
    store.add_task(title="Main Loop Integration", role=DEV)

    outcome = _run_pm_plan(store, "refuse-todo")

    assert outcome.kind == "completion_refused"
    assert store.get_project().status != "done"
    assert store.get_project().completion_summary == ""  # never persisted
    assert any(d["choice"] == "pm_completion_refused" for d in store.list_decisions())


def test_done_refused_while_a_blocked_task_is_open(tmp_errorta_home: Path) -> None:
    # The exact ARK-Login-Sentinel case: a human-required blocked task remains.
    store = _store("refuse-blocked")
    task = store.add_task(title="resolve conflict", role=DEV)
    store.update_task(task.task_id, state="blocked")

    outcome = _run_pm_plan(store, "refuse-blocked")

    assert outcome.kind == "completion_refused"
    assert store.get_project().status != "done"


def test_done_accepted_when_backlog_is_drained(tmp_errorta_home: Path) -> None:
    store = _store("accept-drained")
    task = store.add_task(title="impl", role=DEV)
    store.update_task(task.task_id, state="done")

    outcome = _run_pm_plan(store, "accept-drained")

    assert outcome.kind == "project_done"
    assert store.get_project().completion_summary == "all done"


def test_done_accepted_after_obsolete_task_cancelled(tmp_errorta_home: Path) -> None:
    # D3: an existing mutation path drops the obsolete task, then done passes.
    store = _store("accept-cancel")
    task = store.add_task(title="obsolete", role=DEV)

    refused = _run_pm_plan(store, "accept-cancel")
    assert refused.kind == "completion_refused"

    store.update_task(task.task_id, state="dropped")
    accepted = _run_pm_plan(store, "accept-cancel")
    assert accepted.kind == "project_done"


def test_pm_prompt_lists_open_items_and_forbids_done(tmp_errorta_home: Path) -> None:
    store = _store("prompt-gate")
    store.add_task(title="Join Detector Component", role=DEV)

    prompt = _pm_prompt(store)

    assert "may NOT declare the project done" in prompt
    assert "Join Detector Component" in prompt
    assert "current PM plan schema has no cancel intent" in prompt


def test_pm_prompt_has_no_gate_when_backlog_drained(tmp_errorta_home: Path) -> None:
    store = _store("prompt-clean")
    prompt = _pm_prompt(store)
    assert "may NOT declare the project done" not in prompt
