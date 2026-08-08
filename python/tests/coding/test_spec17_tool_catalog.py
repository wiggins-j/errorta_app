from __future__ import annotations

import json

import pytest

from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.runner import build_run_turn, members_by_coding_role
from errorta_council.coding.topology import DEV, PM, REVIEWER, TESTER, Assign
from errorta_council.coding.turn_controller import (
    allowed_tools_for_role,
    tool_catalog_text,
)
from errorta_council.coding.workspace import CodingWorkspace


@pytest.mark.parametrize("role", [PM, DEV, REVIEWER, TESTER])
@pytest.mark.parametrize("repo_read", [False, True])
@pytest.mark.parametrize("gate", [False, True])
def test_catalog_matches_live_capabilities(role: str, repo_read: bool, gate: bool) -> None:
    rendered = tool_catalog_text(role, repo_read=repo_read, gate=gate)
    tools = ", ".join(allowed_tools_for_role(role)) or "none"

    assert f"Available Coding Mode tools for role {role}: {tools}." in rendered
    assert "No execute, run, or shell tool exists" in rendered
    if repo_read:
        assert all(name in rendered for name in ("Read", "Grep", "Glob"))
        assert "used directly" in rendered
        assert "do not emit them as errorta tool calls" in rendered
    else:
        assert "context_request" in rendered


def test_catalog_capability_arguments_are_required() -> None:
    with pytest.raises(TypeError):
        tool_catalog_text(DEV)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        tool_catalog_text(DEV, repo_read=True)  # type: ignore[call-arg]


def test_unknown_tool_failure_reaches_next_prompt_and_clears(
        tmp_errorta_home) -> None:
    store = LedgerStore("spec17-carry")
    store.create_project(
        north_star="n", definition_of_done="d", target="new", repo_path=None)
    task = store.add_task(title="implement", role=DEV)
    workspace = CodingWorkspace("spec17-carry", store)
    workspace.setup(target="new", repo_path=None)
    members = [
        {"id": "m-dev", "enabled": True, "metadata": {"coding_role": DEV}},
    ]
    prompts: list[str] = []

    def caller(member, prompt):
        prompts.append(prompt)
        tool_call = (
            {"tool": "read_files", "args": {"path": "app.py"}}
            if len(prompts) == 1
            else {"tool": "code_write",
                  "args": {"path": "app.py", "content": "print('ok')\n"}}
        )
        return json.dumps({
            "schema_version": "coding_turn.v1",
            "role": DEV,
            "task_id": task.task_id,
            "intent": {
                "kind": "tool_plan",
                "task_type": "implementation",
                "tool_calls": [tool_call],
            },
        })

    run_turn = build_run_turn(
        store, workspace, members_by_coding_role(members), caller,
        guardrail_enabled=True)
    action = Assign(member_id="m-dev", task_id=task.task_id, role=DEV)

    first = run_turn(action, store)
    persisted = next(t for t in store.list_tasks() if t.task_id == task.task_id)
    assert first.unproductive is True
    assert "tool_not_allowed" in persisted.last_tool_failure
    assert "context_request" in persisted.last_tool_failure

    run_turn(action, store)
    persisted = next(t for t in store.list_tasks() if t.task_id == task.task_id)
    assert "Previous tool failure" in prompts[1]
    assert "tool_not_allowed" in prompts[1]
    assert persisted.last_tool_failure == ""
