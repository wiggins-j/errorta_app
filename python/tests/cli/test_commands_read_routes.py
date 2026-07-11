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


# --- real route-serializer shapes (anti-regression for field-nesting bugs) ----
#
# These feed each renderer a payload hand-mirrored from the ACTUAL engine
# serializer (cited inline) and assert the salient values reach the human text.
# If a renderer drifts back to reading a field at the wrong nesting level / name,
# the value vanishes from the render and these FAIL — which the prior fixtures
# (which encoded the buggy shapes) could not catch.


def test_governance_renders_state_nested_fields(make_ctx):
    # shape per governance.py:696-708 (summary → state/artifacts/reviews/approvals)
    # + governance_status.py:168-213 (status → stage/status/headline)
    client = RouteClient(responses={"/governance": {
        "governance": {
            "state": {"mode": "careful", "phase": "reviewing_plan",
                      "human_code_approval": "final_only", "max_review_rounds": 3,
                      "block_on_problems": True, "monitor": {}},
            "artifacts": [], "reviews": [],
            "approvals": [{"approval_id": "a1", "state": "pending"},
                          {"approval_id": "a2", "state": "approved"}],
        },
        "status": {"stage": "plan", "status": "under_review",
                   "headline": "Reviewer is checking the plan"},
    }})
    ctx = make_ctx(project_id=PID)
    _payload, text = registry.dispatch("governance", client, ctx, [])
    assert "careful" in text, text          # mode, from governance["state"]
    assert "reviewing_plan" in text, text   # phase, from governance["state"]
    assert "Reviewer is checking the plan" in text, text  # status["headline"]
    assert "pending approvals: 1" in text, text  # one approval with state=="pending"


def test_models_renders_bucket_aggregated_stats(make_ctx):
    # shape per performance_corpus.py:206-218 (route with buckets[]) + :136-138
    # (bucket attempts/accepted_rate). Weighted rate = (1.0*3 + 0.5*2)/5 = 0.8.
    client = RouteClient(responses={
        "/coding/model-learning": {"learning": {
            "summary": {"total_attempts": 5, "distinct_routes": 1, "window_days": 90},
            "routes": [{"route_id": "anthropic.claude", "capability_tier": "high",
                        "cost_tier": 2, "tiers_unset": False,
                        "buckets": [
                            {"task_type": "code", "difficulty_tier": "hard",
                             "attempts": 3, "accepted": 3, "accepted_rate": 1.0},
                            {"task_type": "code", "difficulty_tier": "mid",
                             "attempts": 2, "accepted": 1, "accepted_rate": 0.5}]}]}},
        "/model-usage": {"usage": {"multi_members": [], "single_members": []}},
    })
    ctx = make_ctx(project_id=PID)
    _payload, text = registry.dispatch("models", client, ctx, [])
    assert "anthropic.claude" in text, text
    assert "5" in text, text     # summed attempts across buckets
    assert "80%" in text, text   # attempt-weighted accepted-rate


def test_runtime_session_renders_real_fields(make_ctx):
    # shape per runtime.py:133-147 (RuntimeSession.to_dict → state/pgid/
    # allocated_ports; NO status/pid/port/url).
    client = RouteClient(responses={
        "/runtime/profiles": {"profiles": [
            {"profile_id": "p1", "kind": "cli", "runtime_mode": "managed_local",
             "start": ["python", "game.py"], "sandbox": "seatbelt"}]},
        "/runtime/sessions/s-9": {"session": {
            "session_id": "s-9", "profile_id": "p1", "state": "running",
            "pgid": 4242, "allocated_ports": [8080, 8081], "sandbox_backend": "seatbelt",
            "started_at": "2026-01-01T00:00:00", "exit_code": None}},
    })
    ctx = make_ctx(project_id=PID)
    _payload, text = registry.dispatch("runtime", client, ctx, ["--session", "s-9"])
    assert "running" in text, text  # session["state"]
    assert "4242" in text, text     # session["pgid"]
    assert "8080" in text, text     # session["allocated_ports"] joined


def test_log_filters_apply_at_render(make_ctx):
    client = RouteClient(responses={"/team-log": {"entries": [
        {"at": "t", "role": "dev", "member": "m-1", "kind": "k", "message": "hello pygame"},
        {"at": "t", "role": "reviewer", "member": "m-2", "kind": "k", "message": "looks good"},
    ]}})
    ctx = make_ctx(project_id=PID)
    _payload, text = registry.dispatch("log", client, ctx, ["--role", "dev", "--grep", "pygame"])
    assert "hello pygame" in text
    assert "looks good" not in text
