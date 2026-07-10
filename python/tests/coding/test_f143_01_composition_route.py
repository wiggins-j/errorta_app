"""F143-01 Slice D — usage-summary extended shape + per-turn composition endpoint."""
from pathlib import Path

import pytest
from fastapi import HTTPException

from errorta_app.routes.coding import get_turn_composition, get_usage_summary
from errorta_council.coding.ledger import LedgerStore


def _project(project_id: str = "proj") -> LedgerStore:
    store = LedgerStore(project_id)
    store.create_project(north_star="n", definition_of_done="d",
                         target="new", repo_path=None)
    return store


# --- usage-summary extended shape ---------------------------------------------

def test_usage_summary_extended_shape(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    store = _project()
    # a measured DEV turn
    store.record_turn(role="dev", member_id="m-dev", task_id="t1",
                      prompt="p", response="r", outcome="applied",
                      route_id="claude_cli.sonnet",
                      input_tokens=100, output_tokens=40,
                      measured=True, measured_input=100, measured_output=40,
                      provenance="measured")
    # a dark (estimated) DEV turn — its spend must land in the headline
    store.record_turn(role="dev", member_id="m-dev", task_id="t1",
                      prompt="p", response="r", outcome="applied",
                      route_id="claude_cli.sonnet",
                      input_tokens=300, output_tokens=60,
                      measured=False, estimated_input=300, estimated_output=60,
                      provenance="estimated")

    usage = get_usage_summary("proj")["usage"]
    total = usage["total"]
    # genuine effective headline = measured + estimated
    assert total["input"] == 400 and total["output"] == 100
    assert total["measured_input"] == 100 and total["measured_output"] == 40
    assert total["estimated_input"] == 300 and total["estimated_output"] == 60
    # provenance counts
    assert total["measured_turns"] == 1 and total["estimated_turns"] == 1
    assert total["turns"] == 2
    # coverage share of headline tokens: (100+40)/(400+100)=140/500=28%
    assert total["coverage"]["measured_pct"] == 28
    assert total["coverage"]["estimated_pct"] == 72
    # by_role present
    assert "by_role" in usage and usage["by_role"]["dev"]["input"] == 400


def test_usage_summary_unknown_project_is_404(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    with pytest.raises(HTTPException) as exc:
        get_usage_summary("nope")
    assert exc.value.status_code == 404


# --- composition endpoint ------------------------------------------------------

def test_composition_returns_empty_categories_with_cli_overhead(
        tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    store = _project()
    task = store.add_task(title="do it", role="dev")
    rec = store.record_turn(
        role="dev", member_id="m-dev", task_id=task.task_id,
        prompt="p", response="r", outcome="applied",
        route_id="claude_cli.sonnet",
        input_tokens=5000, output_tokens=200,
        measured=True, measured_input=5000, measured_output=200,
        estimated_input=1200, estimated_output=180,
        cli_overhead_tokens=3800, provenance="measured")
    turn_id = rec["turn_id"]

    out = get_turn_composition("proj", task.task_id, turn_id)
    # categories empty this slice (Slice F populates them), but the block is shaped.
    assert out["composition"] == {"sent_total": 0, "categories": []}
    # cli_overhead read straight off the turn's usage block
    assert out["cli_overhead_tokens"] == 3800
    # CLI members get a Layer-2 caveat note referencing the overhead
    assert out["note"] and "3800" in out["note"]


def test_composition_non_cli_turn_has_no_overhead_note(
        tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    store = _project()
    task = store.add_task(title="do it", role="dev")
    rec = store.record_turn(
        role="dev", member_id="m-dev", task_id=task.task_id,
        prompt="p", response="r", outcome="applied",
        route_id="anthropic.sonnet",
        input_tokens=100, output_tokens=40,
        measured=True, measured_input=100, measured_output=40,
        provenance="measured")
    out = get_turn_composition("proj", task.task_id, rec["turn_id"])
    assert out["cli_overhead_tokens"] is None
    assert out["note"] is None
    assert out["composition"]["categories"] == []


def test_composition_serves_pseudo_task_turn(tmp_path: Path, monkeypatch) -> None:
    # F143-01 review F1: PM plan turns carry the pseudo-task-id "plan" (and governance
    # turns "governance:*") — real recorded turns with real compositions that are NOT
    # entries in list_tasks(). The endpoint must serve them (the turn record is the
    # authority for task membership), not 404 on a task-existence guard.
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    store = _project()
    rec = store.record_turn(
        role="pm", member_id="m-pm", task_id="plan",
        prompt="p", response="r", outcome="planned",
        route_id="claude_cli.sonnet",
        input_tokens=1200, output_tokens=300,
        measured=True, measured_input=1200, measured_output=300,
        provenance="measured",
        composition={"sent_total": 1200, "categories": [
            {"class": "role_instructions", "tokens": 400},
            {"class": "project_context", "tokens": 800}]})
    # "plan" is NOT in list_tasks(), but the turn record's task_id matches the path.
    out = get_turn_composition("proj", "plan", rec["turn_id"])
    assert out["composition"]["sent_total"] == 1200
    assert {c["class"] for c in out["composition"]["categories"]} == {
        "role_instructions", "project_context"}


def test_composition_404s(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    store = _project()
    task = store.add_task(title="do it", role="dev")
    rec = store.record_turn(role="dev", member_id="m-dev", task_id=task.task_id,
                            prompt="p", response="r", outcome="applied",
                            input_tokens=10, output_tokens=4, measured=True,
                            provenance="measured")

    # unknown project
    with pytest.raises(HTTPException) as e1:
        get_turn_composition("nope", task.task_id, rec["turn_id"])
    assert e1.value.status_code == 404

    # unknown task
    with pytest.raises(HTTPException) as e2:
        get_turn_composition("proj", "t-does-not-exist", rec["turn_id"])
    assert e2.value.status_code == 404

    # unknown turn
    with pytest.raises(HTTPException) as e3:
        get_turn_composition("proj", task.task_id, "trn-nope")
    assert e3.value.status_code == 404

    # turn exists but belongs to a different task -> 404
    other = store.add_task(title="other", role="dev")
    with pytest.raises(HTTPException) as e4:
        get_turn_composition("proj", other.task_id, rec["turn_id"])
    assert e4.value.status_code == 404
