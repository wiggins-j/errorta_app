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

    **Deliberately no longer a byte-for-byte mirror of ``wizard_create``.**
    Between steps 2 and 3 this seeds the project's first ``Focus`` from
    ``charter["initial_goal"]``, or the north star verbatim when that optional
    field is absent. ``wizard_create`` does not, because the desktop wizard hands
    off to a UI where the operator sets the goal before starting; a charter
    arriving from Slack has no such step, and without a Focus
    ``next_goal.start_gate`` refuses the project's first run — making a
    studio-created project impossible to start. Do not "restore the mirror" by
    deleting the seed; restore parity by teaching the wizard to seed too.

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
    # REQUIRED_CHARTER_FIELDS mirrors wizard.py's tuple verbatim (kept identical
    # so it stays a faithful mirror of the canonical source), which doesn't cover
    # team_recipe/autonomous — wizard.finalize() enforces those separately via
    # _compute_missing() before it ever hands back a charter. This function
    # dereferences both below (charter["team_recipe"], charter["autonomous"])
    # AFTER writing the project + workspace to disk, so they must be validated
    # here too, up front, or a caller-supplied charter missing either one leaves
    # an orphaned project/workspace behind when the KeyError fires.
    if not charter.get("team_recipe"):
        raise ValueError("charter missing required field: 'team_recipe'")
    # `autonomous` is a real yes/no choice and False is a legitimate answer, so
    # check presence (like wizard's _is_explicit_bool), not truthiness.
    if "autonomous" not in charter or charter.get("autonomous") is None:
        raise ValueError("charter missing required field: 'autonomous'")

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

    # Seed the project's first Focus. Without it `next_goal.start_gate` refuses
    # the project's very first start -- it requires an active Focus or a legacy
    # `work_request`, and a charter-created project had neither -- so a project
    # created from Slack could never be started at all.
    #
    # A Focus, not `work_request`: `runner._pm_prompt` scopes planning by
    # `active_focuses()` and reaches `work_request` only as a documented legacy
    # fallback. Writing focus.jsonl here also makes `_ensure_focus_migrated` a
    # permanent no-op for this project, so there is no double-seed.
    #
    # The gate is NOT disarmed by this. When the seeded Focus is completed and
    # archived, `active_focuses()` empties and the gate fires again on the next
    # fresh start -- on a project that by then has finished work and a possibly
    # stale charter, which is the case the gate was written for.
    #
    # `initial_goal` is optional and operator-supplied; the fallback copies the
    # north star VERBATIM. A code-level copy is not an invention, whereas a
    # generated "first increment" would be.
    goal = str(charter.get("initial_goal") or charter["north_star"]).strip()
    if goal:
        store.add_focus(title=goal, body="", origin="studio_charter")

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
        else pm_reference.list_available_routes(),
        # Slice 1 §1: seat a Designer only for UI modalities. The charter's modality
        # is the single input to that gate.
        modality=str(charter.get("modality") or "") or None)

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
