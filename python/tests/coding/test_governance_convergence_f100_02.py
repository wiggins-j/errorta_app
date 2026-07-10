"""F100-02 Slice 2 — round cap + no-progress + stuck stop + status.

Locks A1: the governance review loop converges or stops. A brainstorm that keeps
getting ``changes_requested`` reaches the cap and returns a HARD_BLOCKER
``governance_review_not_converging`` (a "needs you" stop, not a silent
no_progress); byte-identical resubmission trips no-progress; the status
projection reports ``stuck`` at the cap; ``max_review_rounds`` round-trips.
"""
from __future__ import annotations

import json
from pathlib import Path

from errorta_council.coding.governance import (
    DEFAULT_MAX_REVIEW_ROUNDS,
    GovernanceState,
    GovernanceStore,
)
from errorta_council.coding.governance_status import governance_status
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.runner import build_run_turn, members_by_coding_role
from errorta_council.coding.topology import GovernanceReview

_MEMBERS = [
    {"id": "m-pm", "enabled": True, "metadata": {"coding_role": "pm"}},
    {"id": "m-dev", "enabled": True, "metadata": {"coding_role": "dev"}},
    {"id": "m-rev", "enabled": True, "metadata": {"coding_role": "reviewer"}},
]

_BY_ROLE = {
    "pm": [{"id": "m-pm", "name": "PM-Prime", "metadata": {"coding_role": "pm"}}],
    "reviewer": [{"id": "m-rev", "name": "Echo-REV",
                  "metadata": {"coding_role": "reviewer"}}],
}

_FINDING = {"severity": "medium", "title": "needs work", "body": "fix it",
            "blocking": True}


def _store(project_id: str) -> tuple[LedgerStore, GovernanceStore]:
    store = LedgerStore(project_id)
    store.create_project(
        north_star="x", definition_of_done="d", target="new", repo_path=None)
    gov = GovernanceStore.for_ledger(store)
    gov.update_state(mode="strict", phase="reviewing_brainstorm")
    return store, gov


def _step(steps: list[dict], stage: str) -> str:
    return next(s["state"] for s in steps if s["stage"] == stage)


def _seed_rejected_brainstorm(gov: GovernanceStore, body: str) -> str:
    """Append a brainstorm version + a request_changes review and settle it."""
    art = gov.append_artifact(
        kind="brainstorm", title="BS", body_markdown=body, state="under_review")
    gov.append_review(
        artifact_id=art.artifact_id, reviewer_member_id="m-rev",
        verdict="request_changes", findings=[_FINDING], reviewer_role="reviewer")
    gov.settle_artifact_after_review(art.artifact_id, "strict")
    return art.artifact_id


# --- store helpers ---------------------------------------------------------
def test_review_round_count_per_kind(tmp_errorta_home: Path) -> None:
    store, gov = _store("f100-02-rrc")
    _seed_rejected_brainstorm(gov, "body v1")
    _seed_rejected_brainstorm(gov, "body v2")
    assert gov.review_round_count("brainstorm") == 2
    # A spec stage starts fresh — per-kind counting auto-resets.
    assert gov.review_round_count("spec") == 0


def test_no_progress_streak_exact_hash(tmp_errorta_home: Path) -> None:
    store, gov = _store("f100-02-nps")
    _seed_rejected_brainstorm(gov, "identical body")
    assert gov.no_progress_streak("brainstorm") == 0  # one version
    _seed_rejected_brainstorm(gov, "identical body")
    assert gov.no_progress_streak("brainstorm") == 1
    _seed_rejected_brainstorm(gov, "identical body")
    assert gov.no_progress_streak("brainstorm") == 2
    # A real change breaks the streak.
    _seed_rejected_brainstorm(gov, "actually different now")
    assert gov.no_progress_streak("brainstorm") == 0


def test_max_review_rounds_round_trips(tmp_errorta_home: Path) -> None:
    store, gov = _store("f100-02-cap-rt")
    assert gov.load_state().max_review_rounds == DEFAULT_MAX_REVIEW_ROUNDS
    gov.update_state(max_review_rounds=5)
    assert gov.load_state().max_review_rounds == 5
    # Garbage/0 coerces back to the default.
    saved = gov.save_state(GovernanceState.from_dict(
        {**gov.load_state().to_dict(), "max_review_rounds": 0}))
    assert saved.max_review_rounds == DEFAULT_MAX_REVIEW_ROUNDS


