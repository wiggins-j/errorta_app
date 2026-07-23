"""``gate`` — acceptance/test gate status (Spec 03).

The CLI is a pure sidecar *client*, so these exercise the command through the
shared registry against a path-aware fake client that serves a canned
``/test-runs`` payload shaped exactly like the route serializer
(``coding.py`` ``get_test_runs`` → ``{"runs": LedgerStore.list_test_runs()}``,
each run mirroring ``LedgerStore.record_test_run``).
"""
from __future__ import annotations

import json

from errorta_cli import registry

from .conftest import RouteClient

PID = "proj-1"


def _run(*, exit_codes: list[int], passed: bool, head: str, at: str, sandbox: str) -> dict:
    """One recorded test run, mirroring ``record_test_run`` / ``TestRunResult``."""
    results = [
        {"command_id": f"cmd-{i}", "status": "completed" if code == 0 else "failed",
         "exit_code": code, "passed": code == 0}
        for i, code in enumerate(exit_codes)
    ]
    return {
        "test_run_id": f"tr-{head}", "task_id": "t-1",
        "command_ids": [r["command_id"] for r in results],
        "unknown_ids": [], "passed": passed, "results": results,
        "head": head, "sandbox": sandbox, "at": at,
    }


def _two_runs_payload() -> dict:
    """Two runs: 5/12 (fail) then 9/12 (pass)."""
    first = _run(exit_codes=[0] * 5 + [1] * 7, passed=False, head="aaaaaaaa1111",
                 at="2026-07-22T10:00:00", sandbox="seatbelt")
    second = _run(exit_codes=[0] * 9 + [1] * 3, passed=True, head="bbbbbbbb2222",
                  at="2026-07-22T11:30:00", sandbox="seatbelt")
    return {"runs": [first, second]}


def test_gate_hits_test_runs_route(make_ctx):
    client = RouteClient(responses={"/test-runs": _two_runs_payload()})
    ctx = make_ctx(project_id=PID)
    _payload, text = registry.dispatch("gate", client, ctx, [])
    assert any(f"/coding/projects/{PID}/test-runs" in p for p in client.paths())
    assert isinstance(text, str) and text != ""


def test_gate_shows_latest_verdict_trend_and_failing(make_ctx):
    client = RouteClient(responses={"/test-runs": _two_runs_payload()})
    ctx = make_ctx(project_id=PID)
    _payload, text = registry.dispatch("gate", client, ctx, [])
    # Latest verdict is the second run: PASS 9/12.
    assert "PASS 9/12" in text, text
    # Provenance of the latest run.
    assert "bbbbbbbb" in text, text          # short head sha
    assert "11:30:00" in text, text          # timestamp
    assert "seatbelt" in text, text          # sandbox
    # Trend carries BOTH runs so a stuck gate would be visible.
    assert "5/12 → 9/12" in text, text
    # The latest run's failing command ids + exit codes are listed.
    assert "cmd-9" in text and "exit 1" in text, text


def test_gate_json_returns_raw_payload(make_ctx):
    payload = _two_runs_payload()
    client = RouteClient(responses={"/test-runs": payload})
    ctx = make_ctx(project_id=PID)
    _payload, text = registry.dispatch("gate", client, ctx, [], json_mode=True)
    parsed = json.loads(text)
    assert parsed == payload  # raw /test-runs payload, unfiltered


def test_gate_no_runs_recorded(make_ctx):
    client = RouteClient(responses={"/test-runs": {"runs": []}})
    ctx = make_ctx(project_id=PID)
    _payload, text = registry.dispatch("gate", client, ctx, [])
    assert "no gate runs recorded" in text


def test_gate_unbound_project_is_no_project_path(make_ctx):
    client = RouteClient(responses={"/test-runs": _two_runs_payload()})
    ctx = make_ctx()  # no project bound
    _payload, text = registry.dispatch("gate", client, ctx, [])
    assert client.calls == []              # no route hit without a binding
    assert "no project" in text.lower()


def test_gate_json_unbound_is_no_project_sentinel(make_ctx):
    client = RouteClient(responses={"/test-runs": _two_runs_payload()})
    ctx = make_ctx()  # no project bound
    payload, text = registry.dispatch("gate", client, ctx, [], json_mode=True)
    assert client.calls == []
    assert json.loads(text) == {"_no_project": True}
