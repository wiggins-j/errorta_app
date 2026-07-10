"""F100-01 — plain-language governance status projection.

A pure, read-only projection of the F100 governance state into a glanceable
**stage + status** for the UI's Governance Status panel — mirroring
``team_log.build_team_log``. No new ``Task`` rows, no state writes, no model
involvement, no egress (council-side, like ``team_log``/``governance``).

The projection maps the internal governance ``phase`` + the active artifact's
resolved review state into a user-facing **stage** (Brainstorm → Spec → Plan →
Build → Done) and **status** (drafting / under review / changes requested /
approved / building), plus the dual-review actor (reviewer pass → PM pass) and a
stepper over the whole arc. Off mode returns ``mode:"off"`` (panel hidden); light
mode omits the brainstorm review sub-states (brainstorm auto-approved).
"""
from __future__ import annotations

from typing import Any

from .governance import GovernanceStore, required_reviewer_roles
from .topology import PM

# Stage order (the stepper) and the artifact kind that backs each reviewable
# stage. ``build``/``done`` have no governance artifact.
_STAGES = ("brainstorm", "spec", "plan", "build", "done")

# Stage -> the governance artifact_kind whose active id lives in
# ``active_artifact_ids`` (note: plan's artifact kind is "implementation_plan").
_STAGE_KIND = {
    "brainstorm": "brainstorm",
    "spec": "spec",
    "plan": "implementation_plan",
}

# Internal phase -> user-facing stage.
_PHASE_STAGE = {
    "idle": "idle",
    "brainstorming": "brainstorm",
    "reviewing_brainstorm": "brainstorm",
    "awaiting_brainstorm_approval": "brainstorm",
    "drafting_spec": "spec",
    "reviewing_spec": "spec",
    "awaiting_spec_approval": "spec",
    "drafting_plan": "plan",
    "reviewing_plan": "plan",
    "awaiting_plan_approval": "plan",
    "development": "build",
    "awaiting_slice_approval": "build",
    "awaiting_final_approval": "build",
    "complete": "done",
}

# Drafting/revising phases (the PM is writing) -> status "drafting".
_DRAFTING_PHASES = {"brainstorming", "drafting_spec", "drafting_plan"}
# Reviewing phases -> read the active artifact's resolved review state.
_REVIEWING_PHASES = {"reviewing_brainstorm", "reviewing_spec", "reviewing_plan"}

_STAGE_LABEL = {
    "brainstorm": "Brainstorm",
    "spec": "Spec",
    "plan": "Plan",
    "build": "Build",
    "done": "Done",
    "idle": "Getting started",
}

_STATUS_LABEL = {
    "drafting": "drafting",
    "under_review": "under review",
    "changes_requested": "changes requested",
    "approved": "approved",
    "building": "Building",
}


def _resolved_artifact_state(gov: GovernanceStore, artifact_id: str, mode: str,
                             kind: str) -> str:
    """The resolved review state of an artifact without mutating anything.

    Mirrors ``GovernanceStore.settle_artifact_after_review``'s decision (read-only):
    ``changes_requested`` if any latest review rejected; ``approved`` if every
    required reviewer approved; else ``under_review``.
    """
    by_role = gov.latest_review_by_role(artifact_id)
    if any(r.verdict != "approved" for r in by_role.values()):
        return "changes_requested"
    required = required_reviewer_roles(mode, kind)
    if required and all(
        role in by_role and by_role[role].verdict == "approved" for role in required
    ):
        return "approved"
    return "under_review"


def _next_review_role(gov: GovernanceStore, artifact_id: str, mode: str,
                      kind: str) -> str | None:
    """The next required reviewer role that hasn't approved yet (reviewer before
    PM), or None when every required reviewer has approved."""
    by_role = gov.latest_review_by_role(artifact_id)
    for role in required_reviewer_roles(mode, kind):
        review = by_role.get(role)
        if review is None or review.verdict != "approved":
            return role
    return None


def _label_for_role(by_role: dict[str, Any], role: str) -> tuple[str | None, str | None]:
    """(member_id, display_label) for the member playing ``role``, or (None, fallback).

    ``by_role`` is role -> list of member dicts (the ``members_by_coding_role``
    shape: ``{"id": ..., "name": ..., "metadata": {"coding_role": ...}}``). The PM
    falls back to the label "PM" when no member is resolvable.
    """
    members = by_role.get(role) or []
    member = members[0] if members else None
    if member is None:
        return (None, "PM" if role == PM else None)
    member_id = str(member.get("id") or "") or None
    label = str(member.get("name") or member.get("id") or "") or None
    if role == PM and label is None:
        label = "PM"
    return (member_id, label)


def _stepper(stage: str, status: str | None, mode: str) -> list[dict[str, str]]:
    """One entry per stage. Stages before the current one are ``approved``
    (checked), the current stage carries its live sub-state, future stages are
    ``pending``. In light mode the brainstorm step has no review sub-state
    (drafting -> approved)."""
    steps: list[dict[str, str]] = []
    if stage == "idle":
        return [{"stage": s, "state": "pending"} for s in _STAGES]
    current_index = _STAGES.index(stage) if stage in _STAGES else len(_STAGES)
    for i, s in enumerate(_STAGES):
        if i < current_index:
            state = "approved"
        elif i == current_index:
            if stage == "done":
                state = "approved"
            elif stage == "build":
                state = "building"
            elif mode == "light" and s == "brainstorm":
                # Light mode never reviews the brainstorm — show it advancing
                # straight from drafting to approved, no under_review sub-state.
                state = "approved" if status == "approved" else "drafting"
            else:
                state = status or "drafting"
        else:
            state = "pending"
        steps.append({"stage": s, "state": state})
    return steps


