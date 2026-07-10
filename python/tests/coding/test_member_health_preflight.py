"""F120-04 — pre-run preflight (criterion #6).

Before the first turn, each DISTINCT CLI/subscription route is probed once. A
logged-out provider refuses the run start with a structured unhealthy list and
spawns NO worker thread — instead of starting and looping for minutes. The
preflight is config-gated (default on); turning it off lets the run start (then
F120-02's in-loop accounting catches the failure).
"""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from errorta_app import settings
from errorta_council.coding import member_health


def _client(tmp_errorta_home: Path) -> TestClient:
    from errorta_app.server import app
    return TestClient(app, headers={"x-errorta-origin": "tauri-ui"})


def _create(c: TestClient, pid: str) -> None:
    r = c.post("/coding/projects", json={
        "project_id": pid, "north_star": "n", "definition_of_done": "d",
        "target": "new"})
    assert r.status_code in (200, 201), r.text
    # F121: these tests exercise the run-start mechanics directly, past the
    # readiness gate. Confirm setup so the belt-and-suspenders run_setup_required
    # guard doesn't pre-empt the preflight under test.
    _confirm_run_setup(pid)


def _confirm_run_setup(pid: str) -> None:
    from errorta_app.routes.coding import _set_run_setup_confirmed
    from errorta_council.coding.ledger import LedgerStore
    _set_run_setup_confirmed(LedgerStore(pid), True)


_CLAUDE_MEMBERS = [
    {"id": "m-1", "enabled": True, "role": "pm",
     "gateway_route_id": "claude_cli.opus", "provider_kind": "remote"},
    {"id": "m-2", "enabled": True, "role": "dev",
     "gateway_route_id": "claude_cli.opus", "provider_kind": "remote"},
]


def test_logged_out_route_refuses_start_no_thread(
    tmp_errorta_home: Path, monkeypatch,
) -> None:
    c = _client(tmp_errorta_home)
    _create(c, "pf-out")

    def fake_preflight(members):  # noqa: ANN001
        return [{
            "provider": "claude_cli", "route": "claude_cli.opus",
            "reason": "auth_failed", "detail": "not logged in",
            "remediation": "Run the login command for this provider …",
            "member_ids": ["m-1", "m-2"],
        }]

    monkeypatch.setattr(member_health, "preflight_members", fake_preflight)

    # No worker thread should be registered for a refused start.
    from errorta_app.routes import coding as coding_routes
    coding_routes._RUNS.pop("pf-out", None)

    r = c.post("/coding/projects/pf-out/run", json={"members": _CLAUDE_MEMBERS})
    assert r.status_code == 409, r.text
    body = r.json()["detail"]
    assert body["code"] == "member_health_preflight_failed"
    assert body["unhealthy"][0]["provider"] == "claude_cli"
    assert body["unhealthy"][0]["reason"] == "auth_failed"
    assert "m-1" in body["unhealthy"][0]["member_ids"]
    # No thread spawned.
    assert "pf-out" not in coding_routes._RUNS or not coding_routes._thread_alive("pf-out")


def test_healthy_room_starts(tmp_errorta_home: Path, monkeypatch) -> None:
    c = _client(tmp_errorta_home)
    _create(c, "pf-ok")
    monkeypatch.setattr(member_health, "preflight_members", lambda members: [])
    r = c.post("/coding/projects/pf-ok/run", json={"members": _CLAUDE_MEMBERS})
    assert r.status_code == 200, r.text
    assert r.json().get("started") is True


def test_preflight_off_starts_even_if_unhealthy(
    tmp_errorta_home: Path, monkeypatch,
) -> None:
    c = _client(tmp_errorta_home)
    _create(c, "pf-off")
    # Turn preflight OFF — the probe must not even run, so the start proceeds.
    monkeypatch.setattr(settings, "member_health_preflight_enabled", lambda: False)

    def boom(members):  # noqa: ANN001 — must NOT be called when preflight is off
        raise AssertionError("preflight ran while disabled")

    monkeypatch.setattr(member_health, "preflight_members", boom)
    r = c.post("/coding/projects/pf-off/run", json={"members": _CLAUDE_MEMBERS})
    assert r.status_code == 200, r.text
    assert r.json().get("started") is True


def test_already_running_short_circuits_before_preflight(
    tmp_errorta_home: Path, monkeypatch,
) -> None:
    c = _client(tmp_errorta_home)
    _create(c, "pf-running")

    def boom(members):  # noqa: ANN001 — must NOT be called for an active run
        raise AssertionError("preflight ran before the liveness guard")

    from errorta_app.routes import coding as coding_routes
    monkeypatch.setattr(member_health, "preflight_members", boom)
    monkeypatch.setattr(coding_routes, "_thread_alive", lambda project_id: True)

    r = c.post("/coding/projects/pf-running/run", json={"members": _CLAUDE_MEMBERS})
    assert r.status_code == 409
    assert r.json()["detail"] == "a run is already in progress"


def test_non_cli_routes_are_not_preflighted(tmp_errorta_home: Path) -> None:
    """A room of only local routes has no preflightable provider, so the REAL
    preflight returns [] and the start proceeds (no probe cost)."""
    c = _client(tmp_errorta_home)
    _create(c, "pf-local")
    local_members = [
        {"id": "m-1", "enabled": True, "role": "pm",
         "gateway_route_id": "fake.local.deterministic", "provider_kind": "local"},
        {"id": "m-2", "enabled": True, "role": "dev",
         "gateway_route_id": "fake.local.deterministic", "provider_kind": "local"},
    ]
    # No monkeypatch — exercise the real preflight_members against local routes.
    assert member_health.preflight_members(local_members) == []
    r = c.post("/coding/projects/pf-local/run", json={"members": local_members})
    assert r.status_code == 200, r.text
