"""F100 PR-A — lock the redefined off/light/strict artifact governance modes.

* off    — no governance reviews at all (scheduler returns None).
* light  — the reviewer reviews spec + plan (NOT brainstorm); auto-accept on
           approval; no human gate.
* strict — the reviewer AND the PM each review every brainstorm/spec/plan; both
           must approve before the artifact advances; no human gate.

These tests drive the store + scheduler (the runner's review handler is exercised
indirectly via ``settle_artifact_after_review``, which is exactly what the runner
calls after appending a review).
"""
from __future__ import annotations

from pathlib import Path

from errorta_council.coding.governance import GovernanceStore
from errorta_council.coding.governance_scheduler import next_governance_action
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.topology import (
    GovernanceMaterialize,
    GovernancePlan,
    GovernanceReview,
)

_ALL_ROLES = {"pm": ["m-pm"], "reviewer": ["m-r"]}

_FINDINGS = [{"severity": "medium", "title": "needs work", "body": "fix it"}]


def _store(project_id: str) -> LedgerStore:
    store = LedgerStore(project_id)
    store.create_project(
        north_star="Build a governed project",
        definition_of_done="approved plan slices merged",
        target="new",
        repo_path=None,
    )
    return store


def _no_user_approval(gov: GovernanceStore) -> None:
    for ap in gov.list_approvals():
        assert ap.required_actor != "user", (
            f"unexpected user approval created: {ap.approval_id}"
        )


# --------------------------------------------------------------------------
# strict — reviewer THEN pm, both required, no human gate
# --------------------------------------------------------------------------
def test_strict_brainstorm_dual_review_no_human_gate(tmp_errorta_home: Path) -> None:
    store = _store("gov3-strict-bs")
    gov = GovernanceStore.for_ledger(store)
    gov.update_state(mode="strict", phase="brainstorming")

    # PM authors the brainstorm under review (mirrors the runner's append).
    art = gov.append_artifact(kind="brainstorm", title="BS", state="under_review")
    gov.update_state(phase="reviewing_brainstorm")

    # Scheduler asks the REVIEWER first.
    act = next_governance_action(store, _ALL_ROLES)
    assert isinstance(act, GovernanceReview)
    assert act.reviewer_role == "reviewer"
    assert act.artifact_id == art.artifact_id

    # Reviewer approves -> still under_review (PM hasn't reviewed yet).
    gov.append_review(
        artifact_id=art.artifact_id, reviewer_member_id="m-r",
        verdict="approved", reviewer_role="reviewer",
    )
    assert gov.settle_artifact_after_review(art.artifact_id, "strict") == "under_review"
    assert gov.get_artifact(art.artifact_id).state == "under_review"

    # Scheduler now asks the PM.
    act = next_governance_action(store, _ALL_ROLES)
    assert isinstance(act, GovernanceReview)
    assert act.reviewer_role == "pm"
    assert act.artifact_id == art.artifact_id

    # PM approves -> fully approved, phase advances to drafting_spec.
    gov.append_review(
        artifact_id=art.artifact_id, reviewer_member_id="m-pm",
        verdict="approved", reviewer_role="pm",
    )
    assert gov.settle_artifact_after_review(art.artifact_id, "strict") == "approved"
    assert gov.get_artifact(art.artifact_id).state == "approved"
    assert gov.load_state().phase == "drafting_spec"

    # No human approval gate was ever created.
    _no_user_approval(gov)


def test_strict_reject_loops_to_revision(tmp_errorta_home: Path) -> None:
    store = _store("gov3-strict-reject")
    gov = GovernanceStore.for_ledger(store)
    gov.update_state(mode="strict", phase="reviewing_brainstorm")
    art = gov.append_artifact(kind="brainstorm", title="BS", state="under_review")

    gov.append_review(
        artifact_id=art.artifact_id, reviewer_member_id="m-r",
        verdict="request_changes", findings=_FINDINGS, reviewer_role="reviewer",
    )
    assert gov.settle_artifact_after_review(art.artifact_id, "strict") == "changes_requested"
    assert gov.get_artifact(art.artifact_id).state == "changes_requested"
    assert gov.load_state().phase == "brainstorming"

    # Scheduler now hands the PM a fresh brainstorming turn (propose-changes loop).
    act = next_governance_action(store, _ALL_ROLES)
    assert isinstance(act, GovernancePlan)
    assert act.phase == "brainstorming"
    _no_user_approval(gov)


def test_strict_spec_and_plan_require_both_reviewers(tmp_errorta_home: Path) -> None:
    store = _store("gov3-strict-spec-plan")
    gov = GovernanceStore.for_ledger(store)
    gov.update_state(mode="strict", phase="drafting_spec")
    gov.append_artifact(kind="brainstorm", title="BS", state="approved")

    for kind, next_phase in (
        ("spec", "drafting_plan"),
        ("implementation_plan", "development"),
    ):
        art = gov.append_artifact(kind=kind, title=kind, state="under_review")
        # reviewer first
        act = next_governance_action(store, _ALL_ROLES)
        assert isinstance(act, GovernanceReview) and act.reviewer_role == "reviewer"
        assert act.artifact_id == art.artifact_id
        gov.append_review(
            artifact_id=art.artifact_id, reviewer_member_id="m-r",
            verdict="approved", reviewer_role="reviewer",
        )
        assert gov.settle_artifact_after_review(art.artifact_id, "strict") == "under_review"
        # then pm
        act = next_governance_action(store, _ALL_ROLES)
        assert isinstance(act, GovernanceReview) and act.reviewer_role == "pm"
        gov.append_review(
            artifact_id=art.artifact_id, reviewer_member_id="m-pm",
            verdict="approved", reviewer_role="pm",
        )
        assert gov.settle_artifact_after_review(art.artifact_id, "strict") == "approved"
        assert gov.get_artifact(art.artifact_id).state == "approved"
        assert gov.load_state().phase == next_phase

    _no_user_approval(gov)