# --- stuck stop (runner) ---------------------------------------------------
def test_review_loop_stops_at_cap_with_not_converging(tmp_errorta_home: Path) -> None:
    store, gov = _store("f100-02-cap-stop")
    gov.update_state(max_review_rounds=3)
    # Two prior rejected versions (the PM revised >=1 time), distinct bodies.
    _seed_rejected_brainstorm(gov, "body v1")
    _seed_rejected_brainstorm(gov, "body v2")
    assert len(gov.list_artifacts(kind="brainstorm")) >= 2  # PM revised >=1x

    # Third version under review; the runner rejects it -> round 3 == cap.
    art3 = gov.append_artifact(
        kind="brainstorm", title="BS", body_markdown="body v3",
        state="under_review")
    gov.update_state(phase="reviewing_brainstorm")

    def caller(member, prompt):  # noqa: ANN001
        return json.dumps({
            "schema_version": "governance_turn.v1", "role": "reviewer",
            "intent": {"kind": "artifact_review", "artifact_id": art3.artifact_id,
                       "verdict": "request_changes", "findings": [_FINDING]},
        })

    rt = build_run_turn(store, None, members_by_coding_role(_MEMBERS), caller,
                        guardrail_enabled=True)
    outcome = rt(
        GovernanceReview(member_id="m-rev", artifact_id=art3.artifact_id,
                         reviewer_role="reviewer"),
        store,
    )
    assert outcome.hard_blocker is True
    assert outcome.reason == "governance_review_not_converging"
    assert outcome.made_progress is False
    assert gov.review_round_count("brainstorm") == 3
    decisions = [d for d in store.list_decisions()
                 if d.get("choice") == "governance_review_not_converging"]
    assert decisions


def test_light_mode_pm_finalizes_at_cap_without_human(tmp_errorta_home: Path) -> None:
    # Light = the PM is the final authority. When the reviewer deadlocks a spec
    # past the cap, the PM finalizes its best version and the run PROCEEDS — the
    # human is NOT pulled in (no hard_blocker, no not-converging stop).
    store, gov = _store("f100-02-light-finalize")
    gov.update_state(mode="light", max_review_rounds=3, phase="reviewing_spec")
    for body in ("spec v1", "spec v2"):
        art = gov.append_artifact(
            kind="spec", title="SPEC", body_markdown=body, state="under_review")
        gov.append_review(
            artifact_id=art.artifact_id, reviewer_member_id="m-rev",
            verdict="request_changes", findings=[_FINDING], reviewer_role="reviewer")
        gov.settle_artifact_after_review(art.artifact_id, "light")

    art3 = gov.append_artifact(
        kind="spec", title="SPEC", body_markdown="spec v3", state="under_review")
    gov.update_state(phase="reviewing_spec")

    def caller(member, prompt):  # noqa: ANN001
        return json.dumps({
            "schema_version": "governance_turn.v1", "role": "reviewer",
            "intent": {"kind": "artifact_review", "artifact_id": art3.artifact_id,
                       "verdict": "request_changes", "findings": [_FINDING]},
        })

    rt = build_run_turn(store, None, members_by_coding_role(_MEMBERS), caller,
                        guardrail_enabled=True)
    outcome = rt(
        GovernanceReview(member_id="m-rev", artifact_id=art3.artifact_id,
                         reviewer_role="reviewer"),
        store,
    )
    assert outcome.hard_blocker is False
    assert outcome.reason != "governance_review_not_converging"
    assert gov.latest_artifact("spec").state == "approved"
    finalized = [d for d in store.list_decisions()
                 if d.get("choice") == "governance_pm_finalized"]
    assert finalized, "expected a PM-finalized decision in light mode"
    assert not [d for d in store.list_decisions()
                if d.get("choice") == "governance_review_not_converging"]


