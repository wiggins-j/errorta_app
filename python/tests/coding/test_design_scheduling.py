"""Slice 1 §2/§4/§9 — design scheduling: authoring turn, the UI-dispatch gate,
materialize-once, and cli-modality inertness.
"""
from __future__ import annotations

from pathlib import Path

from errorta_council.coding import design_materialize
from errorta_council.coding.governance import GovernanceStore
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.topology import (
    Assign,
    DesignPlan,
    decide_next,
)

_UI_TEAM = [("m-pm", "pm"), ("m-dev", "dev"), ("m-designer", "designer")]
_CLI_TEAM = [("m-pm", "pm"), ("m-dev", "dev")]  # no designer (non-UI modality)


def _store(pid: str) -> LedgerStore:
    store = LedgerStore(pid)
    store.create_project(north_star="n", definition_of_done="d",
                         target="new", repo_path=None)
    return store


def _design_body() -> dict:
    return {
        "direction_matrix": {"typography": "humanist sans", "color": "warm",
                             "density": "comfortable", "shape": "rounded",
                             "motion": "subtle", "era_mood": "modern"},
        "tokens": {"palette": {"bg": "#fff"}},
        "assets": {"font_family_ids": ["inter"], "icon_set_id": "lucide"},
        "screens": [{"screen": "home", "purpose": "p", "layout": "l",
                     "hierarchy": "h", "key_states": ["default"]}],
        "components": [{"name": "button", "usage": "u"}],
    }


# --- authoring turn -----------------------------------------------------------

def test_designer_seated_no_spec_schedules_authoring_turn(
        tmp_errorta_home: Path) -> None:
    store = _store("design-author")
    action = decide_next(store, _UI_TEAM)
    assert isinstance(action, DesignPlan)
    assert action.member_id == "m-designer"


def test_designer_authoring_not_rescheduled_once_a_draft_exists(
        tmp_errorta_home: Path) -> None:
    store = _store("design-drafted")
    gov = GovernanceStore.for_ledger(store)
    gov.append_artifact(kind="design_spec", title="d", body_json=_design_body(),
                        state="changes_requested")
    action = decide_next(store, _UI_TEAM)
    assert not isinstance(action, DesignPlan)


# --- the UI-dispatch gate -----------------------------------------------------

def test_ui_dev_task_held_while_unapproved_but_non_ui_dispatched(
        tmp_errorta_home: Path) -> None:
    store = _store("design-gate")
    gov = GovernanceStore.for_ledger(store)
    # A drafted-but-unapproved contract: DesignPlan won't re-fire, gate blocks UI.
    gov.append_artifact(kind="design_spec", title="d", body_json=_design_body(),
                        state="under_review")
    ui = store.add_task(title="Build the landing page", role="dev",
                        target_files=["index.html"])
    backend = store.add_task(title="Add the backend service", role="dev",
                             target_files=["server.py"])
    action = decide_next(store, _UI_TEAM)
    assert isinstance(action, Assign)
    assert action.task_id == backend.task_id, "non-UI task must dispatch"
    assert action.task_id != ui.task_id, "UI task must be held while unapproved"


def test_ui_dev_task_dispatched_once_approved(tmp_errorta_home: Path) -> None:
    store = _store("design-approved")
    gov = GovernanceStore.for_ledger(store)
    gov.append_artifact(kind="design_spec", title="d", body_json=_design_body(),
                        state="approved")
    ui = store.add_task(title="Build the landing page", role="dev",
                        target_files=["index.html"])
    action = decide_next(store, _UI_TEAM)
    assert isinstance(action, Assign)
    assert action.task_id == ui.task_id


# --- cli modality is inert (no designer, no design phase) ---------------------

def test_cli_project_seats_no_designer(tmp_errorta_home: Path) -> None:
    """The team-composition half of the inert path: a cli/binary/container recipe
    seats no Designer at all (spec §1)."""
    from errorta_council.coding import recipes
    from errorta_council.coding.topology import coding_role_of
    routes = [{"route_id": "local.q", "provider_class": "local"}]
    for modality in ("cli", "binary", "container"):
        members = recipes.resolve_team("balanced", routes, modality=modality)
        assert "designer" not in [coding_role_of(m) for m in members], modality


def test_cli_project_has_no_design_phase(tmp_errorta_home: Path) -> None:
    """The scheduling half of the inert path: with no Designer seated, the design
    preflight schedules nothing, the UI-dispatch gate never engages, and a UI dev
    task is dispatched exactly as a pre-Designer run would — even if a design_spec
    artifact somehow existed. Every design path in the spec is inert."""
    from errorta_council.coding.design_scheduler import (
        design_gate_blocks_ui,
        next_design_action,
    )
    store = _store("design-cli")
    ui = store.add_task(title="Build the landing page", role="dev",
                        target_files=["index.html"])
    by_role = {"pm": ["m-pm"], "dev": ["m-dev"]}  # no designer

    # The design phase and gate are provably inert (not just "no designer in team").
    assert next_design_action(store, by_role) is None
    assert design_gate_blocks_ui(store, by_role) is False

    # No DesignPlan is ever scheduled, and the UI task is dispatched normally.
    action = decide_next(store, _CLI_TEAM)
    assert not isinstance(action, DesignPlan)
    assert isinstance(action, Assign)
    assert action.task_id == ui.task_id


# --- materialize spawns exactly once on approval ------------------------------

def test_materialize_spawns_exactly_once(tmp_errorta_home: Path) -> None:
    store = _store("design-materialize")
    gov = GovernanceStore.for_ledger(store)
    gov.append_artifact(kind="design_spec", title="d", body_json=_design_body(),
                        state="approved")
    assert design_materialize.spawn_materialize_task_if_needed(store, gov) is True
    # idempotent: a second approval turn creates nothing more.
    assert design_materialize.spawn_materialize_task_if_needed(store, gov) is False
    materialize = [t for t in store.list_tasks()
                   if t.title.strip().lower() == "materialize design system"]
    assert len(materialize) == 1
    assert materialize[0].role == "dev"


def test_materialize_noop_when_not_approved(tmp_errorta_home: Path) -> None:
    store = _store("design-materialize-noop")
    gov = GovernanceStore.for_ledger(store)
    gov.append_artifact(kind="design_spec", title="d", body_json=_design_body(),
                        state="under_review")
    assert design_materialize.spawn_materialize_task_if_needed(store, gov) is False
    assert not [t for t in store.list_tasks()
                if t.title.strip().lower() == "materialize design system"]
