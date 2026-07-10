from __future__ import annotations

import json
from pathlib import Path

from errorta_council.coding.ledger import LedgerStore
from errorta_project_grounding.pm_working_memory import (
    SCHEMA_VERSION,
    build_pm_working_memory_snapshot,
    render_pm_working_memory_document,
    summarize_pm_working_memory,
)


def _store(tmp_path: Path) -> LedgerStore:
    store = LedgerStore("pmwm-snapshot", root=tmp_path)
    store.create_project(
        north_star="Ship the calculator",
        definition_of_done="add/subtract work and tests pass",
        target="new",
        repo_path=None,
    )
    return store


def test_snapshot_contains_goal_focus_prs_decisions_and_episodes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task = store.add_task(title="Implement add", role="dev", assignee_member_id="m-2")
    store.update_task(task.task_id, state="doing", pr_id="pr-1")
    store.record_pr(task_id=task.task_id, branch="task/add", head="abc123", dev_member="m-2")
    store.record_decision(
        title="Use int arithmetic",
        context="pm_decision",
        choice="pm_decision",
        rationale="The MVP only needs integer examples.",
        related_task_ids=[task.task_id],
    )
    store.record_episode(
        title="merged task/bootstrap",
        summary="Created the initial project scaffold",
        head="def456",
        related_task_ids=[task.task_id],
    )

    snapshot = build_pm_working_memory_snapshot(store)

    assert snapshot["schema_version"] == SCHEMA_VERSION
    assert snapshot["project"]["north_star"] == "Ship the calculator"
    assert snapshot["focus"]["current_focus"] == "Implement add"
    assert snapshot["focus"]["open_task_count"] == 1
    assert snapshot["integration"]["pr_state"]["counts"]["open"] == 1
    assert snapshot["decisions"][0]["title"] == "Use int arithmetic"
    assert snapshot["integration"]["recent_episodes"][0]["title"] == "merged task/bootstrap"


def test_snapshot_caps_lists_and_renders_stable_json(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for i in range(20):
        store.add_task(title=f"Task {i}", role="dev")
        store.record_decision(
            title=f"Decision {i}",
            context="pm_decision",
            choice="pm_decision",
            rationale="r",
        )

    snapshot = build_pm_working_memory_snapshot(store)
    text = render_pm_working_memory_document(snapshot)

    assert len(snapshot["focus"]["next_tasks"]) == 12
    assert len(snapshot["decisions"]) == 8
    assert "PM working memory" in text
    raw = text.split("JSON:\n", 1)[1]
    parsed = json.loads(raw)
    assert parsed["schema_version"] == SCHEMA_VERSION


def test_summary_is_short_and_actionable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task = store.add_task(title="Current task", role="dev")
    store.update_task(task.task_id, state="doing")

    summary = summarize_pm_working_memory(build_pm_working_memory_snapshot(store))

    assert "Focus: Current task" in summary
    assert "Open tasks: 1" in summary
    assert len(summary) <= 240
