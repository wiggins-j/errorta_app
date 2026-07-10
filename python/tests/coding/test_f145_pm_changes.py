"""F145 Slice 3 — PM Changes consent: apply -> review -> accept keeps / decline reverts."""
from __future__ import annotations

from pathlib import Path

import pytest

from errorta_council.coding import pm_changes as pmc
from errorta_council.coding.autonomy import (
    load_policy,
    policy_from_dict,
    policy_to_dict,
    save_policy,
)
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.workspace import CodingWorkspace


def _project(pid: str) -> LedgerStore:
    store = LedgerStore(pid)
    store.create_project(north_star="n", definition_of_done="d",
                         target="new", repo_path=None)
    CodingWorkspace(pid, store).setup(target="new", repo_path=None)
    return store


def _apply_autonomy(store, **overrides):
    before = {k: policy_to_dict(load_policy(store))[k] for k in overrides}
    merged = {**policy_to_dict(load_policy(store)), **overrides}
    save_policy(store, policy_from_dict(merged))
    return before


def test_decline_reverts_autonomy(tmp_errorta_home: Path):
    store = _project("pmc1")
    assert policy_to_dict(load_policy(store))["checkpoint_cadence"] == "per_milestone"
    before = _apply_autonomy(store, checkpoint_cadence="off", max_parallel_workers=1)
    ch = pmc.record_change(
        store, summary="Go autonomous", details=[{"field": "checkpoint_cadence",
        "before": "per_milestone", "after": "off"}],
        restore_target="autonomy", restore_value=before,
        autonomy={"warning": True, "suggested_cap": None})
    # applied
    assert policy_to_dict(load_policy(store))["checkpoint_cadence"] == "off"
    # decline reverts exactly
    resolved = pmc.decline(store, ch.change_id)
    assert resolved.status == "declined"
    assert policy_to_dict(load_policy(store))["checkpoint_cadence"] == "per_milestone"
    assert policy_to_dict(load_policy(store))["max_parallel_workers"] is None


def test_decline_reverts_governance(tmp_errorta_home: Path):
    from errorta_council.coding.governance import GovernanceStore

    store = _project("pmc-gov")
    gov = GovernanceStore.for_ledger(store)
    assert gov.load_state().block_on_problems is True
    gov.update_state(mode="light", block_on_problems=False)
    ch = pmc.record_change(store, summary="hands-off", details=[],
                           restore_target="governance",
                           restore_value={"block_on_problems": True})
    pmc.decline(store, ch.change_id)
    assert gov.load_state().block_on_problems is True


def test_accept_keeps(tmp_errorta_home: Path):
    store = _project("pmc2")
    before = _apply_autonomy(store, max_parallel_workers=1)
    ch = pmc.record_change(store, summary="Sequential",
                           details=[{"field": "max_parallel_workers", "before": None, "after": 1}],
                           restore_target="autonomy", restore_value=before)
    resolved = pmc.accept(store, ch.change_id)
    assert resolved.status == "accepted"
    assert policy_to_dict(load_policy(store))["max_parallel_workers"] == 1


def test_decline_reverts_team(tmp_errorta_home: Path):
    store = _project("pmc3")
    store.set_run_config(room_id="r0", members=[{"id": "a", "gateway_route_id": "local.x"}])
    before = {"room_id": "r0", "members": [{"id": "a", "gateway_route_id": "local.x"}]}
    store.set_run_config(room_id="r1", members=[{"id": "b", "gateway_route_id": "anthropic.y"}])
    ch = pmc.record_change(store, summary="reassign", details=[],
                           restore_target="run_config", restore_value=before)
    pmc.decline(store, ch.change_id)
    cfg = store.get_run_config()
    assert cfg["members"] == [{"id": "a", "gateway_route_id": "local.x"}]


def test_double_resolve_is_idempotent(tmp_errorta_home: Path):
    store = _project("pmc4")
    before = _apply_autonomy(store, max_iterations=10)
    ch = pmc.record_change(store, summary="x", details=[],
                           restore_target="autonomy", restore_value=before)
    pmc.accept(store, ch.change_id)
    # a second decline after accept is a no-op (status already resolved)
    again = pmc.decline(store, ch.change_id)
    assert again.status == "accepted"
    assert policy_to_dict(load_policy(store))["max_iterations"] == 10


def test_surface_and_validation(tmp_errorta_home: Path):
    store = _project("pmc5")
    with pytest.raises(pmc.PmChangeError):
        pmc.record_change(store, summary="x", details=[],
                          restore_target="bogus", restore_value={})
    ch = pmc.record_change(store, summary="logged", details=[],
                           restore_target="autonomy", restore_value={}, surface="log")
    assert ch.surface == "log"
    assert [c.change_id for c in pmc.list_changes(store, status="pending")] == [ch.change_id]
