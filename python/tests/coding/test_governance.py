from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from errorta_council.coding.governance import GovernanceError, GovernanceStore
from errorta_council.coding.governance_materialize import materialize_approved_plan
from errorta_council.coding.governance_schemas import (
    GovernanceTurnParseError,
    PMPlanDraftIntent,
    parse_governance_turn,
)
from errorta_council.coding.governance_scheduler import next_governance_action
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.topology import (
    Complete,
    GovernanceMaterialize,
    GovernancePlan,
    GovernanceReview,
)


def _store(project_id: str) -> LedgerStore:
    store = LedgerStore(project_id)
    store.create_project(
        north_star="Build a governed project",
        definition_of_done="approved plan slices merged",
        target="new",
        repo_path=None,
    )
    return store


def test_governance_approval_requires_user_actor(tmp_errorta_home: Path) -> None:
    store = _store("gov-approval")
    governance = GovernanceStore.for_ledger(store)
    governance.update_state(mode="strict", phase="brainstorming")
    artifact = governance.append_artifact(
        kind="brainstorm",
        title="Brainstorm",
        body_markdown="direction",
        state="awaiting_approval",
    )
    approval = governance.create_approval(
        kind="brainstorm_approval",
        artifact_id=artifact.artifact_id,
        requested_by_member_id="m-pm",
    )

    try:
        governance.resolve_approval(
            approval.approval_id,
            approved=True,
            resolved_by="m-pm",
            actor_role="pm",
        )
    except GovernanceError as exc:
        assert "pm cannot approve" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("PM self-approval should fail")

    resolved = governance.resolve_approval(
        approval.approval_id,
        approved=True,
        resolved_by="user",
    )

    assert resolved.state == "approved"
    assert governance.get_artifact(artifact.artifact_id).state == "approved"  # type: ignore[union-attr]
    assert governance.load_state().phase == "drafting_spec"


def test_latest_approved_requires_latest_artifact_state(tmp_errorta_home: Path) -> None:
    store = _store("gov-latest")
    governance = GovernanceStore.for_ledger(store)
    governance.append_artifact(kind="spec", title="Spec v1", state="approved")
    governance.append_artifact(kind="spec", title="Spec v2", state="changes_requested")

    assert governance.latest_approved_artifact("spec") is None


def test_parse_governance_plan_requires_unique_slices() -> None:
    valid = {
        "schema_version": "governance_turn.v1",
        "role": "pm",
        "intent": {
            "kind": "plan_draft",
            "title": "Implementation plan",
            "slices": [
                {
                    "slice_id": "S1",
                    "title": "First slice",
                    "done_when": ["done"],
                    "tests": ["pytest"],
                    "review_focus": ["scope"],
                }
            ],
        },
    }
    parsed = parse_governance_turn("pm", json.dumps(valid))
    assert not isinstance(parsed, GovernanceTurnParseError)
    assert isinstance(parsed.intent, PMPlanDraftIntent)

    valid["intent"]["slices"].append(dict(valid["intent"]["slices"][0]))  # type: ignore[index]
    invalid = parse_governance_turn("pm", json.dumps(valid))
    assert isinstance(invalid, GovernanceTurnParseError)


def test_materialize_approved_plan_preserves_provenance_and_deps(
    tmp_errorta_home: Path,
) -> None:
    store = _store("gov-materialize")
    governance = GovernanceStore.for_ledger(store)
    governance.update_state(mode="strict", phase="development")
    spec = governance.append_artifact(
        kind="spec",
        title="Spec",
        body_markdown="spec body",
        state="approved",
    )
    plan = governance.append_artifact(
        kind="implementation_plan",
        title="Plan",
        state="approved",
        body_json={
            "slices": [
                {
                    "slice_id": "S1",
                    "title": "Scaffold",
                    "done_when": ["file exists"],
                    "tests": ["pytest"],
                    "review_focus": ["shape"],
                },
                {
                    "slice_id": "S2",
                    "title": "Wire UI",
                    "depends_on": ["S1"],
                    "done_when": ["panel renders"],
                    "tests": ["vitest"],
                    "review_focus": ["a11y"],
                },
            ]
        },
    )

    result = materialize_approved_plan(store, governance)
    again = materialize_approved_plan(store, governance)
    tasks = store.list_tasks()

    assert result["created"] == 2
    assert again["created"] == 0
    assert len(tasks) == 2
    assert {t.source_plan_artifact_id for t in tasks} == {plan.artifact_id}
    assert {t.source_spec_artifact_id for t in tasks} == {spec.artifact_id}
    assert all(t.governance_required for t in tasks)
    wire = next(t for t in tasks if t.source_slice_id == "S2")
    scaffold = next(t for t in tasks if t.source_slice_id == "S1")
    assert wire.depends_on == [scaffold.task_id]


