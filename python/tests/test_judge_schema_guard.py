"""Tests for errorta_judge.schema_guard.normalize_verdict."""
from __future__ import annotations

import math
from typing import Any

import pytest

from errorta_judge import schema_guard


def test_valid_dict_passthrough() -> None:
    raw = {
        "rating": "pass",
        "reason": "looks good",
        "failure_tags": ["a", "b"],
        "confidence": 0.8,
    }
    out = schema_guard.normalize_verdict(raw)
    assert out["rating"] == "pass"
    assert out["reason"] == "looks good"
    assert out["failure_tags"] == ["a", "b"]
    assert out["confidence"] == 0.8


def test_fenced_json_string() -> None:
    raw = '```json\n{"rating": "fail", "reason": "nope"}\n```'
    out = schema_guard.normalize_verdict(raw)
    assert out["rating"] == "fail"
    assert out["reason"] == "nope"


def test_fenced_plain_string() -> None:
    raw = '```\n{"rating": "partial"}\n```'
    out = schema_guard.normalize_verdict(raw)
    assert out["rating"] == "partial"


def test_partial_json_embedded_in_prose() -> None:
    raw = 'Sure! Here is the verdict: {"rating": "pass", "confidence": 0.5} — done.'
    out = schema_guard.normalize_verdict(raw)
    assert out["rating"] == "pass"
    assert out["confidence"] == 0.5


def test_comma_separated_tag_string() -> None:
    raw = {"rating": "fail", "failure_tags": "hallucination, missing-citation , "}
    out = schema_guard.normalize_verdict(raw)
    assert out["failure_tags"] == ["hallucination", "missing-citation"]


def test_confidence_nan_becomes_none() -> None:
    raw = {"rating": "pass", "confidence": math.nan}
    out = schema_guard.normalize_verdict(raw)
    assert out["confidence"] is None


def test_confidence_clamped_above_one() -> None:
    raw = {"rating": "pass", "confidence": 5.0}
    out = schema_guard.normalize_verdict(raw)
    assert out["confidence"] == 1.0


def test_confidence_clamped_below_zero() -> None:
    raw = {"rating": "pass", "confidence": -2.5}
    out = schema_guard.normalize_verdict(raw)
    assert out["confidence"] == 0.0


def test_confidence_infinity_clamped() -> None:
    raw = {"rating": "pass", "confidence": math.inf}
    out = schema_guard.normalize_verdict(raw)
    assert out["confidence"] == 1.0


def test_confidence_unparseable_becomes_none() -> None:
    raw = {"rating": "pass", "confidence": "high"}
    out = schema_guard.normalize_verdict(raw)
    assert out["confidence"] is None


@pytest.mark.parametrize(
    ("alias", "expected"),
    [
        ("partial-pass", "partial"),
        ("partially correct", "partial"),
        ("mixed", "partial"),
        ("correct", "pass"),
        ("ok", "pass"),
        ("good", "pass"),
        ("incorrect", "fail"),
        ("wrong", "fail"),
        ("bad", "fail"),
        ("PASS", "pass"),
        ("  Fail  ", "fail"),
    ],
)
def test_rating_aliases(alias: str, expected: str) -> None:
    out = schema_guard.normalize_verdict({"rating": alias})
    assert out["rating"] == expected


def test_missing_rating_fallback_to_unparseable() -> None:
    out = schema_guard.normalize_verdict({"reason": "no rating field"})
    assert out["rating"] == "fail"
    assert "judge_unparseable" in out["failure_tags"]


def test_unrecognized_rating_fallback() -> None:
    out = schema_guard.normalize_verdict({"rating": "splendid"})
    assert out["rating"] == "fail"
    assert "judge_unparseable" in out["failure_tags"]


def test_total_garbage_string_falls_back() -> None:
    out = schema_guard.normalize_verdict("definitely not json at all")
    assert out["rating"] == "fail"
    assert out["failure_tags"] == ["judge_unparseable"]
    assert out["confidence"] is None


def test_empty_string_falls_back() -> None:
    out = schema_guard.normalize_verdict("")
    assert out["rating"] == "fail"
    assert out["failure_tags"] == ["judge_unparseable"]


def test_alternate_key_verdict_and_rationale() -> None:
    raw: dict[str, Any] = {"verdict": "good", "rationale": "matches sources"}
    out = schema_guard.normalize_verdict(raw)
    assert out["rating"] == "pass"
    assert out["reason"] == "matches sources"


def test_alternate_key_tags() -> None:
    out = schema_guard.normalize_verdict({"rating": "fail", "tags": ["x", "y"]})
    assert out["failure_tags"] == ["x", "y"]


def test_non_string_rating_returns_unparseable() -> None:
    out = schema_guard.normalize_verdict({"rating": 1})
    assert out["rating"] == "fail"
    assert "judge_unparseable" in out["failure_tags"]