def test_review_loop_stops_on_no_progress_before_cap(tmp_errorta_home: Path) -> None:
    store, gov = _store("f100-02-nps-stop")
    gov.update_state(max_review_rounds=10)  # well above the streak threshold
    # Byte-identical resubmissions: v1 + v2 identical (streak builds to 2 with v3).
    _seed_rejected_brainstorm(gov, "same body")
    _seed_rejected_brainstorm(gov, "same body")

    art3 = gov.append_artifact(
        kind="brainstorm", title="BS", body_markdown="same body",
        state="under_review")
    gov.update_state(phase="reviewing_brainstorm")

    def caller(member, prompt):  # noqa: ANN001
        return json.dumps({
            "schema_version": "governance_turn.v1", "role": "reviewer",
            "intent": {"kind": "artifact_review", "artifact_id": art3.artifact_id,
                       "verdict": "request_changes", "findings": [_FINDING]},
        })

    rt = build_run_turn(store, None, members_by_coding_role(_MEMBERS), caller,
                        guardrail_enabled=True)
    outcome = rt(
        GovernanceReview(member_id="m-rev", artifact_id=art3.artifact_id,
                         reviewer_role="reviewer"),
        store,
    )
    assert outcome.hard_blocker is True
    assert outcome.reason == "governance_review_not_converging"
    assert gov.no_progress_streak("brainstorm") == 2


def test_review_under_cap_does_not_stop(tmp_errorta_home: Path) -> None:
    store, gov = _store("f100-02-under-cap")
    gov.update_state(max_review_rounds=3)
    _seed_rejected_brainstorm(gov, "body v1")  # 1 prior round

    art2 = gov.append_artifact(
        kind="brainstorm", title="BS", body_markdown="body v2",
        state="under_review")
    gov.update_state(phase="reviewing_brainstorm")

    def caller(member, prompt):  # noqa: ANN001
        return json.dumps({
            "schema_version": "governance_turn.v1", "role": "reviewer",
            "intent": {"kind": "artifact_review", "artifact_id": art2.artifact_id,
                       "verdict": "request_changes", "findings": [_FINDING]},
        })

    rt = build_run_turn(store, None, members_by_coding_role(_MEMBERS), caller,
                        guardrail_enabled=True)
    outcome = rt(
        GovernanceReview(member_id="m-rev", artifact_id=art2.artifact_id,
                         reviewer_role="reviewer"),
        store,
    )
    # 2 rounds < cap 3, distinct bodies -> keep going, no stop.
    assert outcome.hard_blocker is False
    assert gov.review_round_count("brainstorm") == 2


# --- status projection -----------------------------------------------------
def test_status_reports_stuck_at_cap(tmp_errorta_home: Path) -> None:
    store, gov = _store("f100-02-status-stuck")
    gov.update_state(max_review_rounds=3)
    # settle flips the phase to the revision (brainstorming) phase — the real
    # state a stopped, not-converging run lands in.
    _seed_rejected_brainstorm(gov, "v1")
    _seed_rejected_brainstorm(gov, "v2")
    _seed_rejected_brainstorm(gov, "v3")  # rounds == cap
    assert gov.load_state().phase == "brainstorming"

    out = governance_status(store, _BY_ROLE, run_active=False)
    assert out["status"] == "stuck"
    assert out["needs_human"] is True
    assert out["review_round"] == 3
    assert "needs you" in out["headline"]
    assert "3 rounds" in out["headline"]
    assert _step(out["steps"], "brainstorm") == "stuck"


def test_status_reports_stuck_in_reviewing_phase(tmp_errorta_home: Path) -> None:
    # Also stuck when surfaced from the reviewing phase (the active artifact is
    # still under_review but the prior versions already hit the cap).
    store, gov = _store("f100-02-status-stuck-rev")
    gov.update_state(max_review_rounds=3)
    _seed_rejected_brainstorm(gov, "v1")
    _seed_rejected_brainstorm(gov, "v2")
    _seed_rejected_brainstorm(gov, "v3")
    gov.update_state(phase="reviewing_brainstorm")

    out = governance_status(store, _BY_ROLE, run_active=False)
    assert out["status"] == "stuck"
    assert out["needs_human"] is True
    assert out["review_round"] == 3
    assert _step(out["steps"], "brainstorm") == "stuck"


def test_status_normal_under_cap(tmp_errorta_home: Path) -> None:
    store, gov = _store("f100-02-status-normal")
    gov.update_state(max_review_rounds=3)
    _seed_rejected_brainstorm(gov, "v1")  # 1 round < cap, phase -> brainstorming

    out = governance_status(store, _BY_ROLE, run_active=False)
    assert out["status"] == "drafting"
    assert out["needs_human"] is False
    assert out["review_round"] is None
