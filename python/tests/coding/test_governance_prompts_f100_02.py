"""F100-02 Slice 1 — A2 stage-calibrated review rubric + A3 effective redraft.

Locks the prompt-only convergence fixes:
* A2 — the brainstorm review prompt states open-questions-are-OK + block-only-on-
  essentials and OMITS the spec-level demands; the spec/plan prompt keeps rigor.
* A3 — after ``changes_requested`` the next PM (re)draft prompt injects the prior
  review's findings + the address-or-defer instruction.
"""
from __future__ import annotations

from pathlib import Path

from errorta_council.coding.governance import GovernanceStore
from errorta_council.coding.governance_prompts import (
    build_governance_review_prompt,
    build_pm_governance_prompt,
)
from errorta_council.coding.ledger import LedgerStore


def _store(project_id: str) -> tuple[LedgerStore, GovernanceStore]:
    store = LedgerStore(project_id)
    store.create_project(
        north_star="Build a governed project",
        definition_of_done="approved plan slices merged",
        target="new",
        repo_path=None,
    )
    return store, GovernanceStore.for_ledger(store)


# --- A2: brainstorm rubric is high-level -----------------------------------
def test_brainstorm_review_prompt_is_high_level(tmp_errorta_home: Path) -> None:
    store, gov = _store("f100-02-a2-bs")
    art = gov.append_artifact(kind="brainstorm", title="BS", state="under_review")

    prompt = build_governance_review_prompt(
        store=store, governance=gov, artifact=art, reviewer_role="reviewer")

    # Open questions explicitly OK + block only on essentials.
    assert "Open questions" in prompt
    assert "NOT" in prompt
    assert "problem statement" in prompt
    assert "non-goals" in prompt
    # Must NOT demand spec-level rigor in the brainstorm rubric.
    assert "acceptance criteria" not in prompt.split("verdict")[0].lower() \
        or "do not demand" in prompt.lower()
    assert "decision owners" not in prompt.lower() or "those belong to the spec" \
        in prompt.lower()


def test_spec_review_prompt_keeps_rigor(tmp_errorta_home: Path) -> None:
    store, gov = _store("f100-02-a2-spec")
    art = gov.append_artifact(kind="spec", title="Spec", state="under_review")

    prompt = build_governance_review_prompt(
        store=store, governance=gov, artifact=art, reviewer_role="reviewer")

    assert "acceptance criteria" in prompt.lower()
    assert "scope creep" in prompt.lower()
    # The brainstorm-only "high-level" calibration must not bleed into spec.
    assert "BRAINSTORM" not in prompt


def test_plan_review_prompt_keeps_rigor(tmp_errorta_home: Path) -> None:
    store, gov = _store("f100-02-a2-plan")
    art = gov.append_artifact(
        kind="implementation_plan", title="Plan", state="under_review")

    prompt = build_governance_review_prompt(
        store=store, governance=gov, artifact=art, reviewer_role="reviewer")

    assert "acceptance criteria" in prompt.lower()
    assert "oversize slices" in prompt.lower()


# --- A3: redraft injects the prior findings --------------------------------
def test_redraft_prompt_injects_unresolved_findings(tmp_errorta_home: Path) -> None:
    store, gov = _store("f100-02-a3")
    art = gov.append_artifact(kind="brainstorm", title="BS", state="under_review")
    gov.update_state(mode="strict", phase="reviewing_brainstorm")
    gov.append_review(
        artifact_id=art.artifact_id,
        reviewer_member_id="m-rev",
        verdict="request_changes",
        findings=[
            {"severity": "high", "title": "Missing problem statement",
             "body": "State the core problem.", "blocking": True},
            {"severity": "low", "title": "Tighten audience",
             "body": "Who is this for?", "blocking": False},
        ],
        reviewer_role="reviewer",
    )
    # settle resolves the brainstorm to changes_requested + phase -> brainstorming.
    gov.settle_artifact_after_review(art.artifact_id, "strict")

    prompt = build_pm_governance_prompt(
        store=store, governance=gov, phase="brainstorming")

    assert "Missing problem statement" in prompt
    assert "State the core problem." in prompt
    assert "Tighten audience" in prompt
    assert "deferred to spec" in prompt
    assert "BLOCKING" in prompt
    # Blocking finding precedes the non-blocking one.
    assert prompt.index("Missing problem statement") < prompt.index("Tighten audience")


def test_fresh_brainstorm_prompt_has_no_findings_block(tmp_errorta_home: Path) -> None:
    store, gov = _store("f100-02-a3-fresh")
    gov.update_state(mode="strict", phase="brainstorming")

    prompt = build_pm_governance_prompt(
        store=store, governance=gov, phase="brainstorming")

    assert "UNRESOLVED REVIEW FINDINGS" not in prompt