def test_governance_scheduler_blocks_without_required_actors(
    tmp_errorta_home: Path,
) -> None:
    store = _store("gov-schedule")
    governance = GovernanceStore.for_ledger(store)
    governance.update_state(mode="strict", phase="brainstorming")

    assert isinstance(next_governance_action(store, {"pm": ["m-pm"]}), GovernancePlan)
    blocked = next_governance_action(store, {})
    assert isinstance(blocked, Complete)
    assert blocked.reason == "no_governance_pm"

    governance.append_artifact(
        kind="brainstorm",
        title="Brainstorm",
        state="approved",
    )
    spec = governance.append_artifact(kind="spec", title="Spec", state="under_review")
    review = next_governance_action(store, {"pm": ["m-pm"], "reviewer": ["m-r"]})
    assert isinstance(review, GovernanceReview)
    assert review.artifact_id == spec.artifact_id


def test_governance_scheduler_materializes_approved_plan(
    tmp_errorta_home: Path,
) -> None:
    store = _store("gov-schedule-plan")
    governance = GovernanceStore.for_ledger(store)
    governance.update_state(mode="light", phase="development")
    governance.append_artifact(kind="brainstorm", title="Brainstorm", state="approved")
    governance.append_artifact(kind="spec", title="Spec", state="approved")
    governance.append_artifact(
        kind="implementation_plan",
        title="Plan",
        state="approved",
        body_json={
            "slices": [
                {
                    "slice_id": "S1",
                    "title": "Slice",
                    "done_when": ["done"],
                    "tests": ["pytest"],
                    "review_focus": ["scope"],
                }
            ]
        },
    )

    action = next_governance_action(store, {"pm": ["m-pm"]})

    assert isinstance(action, GovernanceMaterialize)


