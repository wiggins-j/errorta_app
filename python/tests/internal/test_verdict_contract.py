"""INTERNAL CONTRACT TEST ONLY. Verdict shape may evolve; this file is the canary."""
# ---------------------------------------------------------------------------
# Step 0 enumeration: cases already covered by python/tests/test_judge_schema_guard.py
# (do not duplicate below):
#
#   - test_valid_dict_passthrough: canonical dict with rating=pass round-trips
#   - test_fenced_json_string: ```json\n{...}\n``` parses
#   - test_fenced_plain_string: ``` (no language) wraps verdict
#   - test_partial_json_embedded_in_prose: single braced JSON in prose extracted
#   - test_comma_separated_tag_string: "a, b, " -> ["a","b"]
#   - test_confidence_nan_becomes_none: math.nan -> None
#   - test_confidence_clamped_above_one: 5.0 -> 1.0
#   - test_confidence_clamped_below_zero: -2.5 -> 0.0
#   - test_confidence_infinity_clamped: math.inf -> 1.0
#   - test_confidence_unparseable_becomes_none: "high" -> None
#   - test_rating_aliases (parametrized): partial-pass/partially correct/mixed
#       -> partial; correct/ok/good -> pass; incorrect/wrong/bad -> fail;
#       case-insensitive ("PASS","  Fail  ")
#   - test_missing_rating_fallback_to_unparseable: {"reason":...}
#       -> rating=fail + judge_unparseable
#   - test_unrecognized_rating_fallback: "splendid" -> unparseable
#   - test_total_garbage_string_falls_back: gibberish -> unparseable
#   - test_empty_string_falls_back: "" -> unparseable
#   - test_alternate_key_verdict_and_rationale: verdict+rationale aliases
#   - test_alternate_key_tags: "tags" alias for failure_tags
#   - test_non_string_rating_returns_unparseable: rating=1 -> unparseable
#
# Cases below cover the remaining surface area: 4 valid-passthrough,
# 5 + 3 drifted-shape/alias, 3 + 4 missing-field/confidence-parse,
# 4 malformed, 2 + 3 fenced-JSON, 2 + 2 embedded-in-prose,
# 3 numeric-extremes (35 total when pytest unfolds the parametrize block).
# ---------------------------------------------------------------------------
from __future__ import annotations

from typing import Any

import pytest

from errorta_judge import schema_guard

pytestmark = pytest.mark.internal_contract


# --- Group 1: valid-passthrough (4) -----------------------------------------
# Canonical verdicts in each of the three ratings, plus one with all fields
# populated as a string-form JSON object. These lock the "happy path" so any
# future refactor of normalize_verdict cannot quietly drop a field.

@pytest.mark.parametrize(
    "raw",
    [
        {"rating": "pass", "reason": "matches sources", "failure_tags": [], "confidence": 1.0},
        {"rating": "partial", "reason": "weak citation", "failure_tags": ["weak-cite"], "confidence": 0.5},
        {"rating": "fail", "reason": "hallucinated", "failure_tags": ["hallucination"], "confidence": 0.1},
        {"rating": "pass", "reason": None, "failure_tags": [], "confidence": None},
    ],
    ids=["pass-full", "partial-full", "fail-full", "pass-minimal"],
)
def test_valid_passthrough(raw: dict[str, Any]) -> None:
    out = schema_guard.normalize_verdict(raw)
    assert out["rating"] == raw["rating"]
    assert out["failure_tags"] == raw["failure_tags"]
    assert out["confidence"] == raw["confidence"]
    # reason normalization strips whitespace; None stays None
    assert out["reason"] == raw["reason"]


# --- Group 2: drifted-shape / alias (5) -------------------------------------
# Alias keys + alias values not already covered by the parametrize block in
# test_judge_schema_guard.py (which only tests `rating` field aliasing).

def test_drifted_shapes_score_key_with_alias_value() -> None:
    """`score` is accepted as a synonym of `rating`, and 'good' aliases to pass."""
    out = schema_guard.normalize_verdict({"score": "good"})
    assert out["rating"] == "pass"


def test_drifted_shapes_explanation_as_reason_alias() -> None:
    """`explanation` is the third accepted alias for `reason`."""
    out = schema_guard.normalize_verdict({"rating": "pass", "explanation": "because reasons"})
    assert out["reason"] == "because reasons"


