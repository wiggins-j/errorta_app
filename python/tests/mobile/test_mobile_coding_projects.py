from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from errorta_app import server as server_mod
from errorta_council.coding.ledger import LedgerStore
from errorta_mobile import config as mobile_config
from errorta_mobile import devices as mobile_devices
from errorta_project_grounding.corpus_binding import ProjectCorpusBinding, save_binding


@pytest.fixture(autouse=True)
def _isolated_errorta_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    return tmp_path


def _auth_headers(
    *,
    read_coding_projects: bool = True,
    read_coding_activity: bool = False,
    read_runs: bool = False,
) -> dict[str, str]:
    mobile_config.save({"enabled": True, "bind_mode": "loopback_dev"})
    token = "session-token"
    record = mobile_devices.create(
        display_name="Test iPhone",
        platform="ios",
        public_key="public-key",
        session_token=token,
    )
    mobile_devices.update_capabilities(
        record["device_id"],
        {
            "read_runs": read_runs,
            "read_coding_projects": read_coding_projects,
            "read_coding_activity": read_coding_activity,
        },
    )
    return {
        "x-errorta-mobile-device-id": record["device_id"],
        "authorization": f"Bearer {token}",
    }


class _Result:
    def __init__(self, command_id: str, exit_code: int = 0) -> None:
        self.command_id = command_id
        self.exit_code = exit_code

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "argv": ["python", "-m", "pytest"],
            "cwd": ".",
            "exit_code": self.exit_code,
            "stdout_sha256": "a" * 64,
            "stderr_sha256": "b" * 64,
            "duration_ms": 10,
        }


class _Session:
    command_ids = ["unit"]
    unknown_ids: list[str] = []
    passed = True
    results = [_Result("unit")]
    sandbox = "seatbelt"


def _seed_project() -> LedgerStore:
    store = LedgerStore("PocketBoard")
    store.create_project(
        north_star="Build a PocketBoard kanban app for quick team testing.",
        definition_of_done="Cards can move columns and tests pass.",
        target="existing",
        repo_path="/Users/example/SecretRepo",
    )
    save_binding(
        store,
        ProjectCorpusBinding(
            project_id="PocketBoard",
            mode="build_from_repo",
            corpus_id="secret-corpus",
            source_root="/Users/example/SecretRepo",
            health_state="ready",
            adapter_source="remote",
        ),
    )
    todo = store.add_task(
        title="Add mobile-safe empty state",
        role="dev",
        detail="Do not expose this raw detail to the phone.",
    )
    dev = store.add_task(title="Build list cards", role="dev", assignee_member_id="m-dev")
    reviewer = store.add_task(
        title="review PR: Build list cards",
        role="reviewer",
        depends_on=[dev.task_id],
    )
    tester = store.add_task(
        title="test PR: Build list cards",
        role="tester",
        depends_on=[reviewer.task_id],
    )
    store.update_task(todo.task_id, state="todo")
    store.update_task(dev.task_id, state="done")
    store.update_task(reviewer.task_id, state="done")
    store.update_task(tester.task_id, state="done")
    pr = store.record_pr(
        task_id=dev.task_id,
        branch="task/build-list-cards",
        head="abcdef1234567890",
        dev_member="m-dev",
    )
    store.update_pr(
        pr["pr_id"],
        status="merged",
        reviewer_approved=True,
        tests_passed=True,
        conflicts=[{"path": "/Users/example/SecretRepo/src/App.tsx"}],
    )
    store.record_test_run(_Session(), task_id=tester.task_id, head="abcdef1234567890")
    store.record_tool_event(
        turn_id="turn-1",
        task_id=dev.task_id,
        member_id="m-dev",
        role="dev",
        tool="code_write",
        status="succeeded",
        intent={
            "path": "/Users/example/SecretRepo/src/App.tsx",
            "prompt": "SECRET_RAW_PROMPT",
        },
        result={
            "path": "src/App.tsx",
            "stdout": "SECRET_STDOUT",
            "diff": "SECRET_DIFF",
        },
    )
    store.record_decision(
        title="Use local storage",
        context="SECRET_CONTEXT",
        choice="local_storage",
        rationale="SECRET_RATIONALE",
        related_task_ids=[dev.task_id],
    )
    store.set_run_state(status="running", started_at="2026-06-18T00:00:00Z")
    return store


