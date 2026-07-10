"""F121 Part B — the pre-first-run readiness gate ("Run setup").

Covers: the first-run gate flag (start refuses with run_setup_required, no
thread), confirm applies all the existing setters + sets the flag, the sticky
user-level defaults round-trip + second-project pre-fill + stale-route graceful
fallback, and that the gate's preflight blocks on a logged-out required member.
"""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient


def _client(tmp_errorta_home: Path) -> TestClient:
    from errorta_app.server import app

    return TestClient(app, headers={"x-errorta-origin": "tauri-ui"})


def _create(c: TestClient, pid: str) -> None:
    r = c.post(
        "/coding/projects",
        json={"project_id": pid, "north_star": "n", "definition_of_done": "d", "target": "new"},
    )
    assert r.status_code == 200, r.text


_LOCAL_MEMBERS = [
    {"id": "m-pm", "enabled": True, "gateway_route_id": "fake.local.deterministic",
     "provider_kind": "local", "metadata": {"coding_role": "pm"}},
    {"id": "m-dev", "enabled": True, "gateway_route_id": "fake.local.deterministic",
     "provider_kind": "local", "metadata": {"coding_role": "dev"}},
]


# --- B1: first-run gate flag --------------------------------------------------

def test_fresh_project_is_unconfirmed_and_start_refuses_no_thread(
    tmp_errorta_home: Path,
) -> None:
    c = _client(tmp_errorta_home)
    _create(c, "pgate")

    # The project reports the gate as not-yet-confirmed.
    assert c.get("/coding/projects/pgate/run-setup").json()["run_setup_confirmed"] is False
    assert c.get("/coding/projects/pgate").json()["project"]["run_setup_confirmed"] is False

    from errorta_app.routes import coding as coding_routes
    coding_routes._RUNS.pop("pgate", None)

    # A fresh Start refuses with the structured run_setup_required and spawns
    # NO worker thread.
    r = c.post("/coding/projects/pgate/run", json={"members": _LOCAL_MEMBERS})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "run_setup_required"
    assert "pgate" not in coding_routes._RUNS or not coding_routes._thread_alive("pgate")


def test_confirm_then_start_runs(tmp_errorta_home: Path) -> None:
    import time

    c = _client(tmp_errorta_home)
    _create(c, "pconfirm")
    r = c.post("/coding/projects/pconfirm/run-setup/confirm", json={"members": _LOCAL_MEMBERS})
    assert r.status_code == 200, r.text
    assert r.json()["run_setup_confirmed"] is True

    # After confirm, the same fresh start now proceeds.
    r = c.post("/coding/projects/pconfirm/run", json={"members": _LOCAL_MEMBERS})
    assert r.status_code == 200, r.text
    assert r.json()["started"] is True
    for _ in range(50):
        st = c.get("/coding/projects/pconfirm/run").json()
        if not st["running"] and st["result"] is not None:
            break
        time.sleep(0.2)


# --- B4: confirm applies every setter -----------------------------------------

def test_confirm_applies_all_setters_and_sets_flag(tmp_errorta_home: Path) -> None:
    from errorta_council.coding.autonomy import load_policy
    from errorta_council.coding.governance import GovernanceStore
    from errorta_council.coding.ledger import LedgerStore
    from errorta_council.coding.skills import load_guardrail

    c = _client(tmp_errorta_home)
    _create(c, "papply")

    r = c.post("/coding/projects/papply/run-setup/confirm", json={
        "governance_mode": "strict",
        "block_on_problems": False,
        "human_code_approval": "final_only",
        "max_review_rounds": 4,
        "checkpoint_cadence": "every_n_tasks",
        "checkpoint_n": 7,
        "guardrail_enabled": False,
        "max_iterations": 42,
        "max_parallel_workers": 2,
        "member_failure_limit": 5,
        "members": _LOCAL_MEMBERS,
    })
    assert r.status_code == 200, r.text

    store = LedgerStore("papply")
    gov = GovernanceStore.for_ledger(store).load_state().to_dict()
    assert gov["mode"] == "strict"
    assert gov["block_on_problems"] is False
    assert gov["max_review_rounds"] == 4

    pol = load_policy(store)
    assert pol.checkpoint_cadence == "every_n_tasks"
    assert pol.checkpoint_n == 7
    assert pol.max_iterations == 42
    assert pol.max_parallel_workers == 2
    assert pol.member_failure_limit == 5

    assert load_guardrail(store).enabled is False
    assert store.get_project().run_setup_confirmed is True
    # The team is persisted as the run_config so Start uses it.
    assert [m["id"] for m in store.get_run_config()["members"]] == ["m-pm", "m-dev"]