def test_drifted_shapes_tuple_failure_tags() -> None:
    """Any non-string Iterable of tags coerces via str() on each element."""
    out = schema_guard.normalize_verdict({"rating": "fail", "failure_tags": ("x", "y", "")})
    # empty entries get stripped (str("").strip() is falsy)
    assert out["failure_tags"] == ["x", "y"]


def test_drifted_shapes_reason_whitespace_stripped() -> None:
    """Reason is .strip()'d when it's a string."""
    out = schema_guard.normalize_verdict({"rating": "pass", "reason": "   spaced   "})
    assert out["reason"] == "spaced"


def test_drifted_shapes_rating_with_surrounding_whitespace_and_case() -> None:
    """Rating is lower-cased and stripped before alias lookup."""
    out = schema_guard.normalize_verdict({"rating": "  Partially Correct  "})
    assert out["rating"] == "partial"


# --- Group 3: missing-field (3) ---------------------------------------------
# Beyond test_missing_rating_fallback_to_unparseable, lock how *other* missing
# fields are handled (they default rather than triggering unparseable).

def test_missing_fields_confidence_absent_is_none() -> None:
    out = schema_guard.normalize_verdict({"rating": "pass"})
    assert out["confidence"] is None


def test_missing_fields_failure_tags_absent_is_empty_list() -> None:
    out = schema_guard.normalize_verdict({"rating": "pass"})
    assert out["failure_tags"] == []


def test_missing_fields_rating_null_treated_as_missing() -> None:
    """Explicit `rating: null` is the same as absent: unparseable fallback."""
    out = schema_guard.normalize_verdict({"rating": None})
    assert out["rating"] == "fail"
    assert "judge_unparseable" in out["failure_tags"]


# --- Group 4: malformed (4) -------------------------------------------------
# Degenerate shapes that aren't outright garbage but violate the contract.

def test_malformed_non_list_failure_tags() -> None:
    """Non-iterable, non-string failure_tags (e.g. an int) coerce to []."""
    out = schema_guard.normalize_verdict({"rating": "fail", "failure_tags": 42})
    assert out["failure_tags"] == []
    # rating still survives
    assert out["rating"] == "fail"


def test_malformed_raw_is_list_not_dict() -> None:
    """Top-level JSON arrays do not satisfy isinstance(dict) -> unparseable."""
    out = schema_guard.normalize_verdict(["rating", "pass"])
    assert out["rating"] == "fail"
    assert out["failure_tags"] == ["judge_unparseable"]


def test_malformed_dict_form_json_string_in_rating() -> None:
    """A nested JSON-as-string rating like '{\"x\":1}' is not a known alias."""
    out = schema_guard.normalize_verdict({"rating": '{"x":1}'})
    assert out["rating"] == "fail"
    assert "judge_unparseable" in out["failure_tags"]


def test_malformed_confidence_as_bool_quirk() -> None:
    """LOCKED QUIRK: bool is a subclass of int in Python, so True coerces to
    float(1.0) and clamps to 1.0. Documented here so a future refactor that
    rejects bool doesn't break silently."""
    out = schema_guard.normalize_verdict({"rating": "pass", "confidence": True})
    assert out["confidence"] == 1.0


# --- Group 5: fenced-JSON (2) -----------------------------------------------
# Beyond the two single-fence cases in test_judge_schema_guard.py, lock the
# variants that come up in practice.

def test_fenced_json_with_extra_leading_whitespace() -> None:
    raw = '   ```json\n{"rating": "pass", "confidence": 0.7}\n```   '
    out = schema_guard.normalize_verdict(raw)
    assert out["rating"] == "pass"
    assert out["confidence"] == 0.7


def test_fenced_json_upper_case_language_tag() -> None:
    """Fence regex is case-insensitive on the language tag."""
    raw = '```JSON\n{"rating": "partial"}\n```'
    out = schema_guard.normalize_verdict(raw)
    assert out["rating"] == "partial"


# --- Group 6: embedded-in-prose (2) -----------------------------------------

def test_embedded_in_prose_multiline_with_braced_block() -> None:
    raw = (
        "Here is my verdict, after careful analysis:\n"
        '{"rating": "fail", "failure_tags": ["hallucination"], "confidence": 0.2}\n'
        "Let me know if you want more detail."
    )
    out = schema_guard.normalize_verdict(raw)
    assert out["rating"] == "fail"
    assert out["failure_tags"] == ["hallucination"]
    assert out["confidence"] == 0.2


