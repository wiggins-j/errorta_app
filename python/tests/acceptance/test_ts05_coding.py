"""TS-05 — Coding Team: acceptance journey (project lifecycle slice).

The greenfield project lifecycle through the owner-gated routes: create
(TC-05.1) -> appears in the list + round-trips on GET -> delete (TC-05.18). A
non-owner request is refused. F117 attention signals are locked here at the
route/user journey level: a blocking Problem appears, blocks governance, resolves
into a PM task, and unblocks. The full Brainstorm->Build governance run is
covered by tests/coding/ (fake-provider) + the manual/live layer.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from errorta_app.routes import coding as coding_routes

pytestmark = [pytest.mark.acceptance, pytest.mark.regression]

TAURI = {"x-errorta-origin": "tauri-ui"}


@pytest.fixture
def app_client(tmp_errorta_home, tmp_path):
    app = FastAPI()
    app.include_router(coding_routes.router)
    root = tmp_path / "projects"
    root.mkdir()
    return TestClient(app, headers=TAURI), root


def test_ts05_project_lifecycle(app_client) -> None:
    client, root = app_client
    body = {
        "project_id": "qa-proj", "north_star": "ship it",
        "definition_of_done": "tests pass", "target": "new",
        "delivery_root": str(root),
    }

    # TC-05.1: create a greenfield project.
    created = client.post("/coding/projects", json=body)
    assert created.status_code == 200, created.text
    assert created.json()["project"]["id"] == "qa-proj"

    # Listed + round-trips on GET.
    listed = client.get("/coding/projects").json()
    assert any(p["id"] == "qa-proj" for p in listed["projects"])
    assert client.get("/coding/projects/qa-proj").status_code == 200

    # TC-05.18: delete removes it.
    assert client.delete("/coding/projects/qa-proj").status_code == 200
    assert client.get("/coding/projects/qa-proj").status_code == 404


def test_ts05_create_requires_owner(tmp_errorta_home, tmp_path) -> None:
    app = FastAPI()
    app.include_router(coding_routes.router)
    client = TestClient(app)  # no Tauri origin
    resp = client.post("/coding/projects", json={
        "project_id": "nope", "north_star": "n",
        "definition_of_done": "d", "target": "new",
    })
    assert resp.status_code == 403


def test_ts05_attention_problem_blocks_and_resolves(app_client) -> None:
    """TC-05.26: a blocking attention Problem resolves into PM work."""
    from errorta_council.coding import attention
    from errorta_council.coding.governance import GovernanceState, GovernanceStore
    from errorta_council.coding.governance_scheduler import next_governance_action
    from errorta_council.coding.ledger import LedgerStore
    from errorta_council.coding.topology import Complete

    client, root = app_client
    project_id = "qa-attention-proj"
    created = client.post(
        "/coding/projects",
        json={
            "project_id": project_id,
            "north_star": "ship without ambiguity",
            "definition_of_done": "problem resolved",
            "target": "new",
            "delivery_root": str(root),
        },
    )
    assert created.status_code == 200, created.text

    store = LedgerStore(project_id)
    GovernanceStore.for_ledger(store).save_state(
        GovernanceState(mode="light", phase="drafting_spec")
    )
    signal = attention.raise_signal(
        project_id,
        kind="problem",
        source="pm",
        stage="drafting_spec",
        title="Pick storage",
        summary="The project must choose SQLite or JSON.",
        pm_evaluation="The current spec is blocked on storage authority.",
        suggestions=[
            {
                "id": "sqlite",
                "label": "Use SQLite",
                "detail": "Record SQLite as the local persistence mechanism.",
            }
        ],
        store=store,
    )

    listed = client.get(
        f"/coding/projects/{project_id}/attention",
        params={"state": "open", "kind": "problem"},
    )
    assert listed.status_code == 200, listed.text
    listed_body = listed.json()
    assert listed_body["blocks_stage"] is True
    assert [item["id"] for item in listed_body["signals"]] == [signal.id]

    blocked = next_governance_action(store, {"pm": ["pm-1"]})
    assert isinstance(blocked, Complete)
    assert blocked.reason == "blocked_on_problem"

    resolved = client.post(
        f"/coding/projects/{project_id}/attention/{signal.id}/resolve",
        json={"action": "accept", "suggestion_id": "sqlite"},
    )
    assert resolved.status_code == 200, resolved.text
    resolved_body = resolved.json()
    assert resolved_body["signal"]["state"] == "accepted"
    assert resolved_body["created_task_id"]

    task = next(
        task
        for task in store.list_tasks()
        if task.task_id == resolved_body["created_task_id"]
    )
    assert task.role == "pm"
    assert task._extras["source_signal_id"] == signal.id
    assert "SQLite" in task.detail

    open_after = client.get(
        f"/coding/projects/{project_id}/attention",
        params={"state": "open"},
    )
    assert open_after.status_code == 200, open_after.text
    assert open_after.json()["signals"] == []
    assert open_after.json()["blocks_stage"] is False

    unblocked = next_governance_action(store, {"pm": ["pm-1"]})
    assert not (
        isinstance(unblocked, Complete)
        and getattr(unblocked, "reason", "") == "blocked_on_problem"
    )
