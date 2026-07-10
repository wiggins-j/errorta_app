"""F117-02 — governance settings extension + blocking gate + attention routes."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from errorta_council.coding import attention
from errorta_council.coding.governance import GovernanceState, GovernanceStore
from errorta_council.coding.governance_scheduler import next_governance_action
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.topology import Complete

TAURI = {"x-errorta-origin": "tauri-ui"}


def _blocking_problem(project_id, store, *, stage="drafting_spec"):
    return attention.raise_signal(
        project_id, kind="problem", source="pm", stage=stage,
        title="Pick storage", summary="ambiguous",
        pm_evaluation="The spec is ambiguous on storage.",
        suggestions=[{"id": "s1", "label": "Use a file"}], store=store,
    )


# --- GovernanceState schema round-trip --------------------------------------
def test_governance_state_new_fields_round_trip():
    state = GovernanceState(mode="light", block_on_problems=False,
                            monitor={"no_progress_rounds": 3})
    back = GovernanceState.from_dict(state.to_dict())
    assert back.block_on_problems is False
    assert back.monitor == {"no_progress_rounds": 3}


def test_governance_state_defaults_on_for_legacy(tmp_errorta_home):
    # A pre-F117 state dict (no block_on_problems) must default to on.
    legacy = {"mode": "light", "phase": "idle"}
    assert GovernanceState.from_dict(legacy).block_on_problems is True


# --- blocking gate ----------------------------------------------------------
def test_blocking_gate_stops_then_clears(tmp_errorta_home):
    pid = "gate-proj"
    store = LedgerStore(pid)
    GovernanceStore.for_ledger(store).save_state(
        GovernanceState(mode="light", phase="drafting_spec"))

    sig = _blocking_problem(pid, store)
    action = next_governance_action(store, {"pm": ["m1"]})
    assert isinstance(action, Complete) and action.reason == "blocked_on_problem"

    # Resolve → the gate clears → next action is no longer blocked_on_problem.
    attention.resolve(pid, sig.id, "accept", suggestion_id="s1", store=store)
    action2 = next_governance_action(store, {"pm": ["m1"]})
    assert not (isinstance(action2, Complete)
                and getattr(action2, "reason", "") == "blocked_on_problem")


def test_blocking_gate_off_does_not_stop(tmp_errorta_home):
    pid = "gate-off-proj"
    store = LedgerStore(pid)
    GovernanceStore.for_ledger(store).save_state(
        GovernanceState(mode="light", phase="drafting_spec", block_on_problems=False))
    _blocking_problem(pid, store)
    action = next_governance_action(store, {"pm": ["m1"]})
    assert not (isinstance(action, Complete)
                and getattr(action, "reason", "") == "blocked_on_problem")


# --- routes -----------------------------------------------------------------
@pytest.fixture
def client(tmp_errorta_home):
    from errorta_app.routes import coding as coding_routes
    app = FastAPI()
    app.include_router(coding_routes.router)
    return TestClient(app, headers=TAURI)


def _make_project(client, pid="route-proj", root=None):
    body = {"project_id": pid, "north_star": "n", "definition_of_done": "d",
            "target": "new"}
    if root is not None:
        root.mkdir(parents=True, exist_ok=True)
        body["delivery_root"] = str(root)
    r = client.post("/coding/projects", json=body)
    assert r.status_code == 200, r.text
    return pid


def test_settings_put_round_trips_new_fields(client, tmp_path):
    pid = _make_project(client, "settings-proj", root=tmp_path / "d")
    r = client.put(f"/coding/projects/{pid}/governance/settings",
                   json={"mode": "light", "block_on_problems": False,
                         "monitor": {"no_progress_rounds": 5}})
    assert r.status_code == 200, r.text
    state = r.json()["state"]
    assert state["block_on_problems"] is False
    assert state["monitor"] == {"no_progress_rounds": 5}


def test_attention_routes_list_and_resolve(client, tmp_path):
    pid = _make_project(client, "attn-route-proj", root=tmp_path / "d")
    store = LedgerStore(pid)
    sig = _blocking_problem(pid, store)

    listing = client.get(f"/coding/projects/{pid}/attention?state=open")
    assert listing.status_code == 200
    body = listing.json()
    assert [s["id"] for s in body["signals"]] == [sig.id]

    resolved = client.post(
        f"/coding/projects/{pid}/attention/{sig.id}/resolve",
        json={"action": "accept", "suggestion_id": "s1"})
    assert resolved.status_code == 200
    assert resolved.json()["created_task_id"]
    assert client.get(f"/coding/projects/{pid}/attention?state=open").json()["signals"] == []


def test_resolve_tolerates_double_encoded_body(client, tmp_path):
    # Regression: the desktop webview was observed sending the resolve body
    # JSON-encoded one layer too deep (a JSON string wrapping the JSON object),
    # which a strict typed body param rejected with a 422. The user-facing
    # resolve button must still succeed.
    import json

    pid = _make_project(client, "attn-dbl-proj", root=tmp_path / "d")
    store = LedgerStore(pid)
    sig = _blocking_problem(pid, store)

    doubled = json.dumps(json.dumps({"action": "accept", "suggestion_id": "s1"}))
    resolved = client.post(
        f"/coding/projects/{pid}/attention/{sig.id}/resolve",
        content=doubled,
        headers={**TAURI, "content-type": "application/json"})
    assert resolved.status_code == 200, resolved.text
    assert client.get(
        f"/coding/projects/{pid}/attention?state=open").json()["signals"] == []


def test_resolve_owner_gated_and_errors(client, tmp_path):
    pid = _make_project(client, "attn-gate-proj", root=tmp_path / "d")
    store = LedgerStore(pid)
    sig = _blocking_problem(pid, store)

    # owner gate: no Tauri origin → 403
    no_origin = TestClient(client.app)
    assert no_origin.post(
        f"/coding/projects/{pid}/attention/{sig.id}/resolve",
        json={"action": "accept"}).status_code == 403

    # unknown signal → 404
    assert client.post(
        f"/coding/projects/{pid}/attention/sig-nope/resolve",
        json={"action": "accept"}).status_code == 404

    # illegal action for a problem → 409
    assert client.post(
        f"/coding/projects/{pid}/attention/{sig.id}/resolve",
        json={"action": "dismiss"}).status_code == 409