def test_embedded_in_prose_two_json_blobs_greedy_regex_locked_quirk() -> None:
    """LOCKED QUIRK: _FIRST_JSON_RE is greedy (`\\{.*\\}` with DOTALL), so when
    the prose contains TWO braced blocks the regex spans from the first `{`
    to the last `}`, producing an unparseable composite. Plain json.loads
    on the full string also fails. Result: judge_unparseable.

    If the regex is ever made non-greedy, this test will start failing — at
    which point the new behavior should be reviewed before updating the
    assertion."""
    raw = 'first {"rating":"pass"} then {"rating":"fail"}'
    out = schema_guard.normalize_verdict(raw)
    assert out["rating"] == "fail"
    assert out["failure_tags"] == ["judge_unparseable"]


# --- Group 7: numeric-extremes (3) ------------------------------------------
# Per the plan: "NaN-string -> judge_unparseable, Inf-string -> judge_unparseable,
# in-range out-of-bounds float (e.g. 1.5) -> clamped [0,1]". In practice
# Python's stdlib json.loads accepts the JavaScript-extension literals NaN
# and Infinity by default (no `parse_constant` override is wired in
# schema_guard), so the actual current behavior diverges from the plan
# wording. Per the plan's step 6, we lock the actual behavior here with an
# inline comment, NOT the prescribed behavior.

def test_numeric_extremes_nan_string_quirk() -> None:
    """LOCKED QUIRK: Python json.loads accepts the non-standard literal `NaN`,
    so the verdict parses; _coerce_confidence then rejects the resulting
    float('nan') and returns None. The rating field survives. If
    schema_guard ever passes `parse_constant=...` to json.loads, this case
    will start hitting the judge_unparseable path and should be re-locked."""
    out = schema_guard.normalize_verdict('{"rating": "pass", "confidence": NaN}')
    assert out["rating"] == "pass"
    assert out["confidence"] is None


def test_numeric_extremes_infinity_string_quirk() -> None:
    """LOCKED QUIRK: same as above for `Infinity`. The float is finite-clamped
    to 1.0 by _coerce_confidence's max(0.0, min(1.0, f)) (because
    min(1.0, inf) == 1.0)."""
    out = schema_guard.normalize_verdict('{"rating": "pass", "confidence": Infinity}')
    assert out["rating"] == "pass"
    assert out["confidence"] == 1.0


def test_numeric_extremes_in_range_out_of_bounds_clamps() -> None:
    """Plain in-range out-of-bounds float (1.5) clamps to 1.0."""
    out = schema_guard.normalize_verdict({"rating": "pass", "confidence": 1.5})
    assert out["confidence"] == 1.0


# --- Group 3b: missing-field — confidence parse paths (+4) ------------------
# F-INFRA-03 lift cycle: lock the four under-covered confidence-coercion
# branches in errorta_judge.schema_guard._coerce_confidence (schema_guard.py:54).
# Together with the existing nan/inf/clamping tests, these freeze the public
# behavior of every input shape that _coerce_confidence is asked to handle.

def test_confidence_string_decimal_parses() -> None:
    """`confidence: "0.5"` parses via float() in _coerce_confidence and survives."""
    out = schema_guard.normalize_verdict({"rating": "pass", "confidence": "0.5"})
    assert out["confidence"] == 0.5


def test_confidence_scientific_notation_parses() -> None:
    """`confidence: "1e-3"` parses via float() and resolves to 0.001."""
    out = schema_guard.normalize_verdict({"rating": "pass", "confidence": "1e-3"})
    assert out["confidence"] == 0.001


def test_confidence_leading_whitespace_string_parses() -> None:
    """LOCKED QUIRK: Python's float() accepts leading/trailing whitespace, so
    confidence="  0.7  " becomes 0.7 once _coerce_confidence calls float() at
    schema_guard.py:56. If _coerce_confidence ever adds a .strip()-then-validate
    guard, stops calling float() directly, or starts rejecting whitespace-padded
    numeric strings, this case will need to be re-locked."""
    out = schema_guard.normalize_verdict({"rating": "pass", "confidence": "  0.7  "})
    assert out["confidence"] == 0.7


def test_confidence_percentage_string_unparseable() -> None:
    """`confidence: "50%"` cannot be float()'d -> _coerce_confidence catches
    ValueError and returns None. The rating field survives unchanged."""
    out = schema_guard.normalize_verdict({"rating": "pass", "confidence": "50%"})
    assert out["rating"] == "pass"
    assert out["confidence"] is None


# --- Group 5b: fenced-JSON — fence variants (+3) ----------------------------
# F-INFRA-03 lift cycle: lock the fence-handling variants. These all flow
# through _FENCE_RE (schema_guard.py:18) and _strip_fences (schema_guard.py:22)
# but exercise edges the original 2 fenced-JSON cases did not.

