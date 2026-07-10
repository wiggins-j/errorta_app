"""F122 — Coding project list status projection.

The all-projects tab should show live/user-action status, not only the persisted
project lifecycle string.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from errorta_council.coding.project_status import derive_project_list_status


def _client(tmp_errorta_home: Path) -> TestClient:
    from errorta_app.server import app

    return TestClient(app, headers={"x-errorta-origin": "tauri-ui"})


def _create(c: TestClient, pid: str, *, north_star: str = "n") -> None:
    r = c.post(
        "/coding/projects",
        json={
            "project_id": pid,
            "north_star": north_star,
            "definition_of_done": "d",
            "target": "new",
        },
    )
    assert r.status_code == 200, r.text


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        (
            {
                "lifecycle_status": "active",
                "run_status": "idle",
                "running": False,
                "stop_reason": None,
                "has_blocking_attention": False,
            },
            ("active", "lifecycle"),
        ),
        (
            {
                "lifecycle_status": "active",
                "run_status": "running",
                "running": True,
                "stop_reason": None,
                "has_blocking_attention": False,
            },
            ("running", "live_run"),
        ),
        (
            {
                "lifecycle_status": "active",
                "run_status": "stopped",
                "running": False,
                "stop_reason": "cancelled",
                "has_blocking_attention": True,
            },
            ("needs attention", "blocking_attention"),
        ),
        (
            {
                "lifecycle_status": "active",
                "run_status": "stopped",
                "running": False,
                "stop_reason": "member_unhealthy",
                "has_blocking_attention": False,
            },
            ("needs attention", "member_unhealthy"),
        ),
        (
            {
                "lifecycle_status": "active",
                "run_status": "stopped",
                "running": False,
                "stop_reason": "worker_unproductive",
                "has_blocking_attention": False,
            },
            ("needs attention", "worker_unproductive"),
        ),
        (
            {
                "lifecycle_status": "active",
                "run_status": "stopped",
                "running": False,
                "stop_reason": "auth_failed",
                "has_blocking_attention": False,
            },
            ("needs attention", "auth_failed"),
        ),
        (
            # F128: a run that stopped because the PM kept claiming done while open
            # work remained is needs-attention, not a false "complete".
            {
                "lifecycle_status": "active",
                "run_status": "stopped",
                "running": False,
                "stop_reason": "completion_blocked",
                "has_blocking_attention": False,
            },
            ("needs attention", "conflict_or_blocker"),
        ),
        (
            {
                "lifecycle_status": "active",
                "run_status": "stopped",
                "running": False,
                "stop_reason": "definition_of_done",
                "has_blocking_attention": False,
            },
            ("active", "lifecycle"),
        ),
        (
            {
                "lifecycle_status": "active",
                "run_status": "stopped",
                "running": False,
                "stop_reason": "user_cancelled",
                "has_blocking_attention": False,
            },
            ("active", "lifecycle"),
        ),
        (
            {
                "lifecycle_status": "failed",
                "run_status": "failed",
                "running": False,
                "stop_reason": None,
                "has_blocking_attention": False,
            },
            ("needs attention", "conflict_or_blocker"),
        ),
    ],
)
def test_derive_project_list_status(kwargs: dict[str, object], expected: tuple[str, str]) -> None:
    assert derive_project_list_status(**kwargs) == expected


def test_list_projects_includes_derived_list_status(
    tmp_errorta_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from errorta_app.routes import coding as coding_routes
    from errorta_council.coding import attention
    from errorta_council.coding.ledger import LedgerStore

    c = _client(tmp_errorta_home)
    _create(c, "active")
    _create(c, "running")
    _create(c, "blocked")
    _create(c, "done")

    LedgerStore("running").set_run_state(status="running", stop_reason=None)
    LedgerStore("done").set_run_state(status="stopped", stop_reason="definition_of_done")
    blocked_store = LedgerStore("blocked")
    attention.raise_signal(
        "blocked",
        kind="problem",
        source="member_health",
        stage="develop",
        title="Member cannot run",
        summary="Provider is not logged in.",
        pm_evaluation="The project cannot continue until the provider is logged in.",
        suggestions=[{"id": "login", "label": "Log in", "detail": "Run the login command."}],
        blocking=True,
        store=blocked_store,
    )

    monkeypatch.setattr(coding_routes, "_thread_alive", lambda project_id: project_id == "running")

    r = c.get("/coding/projects")
    assert r.status_code == 200, r.text
    by_id = {p["id"]: p for p in r.json()["projects"]}

    assert by_id["active"]["status"] == "active"
    assert by_id["active"]["list_status"] == "active"
    assert by_id["active"]["list_status_reason"] == "lifecycle"

    assert by_id["running"]["status"] == "active"
    assert by_id["running"]["list_status"] == "running"
    assert by_id["running"]["list_status_reason"] == "live_run"

    assert by_id["blocked"]["list_status"] == "needs attention"
    assert by_id["blocked"]["list_status_reason"] == "blocking_attention"

    assert by_id["done"]["list_status"] == "active"
    assert by_id["done"]["list_status_reason"] == "lifecycle"