def test_mobile_coding_projects_follow_connector_and_capability_gates() -> None:
    client = TestClient(server_mod.app)

    disabled = client.get("/mobile/v1/coding-projects")
    assert disabled.status_code == 503
    assert disabled.json()["detail"] == "mobile_connector_disabled"

    mobile_config.save({"enabled": True, "bind_mode": "loopback_dev"})
    unauthenticated = client.get("/mobile/v1/coding-projects")
    assert unauthenticated.status_code == 401
    assert unauthenticated.json()["detail"] == "mobile_device_auth_required"

    forbidden = client.get(
        "/mobile/v1/coding-projects",
        headers=_auth_headers(read_coding_projects=False, read_runs=True),
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["detail"] == "mobile_capability_forbidden:read_coding_projects"


def test_mobile_coding_project_board_is_safe_and_has_done_badges() -> None:
    _seed_project()
    client = TestClient(server_mod.app)
    headers = _auth_headers()

    detail = client.get("/mobile/v1/coding-projects/PocketBoard", headers=headers)
    assert detail.status_code == 200, detail.text
    project = detail.json()["project"]
    assert project["project_id"] == "PocketBoard"
    assert project["run_state"] == "running"
    assert project["progress"] == {
        "total": 4,
        "done": 3,
        "doing": 0,
        "todo": 1,
        "blocked": 0,
        "percent": 75,
    }
    assert project["grounding"] == {"mode": "build_from_repo", "health_state": "ready"}
    assert project["needs_attention"] is False

    response = client.get(
        "/mobile/v1/coding-projects/PocketBoard/board",
        headers=headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    done = {task["title"]: task for task in body["columns"]["done"]}
    labels = {badge["label"] for badge in done["Build list cards"]["badges"]}
    assert {"DEV", "PR merged", "Review approved", "Tests passed"} <= labels

    listed = client.get("/mobile/v1/coding-projects", headers=headers)
    assert listed.status_code == 200, listed.text
    payload = json.dumps([detail.json(), body, listed.json()])
    assert "/Users/example" not in payload
    assert "SecretRepo" not in payload
    assert "secret-corpus" not in payload
    assert "source_root" not in payload
    assert "adapter_source" not in payload
    assert "raw detail" not in payload
    assert "SECRET_RAW_PROMPT" not in payload
    assert "SECRET_STDOUT" not in payload
    assert "SECRET_DIFF" not in payload


def test_mobile_coding_project_activity_requires_activity_capability_and_redacts() -> None:
    _seed_project()
    client = TestClient(server_mod.app)

    denied = client.get(
        "/mobile/v1/coding-projects/PocketBoard/activity",
        headers=_auth_headers(read_coding_projects=True, read_coding_activity=False),
    )
    assert denied.status_code == 403
    assert denied.json()["detail"] == "mobile_capability_forbidden:read_coding_activity"

    allowed = client.get(
        "/mobile/v1/coding-projects/PocketBoard/activity",
        headers=_auth_headers(read_coding_projects=True, read_coding_activity=True),
    )
    assert allowed.status_code == 200, allowed.text
    body = allowed.json()
    summaries = [item["summary"] for item in body["items"]]
    assert any("DEV code_write src/App.tsx" in summary for summary in summaries)
    assert any("Use local storage: local_storage" in summary for summary in summaries)

    payload = json.dumps(body)
    assert "/Users/example" not in payload
    assert "SecretRepo" not in payload
    assert "SECRET_RAW_PROMPT" not in payload
    assert "SECRET_STDOUT" not in payload
    assert "SECRET_CONTEXT" not in payload
    assert "SECRET_RATIONALE" not in payload


def test_mobile_coding_activity_redacts_absolute_only_paths() -> None:
    store = _seed_project()
    task = next(task for task in store.list_tasks() if task.title == "Build list cards")
    store.record_tool_event(
        turn_id="turn-absolute",
        task_id=task.task_id,
        member_id="m-dev",
        role="dev",
        tool="code_exec",
        status="succeeded",
        intent={
            "path": "~/Private/foo.txt",
            "file": "C:\\Sensitive\\token.txt",
        },
        result={"cwd": "/Users/example/SecretClient"},
    )
    client = TestClient(server_mod.app)

    allowed = client.get(
        "/mobile/v1/coding-projects/PocketBoard/activity",
        headers=_auth_headers(read_coding_projects=True, read_coding_activity=True),
    )

    assert allowed.status_code == 200, allowed.text
    body = allowed.json()
    summaries = [item["summary"] for item in body["items"]]
    assert any(summary == "DEV code_exec" for summary in summaries)
    payload = json.dumps(body)
    assert "SecretClient" not in payload
    assert "Private" not in payload


def test_mobile_coding_free_text_fields_are_redacted() -> None:
    # Defense in depth: a secret / absolute home path / SSH host typed into an
    # author/model-authored FREE-TEXT field (North Star, DoD, task title,
    # decision text) must be scrubbed before it reaches a paired phone.
    store = LedgerStore("RedactProj")
    store.create_project(
        north_star="Ship it via /Users/example/.ssh/id and token sk-ant-LEAKTOKEN12345",
        definition_of_done="Done when deployed to user@10.1.2.3 (secret sk-ant-DODLEAK99999)",
        target="new",
        repo_path=None,
    )
    store.add_task(title="wire /Users/example/secrets.txt loader", role="dev")
    store.record_decision(
        title="use key sk-ant-DECISIONLEAK77",
        context="plan",
        choice="adopt /Users/example/private/config",
        rationale="r",
    )
    client = TestClient(server_mod.app)
    headers = _auth_headers(read_coding_projects=True, read_coding_activity=True)

    blobs = [
        client.get("/mobile/v1/coding-projects", headers=headers).text,
        client.get("/mobile/v1/coding-projects/RedactProj", headers=headers).text,
        client.get("/mobile/v1/coding-projects/RedactProj/board", headers=headers).text,
        client.get("/mobile/v1/coding-projects/RedactProj/activity", headers=headers).text,
    ]
    payload = "\n".join(blobs)
    for leak in (
        "sk-ant-LEAKTOKEN12345", "sk-ant-DODLEAK99999", "sk-ant-DECISIONLEAK77",
        "/Users/example", "10.1.2.3",
    ):
        assert leak not in payload, f"free-text leak reached the phone: {leak}"
    assert "Sensitive" not in payload
    assert "foo.txt" not in payload
    assert "token.txt" not in payload


def test_mobile_coding_project_prs_and_test_runs_are_projected() -> None:
    _seed_project()
    client = TestClient(server_mod.app)
    headers = _auth_headers()

    prs = client.get("/mobile/v1/coding-projects/PocketBoard/prs", headers=headers)
    assert prs.status_code == 200, prs.text
    pr = prs.json()["prs"][0]
    assert pr["branch_label"] == "task/build-list-cards"
    assert pr["status"] == "merged"
    assert pr["review"] == "approved"
    assert pr["tests"] == "passed"
    assert pr["conflict_count"] == 1
    assert "conflicts" not in pr

    tests = client.get("/mobile/v1/coding-projects/PocketBoard/test-runs", headers=headers)
    assert tests.status_code == 200, tests.text
    test_run = tests.json()["runs"][0]
    assert test_run["passed"] is True
    assert test_run["command_count"] == 1
    assert test_run["sandbox"] == "seatbelt"
    assert "results" not in test_run


def test_mobile_coding_pr_projection_uses_direct_task_pr_id() -> None:
    store = LedgerStore("DirectPr")
    store.create_project(
        north_star="Direct PR test association.",
        definition_of_done="Tests pass.",
        target="new",
        repo_path=None,
    )
    dev = store.add_task(title="Build direct PR feature", role="dev")
    store.update_task(dev.task_id, state="done")
    pr = store.record_pr(
        task_id=dev.task_id,
        branch="task/direct-pr-feature",
        head="1234567890abcdef",
        dev_member="m-dev",
    )
    tester = store.add_task(
        title="test PR: direct",
        role="tester",
        pr_id=pr["pr_id"],
    )
    store.update_task(tester.task_id, state="done")
    store.record_test_run(_Session(), task_id=tester.task_id, head="1234567890abcdef")
    client = TestClient(server_mod.app)
    headers = _auth_headers()

    prs = client.get("/mobile/v1/coding-projects/DirectPr/prs", headers=headers)
    assert prs.status_code == 200, prs.text
    assert prs.json()["prs"][0]["tests"] == "passed"

    board = client.get("/mobile/v1/coding-projects/DirectPr/board", headers=headers)
    assert board.status_code == 200, board.text
    done = {task["title"]: task for task in board.json()["columns"]["done"]}
    labels = {badge["label"] for badge in done["Build direct PR feature"]["badges"]}
    assert "Tests passed" in labels


def test_mobile_coding_project_missing_and_invalid_ids_are_stable() -> None:
    client = TestClient(server_mod.app)
    headers = _auth_headers()

    missing = client.get("/mobile/v1/coding-projects/MissingProject", headers=headers)
    assert missing.status_code == 404
    assert missing.json()["detail"] == "mobile_coding_project_not_found"

    invalid = client.get("/mobile/v1/coding-projects/bad%5Cname", headers=headers)
    assert invalid.status_code == 422
    assert invalid.json()["detail"] == "mobile_coding_project_invalid"
