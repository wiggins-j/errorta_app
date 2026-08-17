"""Reusable, non-Tauri engine-side helper: create a runnable-by-construction
Errorta coding project from a charter dict.

This is the composition currently expressed only inside the Tauri-gated
``wizard_create()`` route (``errorta_app/routes/coding.py``). It exists so
other origins — the Slack studio manager being the first — can create a fully
set-up project (ledger + workspace + governance + autonomy + team) without
going through that HTTP route or its origin gate.

Deliberately plain ``errorta_council.coding`` library code: no import from
``errorta_app.routes.*`` or ``errorta_slack``, and no Slack dependency. Heavy
imports are done inside the function to keep this module cheap to import.
"""
from __future__ import annotations

from typing import Any

from .ledger import Project

REQUIRED_CHARTER_FIELDS = (
    "north_star", "audience", "modality", "definition_of_done", "entrypoint")


def create_project_from_charter(
    project_id: str,
    charter: dict[str, Any],
    *,
    delivery_root: str | None = None,
    available_routes: list[dict[str, Any]] | None = None,
    members: list[dict[str, Any]] | None = None,
) -> Project:
    """Create a fully set-up, runnable project from a charter dict.

    Mirrors ``wizard_create()`` steps 1-5 (routes/coding.py, ~2103-2132):

      1. ``LedgerStore(project_id).create_project(...)``
      2. ``CodingWorkspace(project_id, store).setup(target="new", repo_path=None)``
      3. Seed an approved ``brainstorm`` governance artifact whose ``body_json``
         is the charter, then apply the recipe's governance overrides.
      4. Merge the recipe's autonomy overrides into the project's autonomy policy.
      5. Assign a team: the explicit ``members`` if given, else
         ``recipes.resolve_team(charter["team_recipe"], available_routes)``. If a
         team is non-empty, set the run config and mark run-setup confirmed
         (replicating the route-private ``_set_run_setup_confirmed`` effect). If
         no team is assignable, the project is left idle without one — this is
         not an error.

    ``available_routes`` is injectable so callers (and tests) can avoid the real
    ``pm_reference.list_available_routes()``, which can shell out to a CLI or
    probe a local model gateway. It defaults to that live catalog only when not
    injected.

    Raises ``ValueError`` naming the first missing required charter field, or
    whatever ``LedgerStore`` raises (``LedgerError``) on an unsafe project_id.
    """
    for field_name in REQUIRED_CHARTER_FIELDS:
        if not charter.get(field_name):
            raise ValueError(f"charter missing required field: {field_name!r}")

    from . import pm_reference, recipes
    from .autonomy import load_policy, policy_from_dict, policy_to_dict, save_policy
    from .governance import GovernanceStore
    from .ledger import LedgerStore, _atomic_write_json, _now
    from .workspace import CodingWorkspace

    store = LedgerStore(project_id)

    store.create_project(
        north_star=charter["north_star"],
        definition_of_done=charter["definition_of_done"],
        target="new", repo_path=None, delivery_root=delivery_root)
    CodingWorkspace(project_id, store).setup(target="new", repo_path=None)

    recipe, autonomous = charter["team_recipe"], charter["autonomous"]

    gov = GovernanceStore.for_ledger(store)
    gov.append_artifact(
        kind="brainstorm", title=f"Studio charter — {charter['north_star'][:60]}",
        body_markdown=charter.get("scope_notes", ""), body_json=charter,
        state="approved", author={"role": "pm", "id": "studio"})
    gov.update_state(**recipes.governance_overrides(recipe, autonomous=autonomous))

    merged = {**policy_to_dict(load_policy(store)),
              **recipes.autonomy_overrides(recipe, autonomous=autonomous)}
    save_policy(store, policy_from_dict(merged))

    resolved_members = members if members is not None else recipes.resolve_team(
        recipe, available_routes if available_routes is not None
        else pm_reference.list_available_routes())

    if resolved_members:
        store.set_run_config(room_id=None, members=resolved_members)
        # Replicates the route-private _set_run_setup_confirmed(store, True)
        # effect: persist run_setup_confirmed on the project record, round-
        # tripping the rest of the record + _extras verbatim.
        raw = store.get_project().to_dict()
        raw["run_setup_confirmed"] = True
        raw["updated_at"] = _now()
        _atomic_write_json(store._project_path, raw)

    return store.get_project()