# --- B4: sticky defaults round-trip + second-project pre-fill ------------------

def test_sticky_defaults_round_trip_and_seed_second_project(tmp_errorta_home: Path) -> None:
    from errorta_app import settings

    c = _client(tmp_errorta_home)
    _create(c, "pone")

    # Fresh install: no saved defaults yet.
    assert settings.get_coding_run_defaults() == {}
    assert c.get("/coding/projects/pone/run-setup").json()["defaults"] == {}

    c.post("/coding/projects/pone/run-setup/confirm", json={
        "governance_mode": "strict",
        "max_iterations": 33,
        "checkpoint_cadence": "per_milestone",
        "members": _LOCAL_MEMBERS,
    })

    # The resolved config is persisted as the user-level last-used seed.
    defaults = settings.get_coding_run_defaults()
    assert defaults["governance_mode"] == "strict"
    assert defaults["max_iterations"] == 33

    # A brand-new SECOND project's gate surfaces those defaults for its pre-fill.
    _create(c, "ptwo")
    seed = c.get("/coding/projects/ptwo/run-setup").json()["defaults"]
    assert seed["governance_mode"] == "strict"
    assert seed["max_iterations"] == 33


def test_stale_route_in_saved_defaults_degrades_gracefully(tmp_errorta_home: Path) -> None:
    """A saved default referencing a now-unknown/removed key must not break the
    gate — the unknown key is dropped on load, never raised."""
    from errorta_app import settings

    s = settings.load()
    s["coding_run_defaults"] = {
        "governance_mode": "light",
        "team_room_id": "a-room-that-was-deleted",
        "some_future_unknown_key": {"nested": "blob"},  # must be dropped
    }
    settings.save(s)

    defaults = settings.get_coding_run_defaults()
    assert defaults["governance_mode"] == "light"
    # The carry-over room id round-trips (the auth preflight catches a dead team);
    # the unknown/future-shaped key is silently dropped, not a crash.
    assert defaults["team_room_id"] == "a-room-that-was-deleted"
    assert "some_future_unknown_key" not in defaults


# --- B3: gate preflight blocks on logged-out required member ------------------

def test_run_setup_preflight_reports_unhealthy(tmp_errorta_home: Path, monkeypatch) -> None:
    from errorta_council.coding import member_health

    c = _client(tmp_errorta_home)
    _create(c, "ppf")

    def fake_preflight(members):  # noqa: ANN001
        return [{
            "provider": "claude_cli", "route": "claude_cli.opus",
            "reason": "auth_failed", "detail": "not logged in",
            "remediation": "Run the login command …", "member_ids": ["m-pm"],
        }]

    monkeypatch.setattr(member_health, "preflight_members", fake_preflight)

    members = [
        {"id": "m-pm", "enabled": True, "gateway_route_id": "claude_cli.opus",
         "provider_kind": "remote", "metadata": {"coding_role": "pm"}},
    ]
    r = c.post("/coding/projects/ppf/run-setup/preflight", json={"members": members})
    assert r.status_code == 200, r.text
    unhealthy = r.json()["unhealthy"]
    assert unhealthy and unhealthy[0]["provider"] == "claude_cli"
    assert unhealthy[0]["reason"] == "auth_failed"
    assert "m-pm" in unhealthy[0]["member_ids"]


def test_run_setup_preflight_healthy_local_team_is_empty(tmp_errorta_home: Path) -> None:
    c = _client(tmp_errorta_home)
    _create(c, "ppf2")
    r = c.post("/coding/projects/ppf2/run-setup/preflight", json={"members": _LOCAL_MEMBERS})
    assert r.status_code == 200, r.text
    assert r.json()["unhealthy"] == []
