"""Golden invariant #5: no secret / raw-payload leak in rendered output.

Every renderer is given a payload seeded with a secret-shaped value in a field it
should NOT surface (a raw dump, an api key on an item). The human render must never
print it; only the explicit ``--json`` bypass may — and ``--json`` must print the
raw payload and nothing else on stdout.
"""
from __future__ import annotations

import json

import pytest

from errorta_cli import registry

from .conftest import RouteClient

PID = "proj-1"
SECRET = "sk-ant-SECRETLEAK-DO-NOT-RENDER-0001"
RAW = "RAWDUMP-SENTINEL-DO-NOT-RENDER"


def _kitchen_sink() -> dict:
    """A payload carrying every list/snapshot key any renderer reads, with SECRET
    planted only in fields renderers must not surface."""
    return {
        # top-level raw-dump traps (no renderer should str() the whole payload)
        "_raw_dump": RAW,
        "_secret": SECRET,
        # /healthz + status
        "service": "errorta", "version": "1", "python": "3.14",
        "build": {"commit": "abc", "_secret": SECRET},
        "residency": {"mode": "local"},
        "run": {"running": False, "state": {
            "status": "stopped", "stop_reason": "definition_of_done",
            "counters": {"iterations": 3}, "_secret": SECRET, "last_error": None}},
        # team-log
        "entries": [{"at": "2026-01-01T00:00:00", "role": "dev", "member": "m-1",
                     "kind": "pr_opened", "message": "opened", "_secret": SECRET}],
        # decisions (rationale/context/extra are hidden fields)
        "decisions": [{"choice": "pr_merged", "title": "t", "at": "2026-01-01T00:00:00",
                       "rationale": SECRET, "context": SECRET, "_secret": SECRET}],
        # backlog
        "tasks": [{"task_id": "t1", "title": "do it", "role": "dev", "state": "todo",
                   "detail": SECRET, "_secret": SECRET}],
        # prs + worktree gate/diff
        "prs": [{"pr_id": "pr-1", "status": "open", "branch": "b", "task_id": "t1",
                 "reviewer_approved": True, "tests_passed": True, "_secret": SECRET}],
        "gate": {"allowed": True, "blockers": []},
        "diff": "",  # diff is a shown field; keep empty so it can't carry the secret
        # turns + composition
        "turns": [{"turn_id": "tn-1", "role": "dev", "route_id": "r", "outcome": "accepted",
                   "usage": {"input_tokens": 1, "output_tokens": 1, "_secret": SECRET},
                   "prompt": "P", "response": "R", "_secret": SECRET}],
        "composition": {"composition": {"sent_total": 1, "categories": []},
                        "cli_overhead_tokens": None, "note": None},
        # attention (summary/context hidden)
        "signals": [{"kind": "problem", "blocking": True, "stage": "s", "title": "t",
                     "state": "open", "summary": SECRET, "context": SECRET, "_secret": SECRET}],
        "blocks_stage": False,
        # runtime (env_required/_secret hidden)
        "profiles": [{"profile_id": "p", "kind": "cli", "runtime_mode": "managed_local",
                      "start": ["x"], "sandbox": "none", "env_required": [SECRET],
                      "_secret": SECRET}],
        # runtime session — real RuntimeSession.to_dict shape (runtime.py):
        # state/pgid/allocated_ports (NOT status/pid/port/url).
        "session": {"session_id": "s", "state": "running", "pgid": 4242,
                    "allocated_ports": [8080], "sandbox_backend": "none",
                    "_secret": SECRET},
        # tokens + team (usage)
        "usage": {
            "total": {"input": 1, "output": 1, "turns": 1, "coverage": {"measured_pct": 100}},
            "by_role": {"dev": {"input": 1, "output": 1, "turns": 1,
                                 "coverage": {"measured_pct": 100}}},
            "multi_members": [{"member_id": "m-2", "role": "dev", "pool": ["r"],
                               "_secret": SECRET}],
            "single_members": [{"member_id": "m-1", "route_id": "r"}]},
        # models — learning_digest shape (performance_corpus.py): attempts/
        # accepted_rate live inside route["buckets"][], not on the route.
        "learning": {"summary": {"total_attempts": 1, "distinct_routes": 1, "window_days": 90},
                     "routes": [{"route_id": "r", "capability_tier": "mid", "cost_tier": 0,
                                 "buckets": [{"task_type": "code", "difficulty_tier": "mid",
                                              "attempts": 1, "accepted": 1, "accepted_rate": 1.0,
                                              "_secret": SECRET}],
                                 "_secret": SECRET}]},
        # governance — summary() shape (governance.py): settings under state[].
        "governance": {"state": {"mode": "careful", "phase": "idle", "_secret": SECRET},
                       "artifacts": [], "reviews": [], "approvals": [], "_secret": SECRET},
        "status": {"stage": "s", "status": "i", "headline": ""},
        # pm chat + pm changes (summary is shown → keep clean; secret in hidden detail)
        "thread": [{"role": "user", "message": "hi", "at": "2026-01-01T00:00:00",
                    "_secret": SECRET}],
        "pending": [{"change_id": "c-1", "kind": "assign_models", "status": "pending",
                     "summary": "dev to sonnet", "detail": SECRET, "_secret": SECRET}],
        "recent": [],
    }


# name → representative args (every read command in the S2 surface).
COMMAND_ARGS = {
    "status": [], "log": [], "decisions": [], "tasks": [], "board": [],
    "prs": [], "pr": ["pr-1"], "tokens": [], "turns": [], "turn": ["t1", "tn-1"],
    "attention": [], "runtime": ["--session", "s"], "team": [], "models": [],
    "governance": [], "pm": ["chat"],
}


@pytest.mark.parametrize("name", sorted(COMMAND_ARGS))
def test_human_render_never_leaks_secret(name, make_ctx):
    client = RouteClient(default=_kitchen_sink())
    ctx = make_ctx(project_id=PID)
    _payload, text = registry.dispatch(name, client, ctx, list(COMMAND_ARGS[name]))
    assert SECRET not in text, f"{name} leaked a secret into the human render"
    assert RAW not in text, f"{name} dumped a raw-payload sentinel"


@pytest.mark.parametrize("name", sorted(COMMAND_ARGS))
def test_json_bypass_prints_raw_payload_only(name, make_ctx):
    client = RouteClient(default=_kitchen_sink())
    ctx = make_ctx(project_id=PID)
    _payload, text = registry.dispatch(
        name, client, ctx, list(COMMAND_ARGS[name]), json_mode=True
    )
    # Valid JSON, and only JSON — nothing else on the (stdout) surface.
    parsed = json.loads(text)
    assert isinstance(parsed, (dict, list))
    # The raw payload is exactly what --json is for: the secret IS present here.
    assert SECRET in text, f"{name} --json should carry the raw payload"


def test_pm_changes_json_and_human(make_ctx):
    client = RouteClient(default=_kitchen_sink())
    ctx = make_ctx(project_id=PID)
    _p, human = registry.dispatch("pm", client, ctx, ["changes"])
    assert SECRET not in human
    _p, js = registry.dispatch("pm", client, ctx, ["changes"], json_mode=True)
    assert SECRET in js and json.loads(js)
