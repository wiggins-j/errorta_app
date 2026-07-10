"""F143 — /coding/projects/{id}/usage-summary token rollup route."""
from pathlib import Path

import pytest
from fastapi import HTTPException

from errorta_app.routes.coding import get_usage_summary
from errorta_council.coding.ledger import LedgerStore


def _project(project_id: str = "proj") -> LedgerStore:
    store = LedgerStore(project_id)
    store.create_project(north_star="n", definition_of_done="d",
                         target="new", repo_path=None)
    return store


def test_usage_summary_rollup(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    store = _project()
    store.record_turn(role="dev", member_id="m-dev-1", task_id="t1",
                      prompt="p", response="r", outcome="applied",
                      model_assignment={"route_id": "claude_cli.sonnet"},
                      input_tokens=100, output_tokens=40, measured=True)
    store.record_turn(role="reviewer", member_id="m-rev-1", task_id="t1",
                      prompt="p", response="r", outcome="applied",
                      model_assignment={"route_id": "claude_cli.haiku"},
                      input_tokens=30, output_tokens=8, measured=True)
    store.record_turn(role="dev", member_id="m-dev-1", task_id="t2",
                      prompt="p", response="r", outcome="applied",
                      measured=False)  # unreported

    usage = get_usage_summary("proj")["usage"]
    assert usage["total"]["input"] == 130 and usage["total"]["output"] == 48
    assert usage["total"]["turns"] == 3 and usage["total"]["unreported_turns"] == 1
    assert usage["by_member"]["m-dev-1"]["input"] == 100
    assert usage["by_route"]["claude_cli.sonnet"]["output"] == 40
    assert usage["by_route"]["claude_cli.haiku"]["input"] == 30


def test_usage_summary_empty_project(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    _project()
    usage = get_usage_summary("proj")["usage"]
    assert usage["total"]["input"] == 0 and usage["total"]["turns"] == 0
    assert usage["by_member"] == {} and usage["by_route"] == {}


def test_usage_summary_unknown_project_is_404(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    with pytest.raises(HTTPException) as exc:
        get_usage_summary("nope")
    assert exc.value.status_code == 404
