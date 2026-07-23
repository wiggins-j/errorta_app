"""``watch`` — the Spec 06 live run dashboard (client-side compose, no new route).

The command polls ``/run``, ``/usage-summary``, ``/test-runs``, ``/team-log`` and
``/turns``, merges them into one snapshot, and ``render_watch`` draws a single
compact panel. These exercise the command through the shared registry against a
path-aware fake client serving each route's real serializer shape.
"""
from __future__ import annotations

import json

from errorta_cli import registry
from errorta_cli.render.watch import render_watch

from .conftest import RouteClient

PID = "proj-1"


def _gate_run(*, exit_codes: list[int], passed: bool, head: str, at: str) -> dict:
    """One recorded test run, mirroring ``record_test_run`` / gate.py's shape."""
    results = [
        {"command_id": f"cmd-{i}", "status": "completed" if code == 0 else "failed",
         "exit_code": code, "passed": code == 0}
        for i, code in enumerate(exit_codes)
    ]
    return {
        "test_run_id": f"tr-{head}", "task_id": "t-1",
        "command_ids": [r["command_id"] for r in results],
        "passed": passed, "results": results, "head": head,
        "sandbox": "seatbelt", "at": at,
    }


def _run_payload(*, running=True, status="running", stop_reason=None,
                 iterations=61, model_calls=120) -> dict:
    return {
        "running": running,
        "result": None,
        "state": {
            "status": status, "stop_reason": stop_reason,
            "counters": {"iterations": iterations, "model_calls": model_calls},
        },
        "caps": {
            "max_iterations": 200, "max_model_calls": None,
            "max_parallel_workers": None, "delivery_review_round_limit": 3,
            "defaulted": [],
        },
    }


def _usage_payload() -> dict:
    return {"usage": {"total": {"input": 900000, "output": 431926, "turns": 61}}}


def _team_log_payload() -> dict:
    return {"entries": [
        {"at": "2026-07-22T15:59:00", "role": "pm", "member": "pm-1",
         "kind": "plan_posted", "message": "planned the split"},
        {"at": "2026-07-22T16:03:00", "role": "dev", "member": "dev-1",
         "kind": "pr_opened", "message": "opened PR for t-3"},
    ]}


def _turns_payload() -> dict:
    return {"turns": [
        {"turn_id": "tn-1", "role": "dev", "member": "dev-1", "outcome": "accepted"},
    ]}


def _responses(*, run: dict, test_runs: dict) -> dict:
    return {
        "/usage-summary": _usage_payload(),   # matched before "-summary"/"run"
        "/test-runs": test_runs,
        "/team-log": _team_log_payload(),
        "/turns": _turns_payload(),
        "/run": run,                          # least-specific; keep last
    }


def _converging_runs() -> dict:
    return {"runs": [
        _gate_run(exit_codes=[0] * 7 + [1] * 5, passed=False, head="aaaa1111",
                  at="2026-07-22T10:00:00"),
        _gate_run(exit_codes=[0] * 9 + [1] * 3, passed=False, head="bbbb2222",
                  at="2026-07-22T11:30:00"),
    ]}


def _stalled_runs() -> dict:
    return {"runs": [
        _gate_run(exit_codes=[0] * 9 + [1] * 3, passed=False, head="aaaa1111",
                  at="2026-07-22T10:00:00"),
        _gate_run(exit_codes=[0] * 9 + [1] * 3, passed=False, head="bbbb2222",
                  at="2026-07-22T11:30:00"),
    ]}


# --------------------------------------------------------------------------- #
# Composition — hits every source route, once.
# --------------------------------------------------------------------------- #

def test_watch_hits_all_source_routes(make_ctx):
    client = RouteClient(
        responses=_responses(run=_run_payload(), test_runs=_converging_runs()))
    ctx = make_ctx(project_id=PID)
    _payload, text = registry.dispatch("watch", client, ctx, [])
    paths = client.paths()
    for route in ("/run", "/usage-summary", "/test-runs", "/team-log", "/turns"):
        assert any(f"/coding/projects/{PID}{route}" in p for p in paths), (route, paths)
    assert isinstance(text, str) and text != ""


# --------------------------------------------------------------------------- #
# Panel content.
# --------------------------------------------------------------------------- #

