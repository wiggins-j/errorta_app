"""F100 - read-only governance preflight for Coding Mode scheduling."""
from __future__ import annotations

from typing import Any

from .governance import GovernanceArtifact, GovernanceStore, required_reviewer_roles
from .topology import (
    PM,
    REVIEWER,
    Complete,
    GovernanceMaterialize,
    GovernancePlan,
    GovernanceReview,
)


def next_governance_action(
    ledger: Any,
    by_role: dict[str, list[str]],
) -> GovernancePlan | GovernanceReview | GovernanceMaterialize | Complete | None:
    """Return the next governance action, or None to use normal task scheduling."""
    governance = GovernanceStore.for_ledger(ledger)
    state = governance.load_state()
    if state.mode == "off":
        return None

    # F117 blocking gate: while an open blocking Problem exists for the current
    # phase (and block_on_problems is on), stop the run with `blocked_on_problem`.
    # This is the single choke point — no scattered update_state sites are
    # touched. The run re-enters via the normal resume path once the signal is
    # resolved (same shape as awaiting_governance_approval below).
    if state.block_on_problems:
        from .attention import blocks_stage
        if blocks_stage(ledger.project_id, state.phase, store=ledger):
            return Complete(reason="blocked_on_problem")

    if state.phase == "complete":
        return Complete(reason="definition_of_done")

    pending = governance.pending_approval()
    if pending is not None:
        return Complete(reason="awaiting_governance_approval")

    pm_id = (by_role.get(PM) or [None])[0]
    reviewer_id = (by_role.get(REVIEWER) or [None])[0]

    def _pm_or_block(phase: str) -> GovernancePlan | Complete:
        if pm_id:
            return GovernancePlan(member_id=pm_id, phase=phase)
        return Complete(reason="no_governance_pm")

    def _next_review_role(artifact: GovernanceArtifact, mode: str) -> str | None:
        """The next reviewer role that still owes an approving review, or None
        when every required reviewer has approved. Reviewer is asked before PM."""
        required = required_reviewer_roles(mode, artifact.artifact_kind)
        by_role = governance.latest_review_by_role(artifact.artifact_id)
        for role in required:
            review = by_role.get(role)
            if review is None or review.verdict != "approved":
                return role
        return None

    def _artifact_step(kind: str, draft_phase: str):
        latest = governance.latest_artifact(kind)
        if latest is None or latest.state in {"changes_requested", "rejected", "draft"}:
            return _pm_or_block(draft_phase)
        if latest.state == "under_review":
            nxt = _next_review_role(latest, state.mode)
            if nxt is None:
                # Defensive: all required reviews approved but state not settled.
                return _pm_or_block(draft_phase)
            if nxt == "pm":
                if pm_id:
                    return GovernanceReview(
                        member_id=pm_id,
                        artifact_id=latest.artifact_id,
                        reviewer_role="pm",
                    )
                return Complete(reason="no_governance_pm")
            if reviewer_id:
                return GovernanceReview(
                    member_id=reviewer_id,
                    artifact_id=latest.artifact_id,
                    reviewer_role="reviewer",
                )
            return Complete(reason="no_governance_reviewer")
        if latest.state == "awaiting_approval":
            # Legacy human-gate state; no longer entered by the artifact flow.
            return Complete(reason="awaiting_governance_approval")
        return _pm_or_block(draft_phase)

    if governance.latest_approved_artifact("brainstorm") is None:
        return _artifact_step("brainstorm", "brainstorming")

    if governance.latest_approved_artifact("spec") is None:
        return _artifact_step("spec", "drafting_spec")

    if governance.latest_approved_artifact("implementation_plan") is None:
        return _artifact_step("implementation_plan", "drafting_plan")

    plan = governance.latest_approved_artifact("implementation_plan")
    if plan is not None:
        planned = governance.plan_slices(plan)
        existing = {
            getattr(t, "source_slice_id", None)
            for t in ledger.list_tasks()
            if getattr(t, "source_plan_artifact_id", None) == plan.artifact_id
        }
        if planned and not {s.slice_id for s in planned}.issubset(existing):
            if pm_id:
                return GovernanceMaterialize(member_id=pm_id)
            return Complete(reason="no_governance_pm")

    return None


__all__ = ["next_governance_action"]
