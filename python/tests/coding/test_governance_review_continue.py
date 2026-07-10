"""F100 governance review-flow bugfixes (2026-06-25).

Two bugs in the Coding "Coding Team" governance review drawer:

* BUG 1 — a spec/plan/brainstorm artifact must NEVER be persisted with a blank
  body when there is content to show. ``_governance_artifact_payload`` now renders
  the structured fields (title + acceptance criteria) when ``body_markdown`` is
  somehow blank, so the viewer never shows an empty box.

* BUG 2 — "Send & continue" on a review-stopped run used to call ``/run/resume``,
  which is crash-recovery only (status must be ``interrupted``) and 409s a
  ``stopped`` governance run. The new ``/run/continue`` endpoint re-drives the
  governance loop from the stopped stage with the queued interjection in context,
  NO 409, and the interjection is consumed by the next PM turn.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from errorta_council.coding.governance import GovernanceStore
from errorta_council.coding.governance_schemas import PMSpecDraftIntent
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.runner import (
    _governance_artifact_payload,
    build_run_turn,
    members_by_coding_role,
)
from errorta_council.coding.topology import GovernancePlan

_MEMBERS = [
    {"id": "m-pm", "enabled": True, "metadata": {"coding_role": "pm"}},
    {"id": "m-dev", "enabled": True, "metadata": {"coding_role": "dev"}},
    {"id": "m-rev", "enabled": True, "metadata": {"coding_role": "reviewer"}},
    {"id": "m-test", "enabled": True, "metadata": {"coding_role": "tester"}},
]

_ROUTE_MEMBERS = [
    {"id": "m-1", "enabled": True, "role": "pm",
     "gateway_route_id": "fake.local.deterministic", "provider_kind": "local"},
    {"id": "m-2", "enabled": True, "role": "dev",
     "gateway_route_id": "fake.local.deterministic", "provider_kind": "local"},
]


# --- BUG 1: artifact body is never blank when structured fields exist ----------

def test_blank_spec_body_falls_back_to_structured_render() -> None:
    """A spec intent whose ``body_markdown`` is blank (e.g. a future/degraded
    path that bypasses strict validation) must still yield a NON-EMPTY body
    rendered from the structured fields — so the drawer never shows an empty box.
    """
    # model_construct bypasses the schema's non-blank-body validator to simulate
    # the exact degraded input the robustness fix must survive.
    intent = PMSpecDraftIntent.model_construct(
        kind="spec_draft",
        title="Auth spec",
        body_markdown="",
        acceptance_criteria=["Login works", "Logout clears the session"],
        source_refs=[],
        supersedes_artifact_id=None,
    )
    kind, title, markdown, body_json, _refs, _sup = _governance_artifact_payload(intent)
    assert kind == "spec"
    assert title == "Auth spec"
    # The body is non-blank and carries the readable structured content.
    assert markdown.strip()
    assert "Auth spec" in markdown
    assert "Login works" in markdown
    # The acceptance criteria are still in body_json for the structured fallback.
    assert body_json["acceptance_criteria"] == ["Login works", "Logout clears the session"]


def test_spec_body_persists_nonempty_via_runner(tmp_errorta_home: Path) -> None:
    """End-to-end through the runner: a normal PM spec turn persists an artifact
    whose ``body_markdown`` is non-empty (the human can read the spec).
    """
    store = LedgerStore("gov-blank-spec")
    store.create_project(
        north_star="x", definition_of_done="d", target="new", repo_path=None)
    governance = GovernanceStore.for_ledger(store)
    governance.update_state(mode="strict", phase="drafting_spec")
    # Approve a brainstorm so the scheduler is past it (not strictly needed here —
    # we dispatch the spec drafting action directly).
    bs = governance.append_artifact(kind="brainstorm", title="BS", state="approved")
    assert bs.state == "approved"

    # The strict parser rejects a blank spec body, so the degraded blank-body
    # fallback is locked at the payload boundary above. Here we confirm the normal
    # runner path still persists readable body text.
    def caller(member, prompt):  # noqa: ANN001
        return json.dumps({
            "schema_version": "governance_turn.v1",
            "role": "pm",
            "intent": {
                "kind": "spec_draft",
                "title": "Auth spec",
                "body_markdown": "## Goal\nShip auth.",
                "acceptance_criteria": ["Login works"],
            },
        })

    rt = build_run_turn(store, None, members_by_coding_role(_MEMBERS), caller,
                        guardrail_enabled=True)
    outcome = rt(GovernancePlan(member_id="m-pm", phase="drafting_spec"), store)
    assert outcome.kind == "governance_progress"
    spec = governance.latest_artifact("spec")
    assert spec is not None
    assert spec.body_markdown.strip(), "spec body must never be blank"


# --- BUG 2: continue a review-stopped governance run ---------------------------

def _client(tmp_errorta_home: Path) -> TestClient:
    from errorta_app.server import app
    return TestClient(app, headers={"x-errorta-origin": "tauri-ui"})


def _seed_stopped_run(c: TestClient, pid: str, *, with_config: bool = True) -> LedgerStore:
    c.post("/coding/projects", json={"project_id": pid, "north_star": "n",
           "definition_of_done": "d", "target": "new"})
    c.put(f"/coding/projects/{pid}/governance/settings", json={"mode": "strict"})
    store = LedgerStore(pid)
    if with_config:
        store.set_run_config(members=_ROUTE_MEMBERS, room_id="demo-room", saved_at="t0")
    # A review-stopped governance run: status "stopped" (NOT "interrupted").
    store.set_run_state(status="stopped",
                        stop_reason="governance_review_not_converging")
    return store


def test_continue_does_not_409_on_stopped_run(tmp_errorta_home: Path) -> None:
    c = _client(tmp_errorta_home)
    _seed_stopped_run(c, "gov-continue")
    r = c.post("/coding/projects/gov-continue/run/continue", json={})  # empty body
    assert r.status_code == 200, r.text
    assert r.json().get("started") is True


def test_resume_still_409s_on_stopped_run(tmp_errorta_home: Path) -> None:
    """The regression lock: a review-stopped run is NOT resumable via the
    crash-recovery endpoint — that is exactly the 409 the old UI hit. Continue
    is the correct path."""
    c = _client(tmp_errorta_home)
    _seed_stopped_run(c, "gov-resume-409")
    r = c.post("/coding/projects/gov-resume-409/run/resume", json={})
    assert r.status_code == 409
    assert r.json()["detail"] == "run is not recoverable"


def test_continue_rejects_interrupted_run(tmp_errorta_home: Path) -> None:
    """Continue must not bypass resume's crash-recovery workspace-integrity path."""
    c = _client(tmp_errorta_home)
    store = _seed_stopped_run(c, "gov-continue-interrupted")
    store.set_run_state(status="interrupted", stop_reason="sidecar_restart")
    r = c.post("/coding/projects/gov-continue-interrupted/run/continue", json={})
    assert r.status_code == 409
    assert r.json()["detail"] == "run is not continuable"


