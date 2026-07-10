"""Tests for errorta_judge.correction_draft.draft_correction."""
from __future__ import annotations

from errorta_judge.correction_draft import draft_correction


def test_pass_rating_returns_answer_stripped() -> None:
    out = draft_correction("  the answer  ", {"rating": "pass"})
    assert out == "the answer"


def test_fail_rating_prepends_judge_reason_and_tags() -> None:
    verdict = {
        "rating": "fail",
        "reason": "missed citation",
        "failure_tags": ["hallucination", "missing-citation"],
    }
    out = draft_correction("original answer", verdict)
    assert "Judge said: missed citation" in out
    assert "Tags: hallucination, missing-citation" in out
    assert "Corrected answer:\noriginal answer" in out


def test_partial_rating_treated_like_fail() -> None:
    verdict = {"rating": "partial", "reason": "half right", "failure_tags": ["incomplete"]}
    out = draft_correction("body", verdict)
    assert "Judge said: half right" in out
    assert "Tags: incomplete" in out
    assert out.endswith("body")


def test_empty_body_with_header_returns_header() -> None:
    verdict = {"rating": "fail", "reason": "no source", "failure_tags": []}
    out = draft_correction("", verdict)
    assert out == "Judge said: no source"


def test_empty_body_no_header_returns_placeholder() -> None:
    out = draft_correction("", {"rating": "fail"})
    assert out == "Add the correct answer here."


def test_missing_verdict_fields_defaults_to_fail_branch() -> None:
    # No rating, no reason, no tags — header is empty, body is preserved.
    out = draft_correction("just the body", {})
    assert out == "just the body"


def test_tags_only_no_reason() -> None:
    out = draft_correction("ans", {"rating": "fail", "failure_tags": ["x"]})
    assert "Tags: x" in out
    assert "Judge said:" not in out
    assert out.endswith("ans")
