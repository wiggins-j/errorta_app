"""Each S2 read command hits the RIGHT route(s) and renders without error.

Grounded against the real ``coding.py`` GET routes (verified file:line in the S2
report). Uses a path-aware fake client so a command's exact route sequence is
asserted, and dispatches through the shared registry so the render path is
exercised too (a crash in any renderer fails here).
"""
from __future__ import annotations

import pytest

from errorta_cli import registry

from .conftest import RouteClient

PID = "proj-1"

# name → (raw_args, [substrings every one of which must appear in some hit path])
CASES = {
    "status": ([], ["/healthz", f"/coding/projects/{PID}/run"]),
    "log": ([], [f"/coding/projects/{PID}/team-log"]),
    "decisions": ([], [f"/coding/projects/{PID}/decisions"]),
    "tasks": ([], [f"/coding/projects/{PID}/backlog"]),
    "board": ([], [f"/coding/projects/{PID}/backlog"]),
    "prs": ([], [f"/coding/projects/{PID}/prs"]),
    "pr": (["pr-1"], [f"/coding/projects/{PID}/prs", f"/coding/projects/{PID}/worktree"]),
    "tokens": ([], [f"/coding/projects/{PID}/usage-summary"]),
    "turns": ([], [f"/coding/projects/{PID}/turns"]),
    "turn": (["t1", "tn-1"], [
        f"/coding/projects/{PID}/turns",
        f"/coding/projects/{PID}/tasks/t1/turns/tn-1/composition",
    ]),
    "attention": ([], [f"/coding/projects/{PID}/attention"]),
    "runtime": ([], [f"/coding/projects/{PID}/runtime/profiles"]),
    "team": ([], [f"/coding/projects/{PID}/model-usage"]),
    "models": ([], ["/coding/model-learning", f"/coding/projects/{PID}/model-usage"]),
    "governance": ([], [f"/coding/projects/{PID}/governance"]),
    "pm": (["chat"], [f"/coding/projects/{PID}/pm-chat"]),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_command_hits_expected_route(name, make_ctx):
    raw_args, expected = CASES[name]
    client = RouteClient()
    ctx = make_ctx(project_id=PID)
    payload, text = registry.dispatch(name, client, ctx, list(raw_args))
    paths = client.paths()
    for route in expected:
        assert any(route in p for p in paths), (name, route, paths)
    # Renders to a non-empty string without raising.
    assert isinstance(text, str) and text != ""


def test_pm_changes_hits_pm_changes_route(make_ctx):
    client = RouteClient()
    ctx = make_ctx(project_id=PID)
    registry.dispatch("pm", client, ctx, ["changes"])
    assert any(f"/coding/projects/{PID}/pm-changes" in p for p in client.paths())


def test_runtime_session_hits_session_route(make_ctx):
    client = RouteClient()
    ctx = make_ctx(project_id=PID)
    registry.dispatch("runtime", client, ctx, ["--session", "s-9"])
    assert any(f"/coding/projects/{PID}/runtime/sessions/s-9" in p for p in client.paths())


def test_project_scoped_command_makes_no_call_without_project(make_ctx):
    """A project-scoped read early-returns (no route) when nothing is bound."""
    client = RouteClient()
    ctx = make_ctx()  # no project
    payload, text = registry.dispatch("log", client, ctx, [])
    assert client.calls == []
    assert "no project" in text.lower()


def test_decisions_kind_glob_filters_render(make_ctx):
    client = RouteClient(responses={"/decisions": {"decisions": [
        {"choice": "pr_merged", "title": "merged", "at": "2026-01-01T00:00:00"},
        {"choice": "review_approved", "title": "ok", "at": "2026-01-01T00:01:00"},
    ]}})
    ctx = make_ctx(project_id=PID)
    _payload, text = registry.dispatch("decisions", client, ctx, ["--kind", "pr_*"])
    assert "pr_merged" in text
    assert "review_approved" not in text


def test_log_filters_apply_at_render(make_ctx):
    client = RouteClient(responses={"/team-log": {"entries": [
        {"at": "t", "role": "dev", "member": "m-1", "kind": "k", "message": "hello pygame"},
        {"at": "t", "role": "reviewer", "member": "m-2", "kind": "k", "message": "looks good"},
    ]}})
    ctx = make_ctx(project_id=PID)
    _payload, text = registry.dispatch("log", client, ctx, ["--role", "dev", "--grep", "pygame"])
    assert "hello pygame" in text
    assert "looks good" not in text