def test_continue_recovers_saved_team(tmp_errorta_home: Path) -> None:
    c = _client(tmp_errorta_home)
    _seed_stopped_run(c, "gov-continue-team")
    r = c.post("/coding/projects/gov-continue-team/run/continue", json={})
    assert r.status_code == 200, r.text
    # The saved team is preserved (a recovery-from-config doesn't rewrite it).
    cfg = LedgerStore("gov-continue-team").get_run_config()
    assert [m["id"] for m in cfg.get("members", [])] == ["m-1", "m-2"]


def test_continue_without_team_is_actionable(tmp_errorta_home: Path) -> None:
    c = _client(tmp_errorta_home)
    _seed_stopped_run(c, "gov-continue-noteam", with_config=False)
    r = c.post("/coding/projects/gov-continue-noteam/run/continue", json={})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "run_config_missing"


def test_continue_requires_tauri_origin(tmp_errorta_home: Path) -> None:
    from errorta_app.server import app
    _seed_stopped_run(_client(tmp_errorta_home), "gov-continue-origin")
    nope = TestClient(app)  # no tauri origin header
    r = nope.post("/coding/projects/gov-continue-origin/run/continue", json={})
    assert r.status_code == 403


def test_continue_consumes_interjection_in_next_pm_turn(tmp_errorta_home: Path) -> None:
    """The continuation re-drives the PM drafting turn WITH the user's queued
    interjection + latest review findings in context, and consumes it.

    Drives the runner directly (deterministic, no live model) so the assertion is
    on the actual PM prompt the next turn sees and on the interjection cursor.
    """
    store = LedgerStore("gov-continue-interject")
    store.create_project(
        north_star="x", definition_of_done="d", target="new", repo_path=None)
    governance = GovernanceStore.for_ledger(store)
    governance.update_state(mode="strict", phase="drafting_spec")
    governance.append_artifact(kind="brainstorm", title="BS", state="approved")
    # A prior spec rejected with a review finding (the "changes requested" stop).
    spec = governance.append_artifact(
        kind="spec", title="Spec v1", body_markdown="## Goal\nv1",
        body_json={"acceptance_criteria": ["a"]}, state="changes_requested")
    governance.append_review(
        artifact_id=spec.artifact_id, reviewer_member_id="m-rev",
        verdict="request_changes",
        findings=[{"severity": "high", "title": "Too vague",
                   "body": "Add testable acceptance criteria."}])
    # The user's interjection queued from the drawer's "Send & continue".
    store.record_interjection("Focus on MEMORY safety, not speed.",
                              artifact_id=spec.artifact_id)
    assert store.list_unconsumed_interjections(), "precondition: interjection queued"

    seen_prompts: list[str] = []

    def caller(member, prompt):  # noqa: ANN001
        seen_prompts.append(prompt)
        return json.dumps({
            "schema_version": "governance_turn.v1",
            "role": "pm",
            "intent": {
                "kind": "spec_revision",
                "title": "Spec v2",
                "body_markdown": "## Goal\nShip MEMORY-safe auth.",
                "acceptance_criteria": ["No leaks", "Login works"],
            },
        })

    rt = build_run_turn(store, None, members_by_coding_role(_MEMBERS), caller,
                        guardrail_enabled=True)
    outcome = rt(GovernancePlan(member_id="m-pm", phase="drafting_spec"), store)
    assert outcome.kind == "governance_progress"

    # The PM saw the user's authoritative direction AND the prior review finding.
    assert any("Focus on MEMORY safety" in p for p in seen_prompts)
    assert any("testable acceptance criteria" in p.lower() for p in seen_prompts)
    assert any("revise the spec" in p for p in seen_prompts)
    assert not any("revise the brainstorm" in p for p in seen_prompts)
    # The interjection was consumed (not re-fed on the following turn).
    assert not store.list_unconsumed_interjections()
    # A new spec version was drafted (the loop advanced).
    specs = governance.list_artifacts(kind="spec")
    assert len(specs) >= 2
    assert specs[-1].title == "Spec v2"