# --------------------------------------------------------------------------
# light — reviewer reviews spec + plan, NOT brainstorm
# --------------------------------------------------------------------------
def test_light_skips_brainstorm_review_then_reviews_spec(tmp_errorta_home: Path) -> None:
    store = _store("gov3-light")
    gov = GovernanceStore.for_ledger(store)
    gov.update_state(mode="light", phase="drafting_spec")

    # In light, the runner authors the brainstorm already approved (no review).
    bs = gov.append_artifact(kind="brainstorm", title="BS", state="approved")
    assert gov.latest_approved_artifact("brainstorm").artifact_id == bs.artifact_id

    # required_reviewer_roles confirms: no reviewers for brainstorm in light.
    from errorta_council.coding.governance import required_reviewer_roles
    assert required_reviewer_roles("light", "brainstorm") == ()
    assert required_reviewer_roles("light", "spec") == ("reviewer",)

    # Spec needs exactly ONE reviewer approval (no PM review).
    spec = gov.append_artifact(kind="spec", title="Spec", state="under_review")
    act = next_governance_action(store, _ALL_ROLES)
    assert isinstance(act, GovernanceReview)
    assert act.reviewer_role == "reviewer"
    assert act.artifact_id == spec.artifact_id

    gov.append_review(
        artifact_id=spec.artifact_id, reviewer_member_id="m-r",
        verdict="approved", reviewer_role="reviewer",
    )
    assert gov.settle_artifact_after_review(spec.artifact_id, "light") == "approved"
    assert gov.get_artifact(spec.artifact_id).state == "approved"
    assert gov.load_state().phase == "drafting_plan"

    # The scheduler never asks the PM to review in light mode.
    gov.append_artifact(kind="implementation_plan", title="Plan", state="under_review")
    act = next_governance_action(store, _ALL_ROLES)
    assert isinstance(act, GovernanceReview) and act.reviewer_role == "reviewer"
    _no_user_approval(gov)


def test_light_plan_approval_materializes(tmp_errorta_home: Path) -> None:
    store = _store("gov3-light-plan")
    gov = GovernanceStore.for_ledger(store)
    gov.update_state(mode="light", phase="drafting_plan")
    gov.append_artifact(kind="brainstorm", title="BS", state="approved")
    gov.append_artifact(kind="spec", title="Spec", state="approved")
    plan = gov.append_artifact(
        kind="implementation_plan", title="Plan", state="under_review",
        body_json={"slices": [{
            "slice_id": "S1", "title": "Slice",
            "done_when": ["done"], "tests": ["pytest"], "review_focus": ["scope"],
        }]},
    )
    gov.append_review(
        artifact_id=plan.artifact_id, reviewer_member_id="m-r",
        verdict="approved", reviewer_role="reviewer",
    )
    assert gov.settle_artifact_after_review(plan.artifact_id, "light") == "approved"
    assert gov.load_state().phase == "development"

    act = next_governance_action(store, _ALL_ROLES)
    assert isinstance(act, GovernanceMaterialize)


# --------------------------------------------------------------------------
# off — no governance at all
# --------------------------------------------------------------------------
def test_off_mode_returns_none(tmp_errorta_home: Path) -> None:
    store = _store("gov3-off")
    gov = GovernanceStore.for_ledger(store)
    gov.update_state(mode="off", phase="idle")
    assert next_governance_action(store, _ALL_ROLES) is None


# --------------------------------------------------------------------------
# settle_artifact_after_review edge cases
# --------------------------------------------------------------------------
def test_settle_unknown_artifact_returns_empty(tmp_errorta_home: Path) -> None:
    store = _store("gov3-settle-unknown")
    gov = GovernanceStore.for_ledger(store)
    assert gov.settle_artifact_after_review("nope", "strict") == ""


def test_latest_review_by_role_last_wins(tmp_errorta_home: Path) -> None:
    store = _store("gov3-by-role")
    gov = GovernanceStore.for_ledger(store)
    art = gov.append_artifact(kind="spec", title="Spec", state="under_review")
    gov.append_review(
        artifact_id=art.artifact_id, reviewer_member_id="m-r",
        verdict="request_changes", findings=_FINDINGS, reviewer_role="reviewer",
    )
    gov.append_review(
        artifact_id=art.artifact_id, reviewer_member_id="m-r2",
        verdict="approved", reviewer_role="reviewer",
    )
    by_role = gov.latest_review_by_role(art.artifact_id)
    assert set(by_role) == {"reviewer"}
    assert by_role["reviewer"].verdict == "approved"
    assert by_role["reviewer"].reviewer_member_id == "m-r2"
