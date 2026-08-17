"""Slice 1 §2/§4 — read-only design preflight + the UI-dispatch gate.

Mirrors ``governance_scheduler``: pure, read-only over the ledger, returns a
scheduling action (``DesignPlan``) or ``None``. The whole module is INERT when no
Designer is seated (``cli`` / ``binary`` / ``container`` projects) — that is the
modality gate the spec keys on.

Three questions, one place:
* ``next_design_action`` — is a Designer authoring turn due? (author the contract
  once, immediately, when none exists yet).
* ``design_gate_blocks_ui`` — should UI dev tasks be held? (a Designer is seated and
  the design_spec is not yet approved — spec §2's governance gate).
* ``is_ui_task`` — does a dev task touch UI paths? (so non-UI work is never blocked).
"""
from __future__ import annotations

from typing import Any

from . import paths as _paths
from .topology import DESIGNER, DesignPlan

# UI file extensions: the web set the Slice-2 probe uses, widened with the
# component/style extensions a UI dev task legitimately touches. A dev task whose
# touched paths hit any of these is a "UI task" gated until the design_spec is
# approved; everything else (backend logic, tests, chores) is never blocked.
_UI_EXT = (
    ".html", ".htm", ".css", ".scss", ".sass", ".less",
    ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".vue", ".svelte",
)

# The title of the one DEV task spawned on design_spec approval (§4). Its presence
# is how ``spawn_materialize_task_if_needed`` stays idempotent.
MATERIALIZE_TITLE = "materialize design system"


def _designer_ids(by_role: dict[str, list[str]]) -> list[str]:
    return list(by_role.get(DESIGNER) or [])


def design_seated(by_role: dict[str, list[str]]) -> bool:
    """True iff a Designer is on the team (i.e. a UI-modality project)."""
    return bool(_designer_ids(by_role))


def _governance_for(ledger: Any):
    from .governance import GovernanceStore
    return GovernanceStore.for_ledger(ledger)


def next_design_action(ledger: Any, by_role: dict[str, list[str]]):
    """Return a ``DesignPlan`` when the Designer owes its authoring turn, else None.

    Fires ONLY when a Designer is seated and no design_spec artifact exists yet —
    the "author the contract immediately after charter approval" turn, dispatched
    once. Once a draft exists (any state) this returns None; approval happens inside
    the authoring turn (a valid body is accepted). Read-only + defensive: a store
    hiccup never wedges scheduling."""
    ids = _designer_ids(by_role)
    if not ids:
        return None
    try:
        governance = _governance_for(ledger)
        if governance.latest_artifact("design_spec") is None:
            return DesignPlan(member_id=ids[0])
    except Exception:  # noqa: BLE001 — scheduling must not break on a read error
        return None
    return None


def design_gate_blocks_ui(ledger: Any, by_role: dict[str, list[str]]) -> bool:
    """True iff UI dev dispatch must be held: a Designer is seated and the
    design_spec is not yet approved (spec §2's governance gate). Non-UI tasks are
    unaffected — see ``is_ui_task``. Fail-open (False) on a read error so the gate
    can never wedge the run on a store hiccup."""
    if not _designer_ids(by_role):
        return False
    try:
        governance = _governance_for(ledger)
        return governance.latest_approved_artifact("design_spec") is None
    except Exception:  # noqa: BLE001
        return False


def is_ui_task(task: Any) -> bool:
    """True iff a dev task touches UI paths (declared target_files or prose-inferred).

    Fail-open on silence: a task that declares/implies NO path is NOT treated as UI
    (empty == unknown ownership, not universal — the repo's standing idiom), so the
    gate never over-blocks work whose surface it cannot see. The materialize task
    (§4) only exists AFTER approval, so it is never caught by this gate."""
    touched = _paths.task_touched_paths(task)
    if not touched:
        return False
    return any(p.endswith(_UI_EXT) for p in touched)


__all__ = [
    "MATERIALIZE_TITLE", "design_seated", "next_design_action",
    "design_gate_blocks_ui", "is_ui_task",
]
