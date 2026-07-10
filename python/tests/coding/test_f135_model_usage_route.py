"""F135 — /coding/projects/{id}/model-usage rollup route."""
from pathlib import Path

import pytest
from fastapi import HTTPException

from errorta_app.routes.coding import get_model_usage
from errorta_council.coding.ledger import LedgerStore


def _project(home: Path, project_id: str = "proj") -> LedgerStore:
    store = LedgerStore(project_id)
    store.create_project(
        north_star="n", definition_of_done="d", target="new", repo_path=None,
    )
    return store


def _assignment(member: str, route: str, tier: str, source: str, *,
                escalation: int = 0) -> dict:
    return {
        "assignment_id": f"ma-{route}", "task_id": "t", "member_id": member,
        "route_id": route, "task_type": "implementation", "difficulty_tier": tier,
        "rationale": "why", "source": source, "assigned_at": "2026-07-01T00:00:00+00:00",
        "escalation_count": escalation, "attempted_route_ids": [],
    }


def test_model_usage_rollup(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    store = _project(tmp_path)
    store.set_run_config(members=[
        {"id": "m-dev-1", "role": "dev", "model_mode": "multi",
         "model_pool": ["claude_cli.haiku", "claude_cli.sonnet"]},
        {"id": "m-review-1", "role": "reviewer",
         "gateway_route_id": "claude_cli.sonnet"},
    ])
    store.add_task(title="scaffold", role="dev", difficulty_tier="light",
                   model_assignment=_assignment("m-dev-1", "claude_cli.haiku", "light", "pm"))
    store.add_task(title="page", role="dev", difficulty_tier="mid",
                   model_assignment=_assignment("m-dev-1", "claude_cli.sonnet", "mid", "selector"))
    store.add_task(title="page2", role="dev", difficulty_tier="mid",
                   model_assignment=_assignment("m-dev-1", "claude_cli.sonnet", "mid", "selector"))

    usage = get_model_usage("proj")["usage"]
    assert [m["member_id"] for m in usage["multi_members"]] == ["m-dev-1"]
    multi = usage["multi_members"][0]
    assert multi["model_mode"] == "multi"
    assert multi["pool"] == ["claude_cli.haiku", "claude_cli.sonnet"]
    by_route = {(a["route_id"], a["source"]): a["count"] for a in multi["assignments"]}
    assert by_route[("claude_cli.haiku", "pm")] == 1
    assert by_route[("claude_cli.sonnet", "selector")] == 2
    assert usage["single_members"] == [
        {"member_id": "m-review-1", "route_id": "claude_cli.sonnet"}]


def test_multi_member_with_zero_assignments_still_appears(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    store = _project(tmp_path)
    store.set_run_config(members=[
        {"id": "m-dev-1", "role": "dev", "model_mode": "multi",
         "model_pool": ["claude_cli.haiku", "claude_cli.sonnet"]},
    ])
    usage = get_model_usage("proj")["usage"]
    assert len(usage["multi_members"]) == 1
    assert usage["multi_members"][0]["assignments"] == []
    assert usage["multi_members"][0]["pool"] == ["claude_cli.haiku", "claude_cli.sonnet"]


def test_escalations_surface(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    store = _project(tmp_path)
    store.set_run_config(members=[
        {"id": "m-dev-1", "role": "dev", "model_mode": "multi",
         "model_pool": ["claude_cli.haiku", "claude_cli.sonnet"]},
    ])
    store.add_task(title="hard", role="dev", difficulty_tier="mid",
                   model_assignment=_assignment("m-dev-1", "claude_cli.sonnet", "mid",
                                                 "escalation", escalation=1))
    multi = get_model_usage("proj")["usage"]["multi_members"][0]
    assert len(multi["escalations"]) == 1
    assert multi["escalations"][0]["escalation_count"] == 1


def test_unknown_project_is_404(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    with pytest.raises(HTTPException) as exc:
        get_model_usage("nope")
    assert exc.value.status_code == 404