def test_fenced_json_tilde_fence_not_recognized_quirk() -> None:
    """LOCKED QUIRK: _FENCE_RE only matches backtick fences (```), not tilde
    fences (~~~). A tilde-fenced verdict does not get its fence stripped, but
    the embedded-prose _FIRST_JSON_RE path still picks up the braced block
    inside it. If _FENCE_RE is ever broadened to accept tilde fences, this
    test will continue to pass (the rating still resolves) but the parse path
    will differ — the assertion is documenting the current end-to-end
    behavior, not the specific code path."""
    raw = '~~~json\n{"rating": "pass"}\n~~~'
    out = schema_guard.normalize_verdict(raw)
    assert out["rating"] == "pass"


def test_fenced_json_with_trailing_text_after_closing_fence() -> None:
    """Trailing prose after the closing fence is tolerated; the embedded-JSON
    fallback (_FIRST_JSON_RE) picks up the inner braced block even though the
    full string is no longer just a fenced block."""
    raw = '```json\n{"rating": "partial"}\n```\nthat is my final answer'
    out = schema_guard.normalize_verdict(raw)
    assert out["rating"] == "partial"


def test_fenced_json_empty_fenced_block_unparseable() -> None:
    """An empty fence (```json\\n```) produces no JSON candidate — fence-strip
    yields empty, _FIRST_JSON_RE finds no brace pair, json.loads fails on the
    original. Falls through to judge_unparseable."""
    out = schema_guard.normalize_verdict('```json\n```')
    assert out["rating"] == "fail"
    assert "judge_unparseable" in out["failure_tags"]


# --- Group 2b: drifted-shape / alias coverage (+3) --------------------------
# F-INFRA-03 lift cycle: lock three additional alias edges that the original
# Group 2 left uncovered.

def test_alias_rationale_as_reason_third_path() -> None:
    """`rationale` is the second reason alias (between `reason` and
    `explanation`) per schema_guard.py:108. Lock it explicitly so future
    refactors of that fallback chain do not silently drop it."""
    out = schema_guard.normalize_verdict({"rating": "pass", "rationale": "looks right"})
    assert out["reason"] == "looks right"


def test_alias_tags_with_mixed_type_contents() -> None:
    """`tags` is the alias for failure_tags routed through _coerce_tags's
    Iterable branch. Mixed-type entries are str()-coerced (so int 7 becomes
    "7" and None becomes "None") and only blank-string entries are dropped —
    None survives as the string "None" precisely because str(None) == "None"
    is truthy."""
    out = schema_guard.normalize_verdict({"rating": "fail", "tags": ["a", 7, "", None]})
    assert out["failure_tags"] == ["a", "7", "None"]


def test_unknown_rating_value_falls_through_to_unparseable() -> None:
    """Rating value 'unknown' is not in VALID_RATINGS and not present in any
    of _coerce_rating's alias buckets -> _coerce_rating returns None ->
    normalize_verdict produces the judge_unparseable fallback shape."""
    out = schema_guard.normalize_verdict({"rating": "unknown"})
    assert out["rating"] == "fail"
    assert "judge_unparseable" in out["failure_tags"]


# --- Group 6b: embedded-in-prose — additional edges (+2) --------------------
# F-INFRA-03 lift cycle: lock two more prose-embedding edges.

def test_embedded_in_prose_inside_markdown_blockquote() -> None:
    """LOCKED QUIRK: _FIRST_JSON_RE is `\\{.*\\}` with DOTALL — it picks up the
    brace pair regardless of the leading `> ` blockquote prefix because the
    regex never anchors. If the regex ever gains a `^` anchor or line-mode
    handling, blockquote-embedded verdicts will start falling through and
    this case should be re-locked."""
    raw = "> reviewer says:\n> {\"rating\": \"pass\"}\n"
    out = schema_guard.normalize_verdict(raw)
    assert out["rating"] == "pass"


def test_embedded_in_prose_with_literal_newlines_in_string_values() -> None:
    """A JSON value containing a literal `\\n` escape parses cleanly via
    json.loads — the resulting Python string carries an actual newline.
    Reason whitespace is .strip()'d at the boundary but interior newlines
    survive untouched."""
    raw = '{"rating": "pass", "reason": "first line\\nsecond line"}'
    out = schema_guard.normalize_verdict(raw)
    assert out["rating"] == "pass"
    assert out["reason"] == "first line\nsecond line"
