"""F100 bugfix (2026-06-22) — robust strict artifact review.

Locks the lenient/normalizing parse (B1), the sharpened review prompt (B2),
and the runner corrective-retry + hard_blocker resilience (B3) against the exact
``bible-thoughts`` failure: a reviewer that says ``needs_work`` with richer
finding fields used to dead-end the run with ``no_progress``.
"""
from __future__ import annotations

import json
from pathlib import Path

from errorta_council.coding.governance import GovernanceStore
from errorta_council.coding.governance_prompts import build_governance_review_prompt
from errorta_council.coding.governance_schemas import (
    GovernanceTurnParseError,
    ReviewerArtifactReviewIntent,
    parse_governance_turn,
)
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.runner import (
    _INTENT_CORRECTIVE_RETRIES,
    build_run_turn,
    members_by_coding_role,
)
from errorta_council.coding.topology import GovernanceReview

_MEMBERS = [
    {"id": "m-pm", "enabled": True, "metadata": {"coding_role": "pm"}},
    {"id": "m-dev", "enabled": True, "metadata": {"coding_role": "dev"}},
    {"id": "m-rev", "enabled": True, "metadata": {"coding_role": "reviewer"}},
    {"id": "m-test", "enabled": True, "metadata": {"coding_role": "tester"}},
]


# The exact reviewer payload captured from the bible-thoughts run: a
# `needs_work` verdict plus a {severity, category, location, description}
# finding shape. This used to raise turn_schema_mismatch.
_BIBLE_THOUGHTS_FINDING = {
    "severity": "high",
    "category": "missing_acceptance_criteria",
    "location": "success_criteria",
    "description": "The brainstorm leaves success criteria vague and untestable.",
}


def _review_turn(role: str, verdict: str, findings: list[dict]) -> str:
    return json.dumps(
        {
            "schema_version": "governance_turn.v1",
            "role": role,
            "intent": {
                "kind": "artifact_review",
                "artifact_id": "art-1",
                "verdict": verdict,
                "findings": findings,
            },
        }
    )


def test_needs_work_normalizes_to_request_changes() -> None:
    parsed = parse_governance_turn(
        "reviewer", _review_turn("reviewer", "needs_work", [_BIBLE_THOUGHTS_FINDING])
    )
    assert not isinstance(parsed, GovernanceTurnParseError)
    assert isinstance(parsed.intent, ReviewerArtifactReviewIntent)
    assert parsed.intent.verdict == "request_changes"


def test_bible_thoughts_finding_coerces() -> None:
    parsed = parse_governance_turn(
        "reviewer", _review_turn("reviewer", "needs_work", [_BIBLE_THOUGHTS_FINDING])
    )
    assert not isinstance(parsed, GovernanceTurnParseError)
    finding = parsed.intent.findings[0]
    # title synthesized from `category` (humanized + title-cased)
    assert finding.title == "Missing Acceptance Criteria"
    # body lifted from `description`
    assert finding.body == _BIBLE_THOUGHTS_FINDING["description"]
    # severity preserved
    assert finding.severity == "high"
    # unknown keys (category, location) ignored, not present on the model
    assert not hasattr(finding, "category")


def test_title_synthesized_from_body_when_no_category() -> None:
    finding = {
        "severity": "medium",
        "detail": "The plan slice is oversize. Split it into two. Also rename it.",
    }
    parsed = parse_governance_turn(
        "reviewer", _review_turn("reviewer", "revise", [finding])
    )
    assert not isinstance(parsed, GovernanceTurnParseError)
    f = parsed.intent.findings[0]
    # `detail` -> body
    assert f.body.startswith("The plan slice is oversize")
    # title synthesized from the first clause of the body, capped
    assert f.title == "The plan slice is oversize"


def test_unknown_finding_severity_defaults_to_medium() -> None:
    parsed = parse_governance_turn(
        "reviewer",
        _review_turn(
            "reviewer",
            "needs_work",
            [{"severity": "serious", "title": "Ambiguous scope", "body": "Missing tests."}],
        ),
    )
    assert not isinstance(parsed, GovernanceTurnParseError)
    assert parsed.intent.findings[0].severity == "medium"


def test_verdict_synonyms_canonical_values() -> None:
    cases = {
        "approve": "approved",
        "lgtm": "approved",
        "ok": "approved",
        "pass": "approved",
        "changes_requested": "request_changes",
        "reject": "request_changes",
        "block": "blocked",
        "blocker": "blocked",
    }
    for raw, canonical in cases.items():
        findings = [] if canonical == "approved" else [{"title": "x", "body": "y"}]
        parsed = parse_governance_turn(
            "reviewer", _review_turn("reviewer", raw, findings)
        )
        assert not isinstance(parsed, GovernanceTurnParseError), raw
        assert parsed.intent.verdict == canonical, raw


def test_approved_empty_findings_still_valid() -> None:
    parsed = parse_governance_turn("reviewer", _review_turn("reviewer", "approved", []))
    assert not isinstance(parsed, GovernanceTurnParseError)
    assert parsed.intent.verdict == "approved"
    assert parsed.intent.findings == []