def test_watch_panel_composes_expected_lines(make_ctx):
    client = RouteClient(
        responses=_responses(run=_run_payload(), test_runs=_converging_runs()))
    ctx = make_ctx(project_id=PID)
    _payload, text = registry.dispatch("watch", client, ctx, [])
    # line 1 — run status + turn + caps + indicator
    assert "run: running" in text, text
    assert "turn 61" in text, text
    assert "caps: iter 61/200" in text, text
    assert "calls 120/∞" in text, text          # unlimited model_calls
    assert "[converging]" in text, text          # gate rose 7/12 → 9/12
    # line 2 — token total (in / out)
    assert "tokens: 1,331,926 (in 900,000 / out 431,926)" in text, text
    # line 3 — gate latest + trend (reused gate pass-count logic)
    assert "gate: 9/12" in text, text
    assert "7/12 → 9/12" in text, text
    # line 4 + 5 — members + last event
    assert "members:" in text and "dev-1 pr_opened" in text, text
    assert "last:" in text and "opened PR for t-3" in text, text


def test_watch_indicator_done_when_stop_reason_set(make_ctx):
    run = _run_payload(running=False, status="stopped",
                       stop_reason="definition_of_done")
    client = RouteClient(responses=_responses(run=run, test_runs=_converging_runs()))
    ctx = make_ctx(project_id=PID)
    _payload, text = registry.dispatch("watch", client, ctx, [])
    assert "[done]" in text, text
    assert "definition_of_done" in text, text    # terminal reason surfaced


def test_watch_indicator_stalled_when_gate_flat(make_ctx):
    client = RouteClient(
        responses=_responses(run=_run_payload(), test_runs=_stalled_runs()))
    ctx = make_ctx(project_id=PID)
    _payload, text = registry.dispatch("watch", client, ctx, [])
    assert "[stalled]" in text, text             # last two gate runs unchanged


def test_watch_indicator_running_without_gate_history(make_ctx):
    client = RouteClient(
        responses=_responses(run=_run_payload(), test_runs={"runs": []}))
    ctx = make_ctx(project_id=PID)
    _payload, text = registry.dispatch("watch", client, ctx, [])
    assert "[running]" in text, text
    assert "gate: no runs" in text, text


# --------------------------------------------------------------------------- #
# render_watch directly — degrades gracefully on a sparse / caps-less snapshot.
# --------------------------------------------------------------------------- #

def test_render_watch_degrades_without_caps():
    payload = {
        "run": {"running": True, "state": {"status": "running", "counters": {}}},
        "usage": {}, "test_runs": {}, "team_log": {}, "turns": {},
    }
    text = render_watch(payload, None)
    assert "run: running" in text
    assert "caps:" not in text                   # older server omits caps
    assert "tokens: 0 (in 0 / out 0)" in text
    assert "gate: no runs" in text


# --------------------------------------------------------------------------- #
# Scriptable surfaces.
# --------------------------------------------------------------------------- #

def test_watch_json_returns_raw_composed_snapshot(make_ctx):
    run, test_runs = _run_payload(), _converging_runs()
    client = RouteClient(responses=_responses(run=run, test_runs=test_runs))
    ctx = make_ctx(project_id=PID)
    _payload, text = registry.dispatch("watch", client, ctx, [], json_mode=True)
    parsed = json.loads(text)
    # The raw compose keys are present (each carrying its route's raw payload).
    assert set(parsed) == {"run", "usage", "test_runs", "team_log", "turns"}
    assert parsed["run"] == run
    assert parsed["test_runs"] == test_runs


def test_watch_unbound_project_is_no_project_path(make_ctx):
    client = RouteClient(responses=_responses(run=_run_payload(),
                                              test_runs=_converging_runs()))
    ctx = make_ctx()  # no project bound
    _payload, text = registry.dispatch("watch", client, ctx, [])
    assert client.calls == []                    # no route hit without a binding
    assert "no project" in text.lower()


def test_watch_json_unbound_is_no_project_sentinel(make_ctx):
    client = RouteClient(responses=_responses(run=_run_payload(),
                                              test_runs=_converging_runs()))
    ctx = make_ctx()  # no project bound
    _payload, text = registry.dispatch("watch", client, ctx, [], json_mode=True)
    assert client.calls == []
    assert json.loads(text) == {"_no_project": True}
