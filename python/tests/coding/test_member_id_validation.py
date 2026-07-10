"""A malformed coding team (member missing/duplicate ``id``) must be rejected at
the API boundary with a clear 422 — not crash the run worker thread with an
unhandled ``KeyError: 'id'`` mid-run (found via live testing).

The runner and topology key speaker order / rank on ``m["id"]``
(runner.py ~2833/2839, topology.py ~517); a member that carries e.g.
``member_id`` instead of ``id`` used to sail past run-start and blow up the
worker 0.3s in."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

_TAURI = {"x-errorta-origin": "tauri-ui"}


def _client() -> TestClient:
    from errorta_app.routes import coding as coding_routes
    app = FastAPI()
    app.include_router(coding_routes.router)
    return TestClient(app, headers=_TAURI)


def _new_project(pid: str) -> None:
    from errorta_council.coding.ledger import LedgerStore
    LedgerStore(pid).create_project(north_star="n", definition_of_done="d",
                                    target="new", repo_path=None)


# The malformed member the live test hit: `member_id` instead of `id`.
_BAD = [{"member_id": "m-pm", "enabled": True,
         "gateway_route_id": "r", "provider_kind": "local",
         "metadata": {"coding_role": "pm"}}]

_GOOD = [{"id": "m-pm", "enabled": True, "gateway_route_id": "r",
          "provider_kind": "local", "metadata": {"coding_role": "pm"}}]


def test_run_rejects_member_without_id(tmp_errorta_home: Path) -> None:
    _new_project("v1")
    c = _client()
    # Confirm run-setup with a GOOD team (clears the F121 gate) so /run reaches
    # the id backstop; then /run with a BAD team must 422, not crash the worker.
    assert c.post("/coding/projects/v1/run-setup/confirm",
                  json={"members": _GOOD, "governance_mode": "off"}).status_code == 200
    r = c.post("/coding/projects/v1/run", json={"members": _BAD})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "invalid_member_ids"


def test_confirm_rejects_member_without_id(tmp_errorta_home: Path) -> None:
    _new_project("v2")
    r = _client().post("/coding/projects/v2/run-setup/confirm",
                       json={"members": _BAD})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "invalid_member_ids"


def test_confirm_rejection_does_not_partially_apply_setup(
    tmp_errorta_home: Path,
) -> None:
    from errorta_app import settings
    from errorta_council.coding.autonomy import load_policy
    from errorta_council.coding.governance import GovernanceStore
    from errorta_council.coding.ledger import LedgerStore
    from errorta_council.coding.skills import load_guardrail

    _new_project("v2-atomic")
    store = LedgerStore("v2-atomic")
    before_governance = GovernanceStore.for_ledger(store).load_state().to_dict()
    before_policy = load_policy(store)
    before_guardrail = load_guardrail(store)
    before_preflight = settings.member_health_preflight_enabled()

    r = _client().post("/coding/projects/v2-atomic/run-setup/confirm", json={
        "members": _BAD,
        "governance_mode": "strict",
        "max_iterations": 42,
        "guardrail_enabled": not before_guardrail.enabled,
        "preflight_enabled": not before_preflight,
    })

    assert r.status_code == 422
    after_governance = GovernanceStore.for_ledger(store).load_state().to_dict()
    assert {
        key: value for key, value in after_governance.items() if key != "updated_at"
    } == {
        key: value for key, value in before_governance.items() if key != "updated_at"
    }
    assert load_policy(store) == before_policy
    assert load_guardrail(store) == before_guardrail
    assert settings.member_health_preflight_enabled() is before_preflight
    assert store.get_project().run_setup_confirmed is False
    assert store.get_run_config() == {}


def test_preflight_rejects_member_without_id(tmp_errorta_home: Path) -> None:
    _new_project("v3")
    r = _client().post("/coding/projects/v3/run-setup/preflight",
                       json={"members": _BAD})
    assert r.status_code == 422


def test_rejects_duplicate_member_id(tmp_errorta_home: Path) -> None:
    _new_project("v4")
    dup = [dict(_GOOD[0]), dict(_GOOD[0])]  # same id twice
    r = _client().post("/coding/projects/v4/run-setup/preflight", json={"members": dup})
    assert r.status_code == 422
    assert "Duplicate" in r.json()["detail"]["message"]


def test_wellformed_team_passes_id_validation(tmp_errorta_home: Path,
                                              monkeypatch) -> None:
    """A well-formed team clears the id guard (it fails later on health/preflight
    for a bogus route, not on id validation)."""
    _new_project("v5")
    # preflight probes providers; a well-formed team should get PAST id validation
    # and into preflight_members (which returns unhealthy for the bogus route),
    # i.e. a 200 with an unhealthy list — NOT a 422 for invalid ids.
    r = _client().post("/coding/projects/v5/run-setup/preflight", json={"members": _GOOD})
    assert r.status_code == 200
    assert "unhealthy" in r.json()