def test_non_approved_no_findings_still_rejected() -> None:
    parsed = parse_governance_turn(
        "reviewer", _review_turn("reviewer", "needs_work", [])
    )
    # verdict normalizes to request_changes, but the findings-required rule still
    # rejects the turn — the canonical contract stays strict.
    assert isinstance(parsed, GovernanceTurnParseError)


def test_garbage_verdict_still_fails() -> None:
    parsed = parse_governance_turn(
        "reviewer", _review_turn("reviewer", "banana", [{"title": "x"}])
    )
    assert isinstance(parsed, GovernanceTurnParseError)


def test_normalizer_fires_for_pm_dual_review() -> None:
    # The PM dual-review (role="pm", kind="artifact_review") routes through the
    # SAME ReviewerArtifactReviewIntent via _pm_intent, so the normalizer must
    # fire for the PM role too.
    parsed = parse_governance_turn(
        "pm", _review_turn("pm", "needs_work", [_BIBLE_THOUGHTS_FINDING])
    )
    assert not isinstance(parsed, GovernanceTurnParseError)
    assert isinstance(parsed.intent, ReviewerArtifactReviewIntent)
    assert parsed.intent.verdict == "request_changes"
    assert parsed.intent.findings[0].title == "Missing Acceptance Criteria"


def test_review_prompt_states_verdict_enum_and_finding_fields() -> None:
    class _Artifact:
        artifact_id = "art-1"
        artifact_kind = "brainstorm"
        version = 1
        state = "under_review"
        title = "Brainstorm"
        body_markdown = "..."
        body_json: dict = {}

    class _Governance:
        def list_reviews(self, *, artifact_id: str) -> list:
            return []

    class _Store:
        def get_project(self):  # noqa: ANN001
            raise RuntimeError("no project")

    prompt = build_governance_review_prompt(
        store=_Store(),
        governance=_Governance(),
        artifact=_Artifact(),  # type: ignore[arg-type]
        reviewer_role="reviewer",
    )
    for token in ("approved", "request_changes", "blocked",
                  '"title"', '"body"', '"blocking"'):
        assert token in prompt, token


# --- B3: runner corrective-retry + hard_blocker resilience ------------------


def _gov_store(project_id: str) -> tuple[LedgerStore, GovernanceStore, str]:
    store = LedgerStore(project_id)
    store.create_project(
        north_star="x", definition_of_done="d", target="new", repo_path=None
    )
    governance = GovernanceStore.for_ledger(store)
    governance.update_state(mode="strict", phase="reviewing_brainstorm")
    artifact = governance.append_artifact(
        kind="brainstorm", title="Brainstorm", state="under_review",
    )
    return store, governance, artifact.artifact_id


def test_normalizable_review_parses_first_attempt_no_retry(
    tmp_errorta_home: Path,
) -> None:
    store, governance, artifact_id = _gov_store("govretry-ok")
    calls: list[str] = []

    def caller(member, prompt):  # noqa: ANN001
        calls.append(prompt)
        return json.dumps({
            "schema_version": "governance_turn.v1",
            "role": "reviewer",
            "intent": {
                "kind": "artifact_review",
                "artifact_id": artifact_id,
                "verdict": "needs_work",  # synonym -> request_changes
                "findings": [_BIBLE_THOUGHTS_FINDING],
            },
        })

    rt = build_run_turn(store, None, members_by_coding_role(_MEMBERS), caller,
                        guardrail_enabled=True)
    outcome = rt(
        GovernanceReview(member_id="m-rev", artifact_id=artifact_id,
                         reviewer_role="reviewer"),
        store,
    )
    assert outcome.kind == "governance_progress"
    assert outcome.hard_blocker is False
    # Parsed on the FIRST attempt — exactly one model call, no corrective retry.
    assert len(calls) == 1
    reviews = governance.list_reviews(artifact_id=artifact_id)
    assert reviews and reviews[-1].verdict == "request_changes"


def test_unparseable_review_retries_then_hard_blocker(
    tmp_errorta_home: Path,
) -> None:
    store, governance, artifact_id = _gov_store("govretry-bad")
    calls: list[str] = []

    def caller(member, prompt):  # noqa: ANN001
        calls.append(prompt)
        # A verdict the normalizer can NEVER save -> stays a schema mismatch.
        return json.dumps({
            "schema_version": "governance_turn.v1",
            "role": "reviewer",
            "intent": {
                "kind": "artifact_review",
                "artifact_id": artifact_id,
                "verdict": "banana",
                "findings": [{"title": "x", "body": "y"}],
            },
        })

    rt = build_run_turn(store, None, members_by_coding_role(_MEMBERS), caller,
                        guardrail_enabled=True)
    outcome = rt(
        GovernanceReview(member_id="m-rev", artifact_id=artifact_id,
                         reviewer_role="reviewer"),
        store,
    )
    # Clear blocker, NOT a bare no_progress.
    assert outcome.hard_blocker is True
    assert outcome.reason == "governance_review_unparseable"
    assert outcome.made_progress is False
    # Initial attempt + bounded corrective retries.
    assert len(calls) == 1 + _INTENT_CORRECTIVE_RETRIES
    # The corrective re-prompt restated the schema.
    assert "corrective retry" in calls[-1]
    # The audit trail still records the rejection(s).
    decisions = [d for d in store.list_decisions()
                 if d.get("choice") == "governance_review_turn_rejected"]
    assert decisions
