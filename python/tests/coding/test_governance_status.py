"""F100-01 — lock the governance status projection (pure, read-only).

Mirrors ``test_governance_three_modes`` fixtures: drive the governance store
through each (phase, artifact state, reviews) and assert the plain-language
stage/status/headline/actor/review_pass/steps the UI renders. No new tasks, no
state writes beyond the governance store the test itself drives.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from errorta_council.coding.governance import GovernanceStore
from errorta_council.coding.governance_status import governance_status
from errorta_council.coding.ledger import LedgerStore

# role -> member dicts (the ``members_by_coding_role`` shape).
_BY_ROLE = {
    "pm": [{"id": "m-pm", "name": "PM-Prime", "metadata": {"coding_role": "pm"}}],
    "reviewer": [{"id": "m-rev", "name": "Echo-REV",
                  "metadata": {"coding_role": "reviewer"}}],
    "dev": [{"id": "m-dev", "name": "Dev-1", "metadata": {"coding_role": "dev"}}],
}

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


def _step(steps: list[dict], stage: str) -> str:
    return next(s["state"] for s in steps if s["stage"] == stage)


# --------------------------------------------------------------------------
# strict — the full brainstorm → spec → plan → build → done walk
# --------------------------------------------------------------------------
def test_strict_brainstorm_drafting(tmp_errorta_home: Path) -> None:
    store = _store("gs-strict-bs-draft")
    gov = GovernanceStore.for_ledger(store)
    gov.update_state(mode="strict", phase="brainstorming")
    gov.append_artifact(kind="brainstorm", title="BS", state="draft")

    out = governance_status(store, _BY_ROLE, run_active=True)
    assert out["mode"] == "strict"
    assert out["stage"] == "brainstorm"
    assert out["status"] == "drafting"
    assert out["headline"] == "Brainstorm — drafting"
    assert out["actor_label"] == "PM-Prime"
    assert out["actor_member_id"] == "m-pm"
    assert out["review_pass"] is None
    assert _step(out["steps"], "brainstorm") == "drafting"
    assert _step(out["steps"], "spec") == "pending"
    assert out["build_progress"] is None


def test_strict_brainstorm_under_review_reviewer_pass(tmp_errorta_home: Path) -> None:
    store = _store("gs-strict-bs-rev")
    gov = GovernanceStore.for_ledger(store)
    gov.update_state(mode="strict", phase="reviewing_brainstorm")
    gov.append_artifact(kind="brainstorm", title="BS", state="under_review")

    out = governance_status(store, _BY_ROLE, run_active=True)
    assert out["stage"] == "brainstorm"
    assert out["status"] == "under_review"
    assert out["headline"] == "Brainstorm — under review"
    assert out["review_pass"] == "reviewer"
    assert out["actor_label"] == "Echo-REV"
    assert out["actor_member_id"] == "m-rev"
    assert _step(out["steps"], "brainstorm") == "under_review"


def test_strict_under_review_advances_to_pm_pass(tmp_errorta_home: Path) -> None:
    store = _store("gs-strict-bs-pm")
    gov = GovernanceStore.for_ledger(store)
    gov.update_state(mode="strict", phase="reviewing_brainstorm")
    art = gov.append_artifact(kind="brainstorm", title="BS", state="under_review")
    # reviewer approved; PM has not reviewed yet.
    gov.append_review(artifact_id=art.artifact_id, reviewer_member_id="m-rev",
                      verdict="approved", reviewer_role="reviewer")

    out = governance_status(store, _BY_ROLE, run_active=True)
    assert out["status"] == "under_review"
    assert out["review_pass"] == "pm"
    assert out["actor_label"] == "PM-Prime"
    assert out["actor_member_id"] == "m-pm"


def test_strict_changes_requested_pm_revising(tmp_errorta_home: Path) -> None:
    store = _store("gs-strict-bs-changes")
    gov = GovernanceStore.for_ledger(store)
    gov.update_state(mode="strict", phase="reviewing_brainstorm")
    art = gov.append_artifact(kind="brainstorm", title="BS", state="under_review")
    gov.append_review(artifact_id=art.artifact_id, reviewer_member_id="m-rev",
                      verdict="request_changes", findings=_FINDINGS,
                      reviewer_role="reviewer")

    out = governance_status(store, _BY_ROLE, run_active=True)
    assert out["status"] == "changes_requested"
    assert out["headline"] == "Brainstorm — changes requested"
    assert out["actor_label"] == "PM-Prime"
    assert out["review_pass"] is None


def test_strict_brainstorm_approved_advances_stage(tmp_errorta_home: Path) -> None:
    store = _store("gs-strict-spec-stage")
    gov = GovernanceStore.for_ledger(store)
    # brainstorm approved; now drafting the spec.
    gov.update_state(mode="strict", phase="drafting_spec")
    gov.append_artifact(kind="brainstorm", title="BS", state="approved")
    gov.append_artifact(kind="spec", title="Spec", state="draft")

    out = governance_status(store, _BY_ROLE, run_active=True)
    assert out["stage"] == "spec"
    assert out["status"] == "drafting"
    assert out["headline"] == "Spec — drafting"
    # the completed brainstorm step is checked.
    assert _step(out["steps"], "brainstorm") == "approved"
    assert _step(out["steps"], "spec") == "drafting"
    assert _step(out["steps"], "plan") == "pending"


def test_strict_spec_under_review(tmp_errorta_home: Path) -> None:
    store = _store("gs-strict-spec-rev")
    gov = GovernanceStore.for_ledger(store)
    gov.update_state(mode="strict", phase="reviewing_spec")
    gov.append_artifact(kind="brainstorm", title="BS", state="approved")
    gov.append_artifact(kind="spec", title="Spec", state="under_review")

    out = governance_status(store, _BY_ROLE, run_active=True)
    assert out["stage"] == "spec"
    assert out["status"] == "under_review"
    assert out["headline"] == "Spec — under review"
    assert out["review_pass"] == "reviewer"
    assert _step(out["steps"], "brainstorm") == "approved"


def test_strict_plan_under_review(tmp_errorta_home: Path) -> None:
    store = _store("gs-strict-plan-rev")
    gov = GovernanceStore.for_ledger(store)
    gov.update_state(mode="strict", phase="reviewing_plan")
    gov.append_artifact(kind="brainstorm", title="BS", state="approved")
    gov.append_artifact(kind="spec", title="Spec", state="approved")
    gov.append_artifact(kind="implementation_plan", title="Plan", state="under_review")

    out = governance_status(store, _BY_ROLE, run_active=True)
    assert out["stage"] == "plan"
    assert out["status"] == "under_review"
    assert out["headline"] == "Plan — under review"
    assert _step(out["steps"], "spec") == "approved"
    assert _step(out["steps"], "plan") == "under_review"
    assert _step(out["steps"], "build") == "pending"


def test_strict_development_build_progress(tmp_errorta_home: Path) -> None:
    store = _store("gs-strict-build")
    gov = GovernanceStore.for_ledger(store)
    gov.update_state(mode="strict", phase="development")
    gov.append_artifact(kind="brainstorm", title="BS", state="approved")
    gov.append_artifact(kind="spec", title="Spec", state="approved")
    gov.append_artifact(kind="implementation_plan", title="Plan", state="approved")
    t1 = store.add_task(title="task one", role="dev")
    store.add_task(title="task two", role="dev")
    t3 = store.add_task(title="task three", role="dev")
    store.update_task(t1.task_id, state="done")
    store.update_task(t3.task_id, state="dropped")

    out = governance_status(store, _BY_ROLE, run_active=True)
    assert out["stage"] == "build"
    assert out["status"] == "building"
    assert out["headline"] == "Building"
    # 1 done; total excludes the dropped one (3 - 1 dropped = 2).
    assert out["build_progress"] == {"done": 1, "total": 2}
    assert _step(out["steps"], "plan") == "approved"
    assert _step(out["steps"], "build") == "building"
    assert _step(out["steps"], "done") == "pending"


def test_strict_complete_done(tmp_errorta_home: Path) -> None:
    store = _store("gs-strict-done")
    gov = GovernanceStore.for_ledger(store)
    gov.update_state(mode="strict", phase="complete")

    out = governance_status(store, _BY_ROLE, run_active=False)
    assert out["stage"] == "done"
    assert out["status"] is None
    assert out["headline"] == "Done"
    assert out["build_progress"] is None
    assert all(s["state"] == "approved" for s in out["steps"])


def test_idle_getting_started(tmp_errorta_home: Path) -> None:
    store = _store("gs-strict-idle")
    gov = GovernanceStore.for_ledger(store)
    gov.update_state(mode="strict", phase="idle")

    out = governance_status(store, _BY_ROLE, run_active=False)
    assert out["stage"] == "idle"
    assert out["status"] is None
    assert out["headline"] == "Getting started"
    assert all(s["state"] == "pending" for s in out["steps"])


# --------------------------------------------------------------------------
# off — panel hidden
# --------------------------------------------------------------------------
def test_off_mode_hidden(tmp_errorta_home: Path) -> None:
    store = _store("gs-off")
    gov = GovernanceStore.for_ledger(store)
    gov.update_state(mode="off", phase="idle")

    out = governance_status(store, _BY_ROLE, run_active=False)
    assert out["mode"] == "off"
    assert out["stage"] == "idle"
    assert out["status"] is None
    assert out["headline"] == ""
    assert out["steps"] == []
    assert out["build_progress"] is None


# --------------------------------------------------------------------------
# light — brainstorm auto-approved, no under-review status
# --------------------------------------------------------------------------
def test_light_brainstorm_no_under_review(tmp_errorta_home: Path) -> None:
    store = _store("gs-light-bs")
    gov = GovernanceStore.for_ledger(store)
    # Light authors the brainstorm already approved (no review), then drafts spec.
    gov.update_state(mode="light", phase="drafting_spec")
    gov.append_artifact(kind="brainstorm", title="BS", state="approved")
    gov.append_artifact(kind="spec", title="Spec", state="draft")

    out = governance_status(store, _BY_ROLE, run_active=True)
    assert out["mode"] == "light"
    assert out["stage"] == "spec"
    # The brainstorm step is approved/checked with no under_review sub-state.
    assert _step(out["steps"], "brainstorm") == "approved"


def test_light_spec_still_reviewed(tmp_errorta_home: Path) -> None:
    store = _store("gs-light-spec")
    gov = GovernanceStore.for_ledger(store)
    gov.update_state(mode="light", phase="reviewing_spec")
    gov.append_artifact(kind="brainstorm", title="BS", state="approved")
    gov.append_artifact(kind="spec", title="Spec", state="under_review")

    out = governance_status(store, _BY_ROLE, run_active=True)
    assert out["stage"] == "spec"
    assert out["status"] == "under_review"
    assert out["review_pass"] == "reviewer"
    assert out["actor_label"] == "Echo-REV"


# --------------------------------------------------------------------------
# pure projection — no egress, no new tasks
# --------------------------------------------------------------------------
def test_no_new_tasks(tmp_errorta_home: Path) -> None:
    store = _store("gs-no-tasks")
    gov = GovernanceStore.for_ledger(store)
    gov.update_state(mode="strict", phase="reviewing_brainstorm")
    gov.append_artifact(kind="brainstorm", title="BS", state="under_review")

    before = len(store.list_tasks())
    governance_status(store, _BY_ROLE, run_active=True)
    assert len(store.list_tasks()) == before == 0


def test_governance_status_imports_no_egress() -> None:
    """The projection module must import zero egress machinery (invariant 3),
    checked in a CLEAN subprocess (mirrors ``test_ledger_no_egress``)."""
    code = (
        "import sys; import errorta_council.coding.governance_status;"
        "banned=['httpx','requests','errorta_model_gateway','subprocess'];"
        "leaked=[m for m in banned if m in sys.modules];"
        "print(','.join(leaked));"
        "sys.exit(1 if leaked else 0)"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, (
        f"governance_status leaked egress imports: {proc.stdout.strip()}"
    )