def test_governance_routes_settings_approval_and_export(tmp_errorta_home: Path) -> None:
    from errorta_app.server import app

    client = TestClient(app, headers={"x-errorta-origin": "tauri-ui"})
    created = client.post(
        "/coding/projects",
        json={
            "project_id": "gov-routes",
            "north_star": "n",
            "definition_of_done": "d",
            "target": "new",
        },
    )
    assert created.status_code == 200, created.text
    settings = client.put(
        "/coding/projects/gov-routes/governance/settings",
        json={"mode": "strict"},
    )
    assert settings.status_code == 200, settings.text
    assert settings.json()["state"]["mode"] == "strict"

    store = LedgerStore("gov-routes")
    governance = GovernanceStore.for_ledger(store)
    artifact = governance.append_artifact(
        kind="spec",
        title="Spec",
        body_markdown="# Spec",
        state="awaiting_approval",
    )
    approval = governance.create_approval(
        kind="spec_approval",
        artifact_id=artifact.artifact_id,
        requested_by_member_id="m-reviewer",
    )

    listed = client.get("/coding/projects/gov-routes/governance/approvals")
    assert listed.status_code == 200
    assert listed.json()["approvals"][0]["approval_id"] == approval.approval_id

    approved = client.post(
        f"/coding/projects/gov-routes/governance/approvals/{approval.approval_id}/approve",
        json={},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["approval"]["state"] == "approved"

    exported = client.post(
        f"/coding/projects/gov-routes/governance/artifacts/{artifact.artifact_id}/export-task",
        json={"target_path": "docs/specs/F-example.md"},
    )
    assert exported.status_code == 200, exported.text
    assert exported.json()["task"]["source_spec_artifact_id"] == artifact.artifact_id


def test_governance_mutation_routes_require_tauri_origin(tmp_errorta_home: Path) -> None:
    """The approval/reject/settings/export-task gates change governance state and
    can authorize plan materialization. A non-Tauri caller (any other local
    process) must be refused 403 — only the UI may approve a gate."""
    from errorta_app.server import app

    tauri = TestClient(app, headers={"x-errorta-origin": "tauri-ui"})
    created = tauri.post(
        "/coding/projects",
        json={"project_id": "gov-origin", "north_star": "n",
              "definition_of_done": "d", "target": "new"},
    )
    assert created.status_code == 200, created.text

    store = LedgerStore("gov-origin")
    governance = GovernanceStore.for_ledger(store)
    governance.update_state(mode="strict", phase="awaiting_spec_approval")
    artifact = governance.append_artifact(
        kind="spec", title="Spec", body_markdown="# Spec", state="awaiting_approval",
    )
    approval = governance.create_approval(
        kind="spec_approval", artifact_id=artifact.artifact_id,
        requested_by_member_id="m-reviewer",
    )

    # No origin header -> every state-mutating governance route is refused, and
    # the gate stays pending (no silent approval).
    nope = TestClient(app)  # no x-errorta-origin
    pid = "gov-origin"
    assert nope.put(f"/coding/projects/{pid}/governance/settings",
                    json={"mode": "off"}).status_code == 403
    assert nope.post(
        f"/coding/projects/{pid}/governance/approvals/{approval.approval_id}/approve",
        json={"actor": "user"}).status_code == 403
    assert nope.post(
        f"/coding/projects/{pid}/governance/approvals/{approval.approval_id}/reject",
        json={"actor": "user"}).status_code == 403
    assert nope.post(
        f"/coding/projects/{pid}/governance/artifacts/{artifact.artifact_id}/export-task",
        json={"target_path": "docs/specs/x.md"}).status_code == 403

    # The gate was not resolved by any of the refused calls.
    assert GovernanceStore.for_ledger(store).get_approval(
        approval.approval_id).state == "pending"
    assert GovernanceStore.for_ledger(store).get_artifact(
        artifact.artifact_id).state == "awaiting_approval"


def test_materialize_refuses_unapproved_plan_in_strict_mode(
    tmp_errorta_home: Path,
) -> None:
    """Fail-closed: strict governance must never turn an un-approved (or
    review-only) plan into executable DEV tasks."""
    store = _store("gov-no-approve")
    governance = GovernanceStore.for_ledger(store)
    governance.update_state(mode="strict", phase="development")
    # A spec + plan that exist but were never approved.
    governance.append_artifact(kind="spec", title="Spec", state="under_review")
    governance.append_artifact(
        kind="implementation_plan", title="Plan", state="awaiting_approval",
        body_json={"slices": [{"slice_id": "S1", "title": "Scaffold"}]},
    )

    try:
        materialize_approved_plan(store, governance)
        assert False, "expected strict governance to refuse an unapproved plan"
    except GovernanceError as exc:
        assert "approved plan" in str(exc)
    assert store.list_tasks() == []


def test_governance_summary_includes_status_strict(tmp_errorta_home: Path) -> None:
    """F100-01: the governance summary route folds in the plain-language status
    (additive — the existing ``governance`` payload is unchanged)."""
    from errorta_app.server import app

    client = TestClient(app, headers={"x-errorta-origin": "tauri-ui"})
    created = client.post(
        "/coding/projects",
        json={"project_id": "gov-status", "north_star": "n",
              "definition_of_done": "d", "target": "new"},
    )
    assert created.status_code == 200, created.text
    settings = client.put(
        "/coding/projects/gov-status/governance/settings",
        json={"mode": "strict"},
    )
    assert settings.status_code == 200, settings.text

    store = LedgerStore("gov-status")
    gov = GovernanceStore.for_ledger(store)
    gov.update_state(mode="strict", phase="reviewing_brainstorm")
    gov.append_artifact(kind="brainstorm", title="BS", state="under_review")

    resp = client.get("/coding/projects/gov-status/governance")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # existing payload preserved.
    assert "governance" in body
    assert body["governance"]["state"]["mode"] == "strict"
    # additive status block.
    status = body["status"]
    assert status["mode"] == "strict"
    assert status["stage"] == "brainstorm"
    assert status["status"] == "under_review"
    assert status["headline"] == "Brainstorm — under review"
    assert status["review_pass"] == "reviewer"


def test_governance_summary_status_off_hidden(tmp_errorta_home: Path) -> None:
    from errorta_app.server import app

    client = TestClient(app, headers={"x-errorta-origin": "tauri-ui"})
    created = client.post(
        "/coding/projects",
        json={"project_id": "gov-status-off", "north_star": "n",
              "definition_of_done": "d", "target": "new"},
    )
    assert created.status_code == 200, created.text

    resp = client.get("/coding/projects/gov-status-off/governance")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"]["mode"] == "off"