def governance_status(store: Any, by_role: dict[str, Any], *,
                      run_active: bool) -> dict[str, Any]:
    """Project the governance state into a plain-language stage + status.

    ``store`` is the project ``LedgerStore`` (same as ``build_team_log``);
    ``by_role`` is the role -> member-dicts roster (``members_by_coding_role``
    shape); ``run_active`` is whether a run is currently driving the project.
    Fully guarded — a missing/empty governance store degrades to an idle status.
    """
    gov = GovernanceStore.for_ledger(store)
    state = gov.load_state()
    mode = state.mode
    phase = state.phase

    if mode == "off":
        return {
            "mode": "off",
            "stage": "idle",
            "status": None,
            "headline": "",
            "actor_member_id": None,
            "actor_label": None,
            "review_pass": None,
            "steps": [],
            "build_progress": None,
        }

    stage = _PHASE_STAGE.get(phase, "idle")
    status: str | None = None
    actor_member_id: str | None = None
    actor_label: str | None = None
    review_pass: str | None = None
    # F100-02 A1 (RC6): additive "stuck / needs you" surface. Set when a review
    # stage has hit the round cap with the artifact still in changes_requested.
    needs_human = False
    review_round: int | None = None

    if stage == "idle":
        status = None
    elif stage == "done":
        status = None
    elif stage == "build":
        status = "building"
    elif phase in _DRAFTING_PHASES:
        # The PM is writing the artifact. RC6: after a rejected settle the phase
        # flips to the revision (drafting) phase — that's where a stuck run lands
        # (the hard_blocker stop fires right after the settle). Detect the
        # not-converging stuck state here so the panel shows "needs you".
        status = "drafting"
        actor_member_id, actor_label = _label_for_role(by_role, PM)
        kind = _STAGE_KIND.get(stage)
        if kind:
            latest = gov.latest_artifact(kind)
            if (
                latest is not None
                and _resolved_artifact_state(gov, latest.artifact_id, mode, kind)
                == "changes_requested"
            ):
                rounds = gov.review_round_count(kind)
                if rounds >= state.max_review_rounds:
                    needs_human = True
                    review_round = rounds
                    status = "stuck"
                    actor_member_id, actor_label = (None, None)
    elif phase in _REVIEWING_PHASES:
        kind = _STAGE_KIND.get(stage)
        artifact_id = state.active_artifact_ids.get(kind or "") if kind else None
        if not artifact_id:
            # Defensive: in a reviewing phase but no active artifact recorded.
            status = "drafting"
            actor_member_id, actor_label = _label_for_role(by_role, PM)
        else:
            status = _resolved_artifact_state(gov, artifact_id, mode, kind)
            if status == "under_review":
                review_pass = _next_review_role(gov, artifact_id, mode, kind)
                if review_pass is not None:
                    actor_member_id, actor_label = _label_for_role(
                        by_role, review_pass)
            elif status == "changes_requested":
                # PM is revising.
                actor_member_id, actor_label = _label_for_role(by_role, PM)
                # RC6: at/over the cap, the loop is stuck and awaiting the human.
                rounds = gov.review_round_count(kind)
                if rounds >= state.max_review_rounds:
                    needs_human = True
                    review_round = rounds
                    status = "stuck"
                    actor_member_id, actor_label = (None, None)
            # approved -> no live actor (advancing)

    # Headline: "<Stage> — <Status label>", with build/done/idle specials.
    if stage == "idle":
        headline = _STAGE_LABEL["idle"]
    elif stage == "done":
        headline = _STAGE_LABEL["done"]
    elif stage == "build":
        headline = _STATUS_LABEL["building"]
    elif needs_human and review_round is not None:
        # RC6: stuck "needs you" headline with the round count.
        headline = (
            f"{_STAGE_LABEL.get(stage, stage)} — needs you · "
            f"stuck after {review_round} rounds"
        )
    elif status is not None:
        headline = f"{_STAGE_LABEL.get(stage, stage)} — {_STATUS_LABEL.get(status, status)}"
    else:
        headline = _STAGE_LABEL.get(stage, stage)

    build_progress: dict[str, int] | None = None
    if stage == "build":
        try:
            tasks = list(store.list_tasks())
            total = sum(1 for t in tasks if getattr(t, "state", "") != "dropped")
            done = sum(1 for t in tasks if getattr(t, "state", "") == "done")
            build_progress = {"done": done, "total": total}
        except Exception:
            build_progress = None

    return {
        "mode": mode,
        "stage": stage,
        "status": status,
        "headline": headline,
        "actor_member_id": actor_member_id,
        "actor_label": actor_label,
        "review_pass": review_pass,
        "needs_human": needs_human,
        "review_round": review_round,
        "steps": _stepper(stage, status, mode),
        "build_progress": build_progress,
    }


__all__ = ["governance_status"]
