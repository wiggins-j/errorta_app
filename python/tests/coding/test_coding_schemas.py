"""F087-09 — strict turn schema parser tests (valid + malformed every role)."""
from __future__ import annotations

import json
from pathlib import Path

from errorta_council.coding.schemas import (
    DeveloperToolPlanIntent,
    ParsedTurn,
    PMPlanIntent,
    ReviewerVerdictIntent,
    TesterPlanIntent,
    TurnErrorCode,
    TurnParseError,
    parse_coding_turn,
)

FIX = Path(__file__).parent / "fixtures" / "coding_turn_v1"


def _env(role, intent, *, task_id=None, version="coding_turn.v1"):
    e = {"schema_version": version, "role": role, "intent": intent}
    if task_id is not None:
        e["task_id"] = task_id
    return json.dumps(e)


# --- envelope ---------------------------------------------------------------


def test_non_json_is_turn_non_json():
    r = parse_coding_turn("dev", "t1", "not json at all")
    assert isinstance(r, TurnParseError) and r.code == TurnErrorCode.turn_non_json


def test_missing_schema_version_is_stamped_when_shape_is_clear():
    # F127: a turn missing only schema_version but with a clear role+intent is
    # stamped and accepted (intent still validated strictly) — leniency for weaker
    # workers, no fabrication of meaning.
    body = json.dumps({"role": "dev", "task_id": None,
                       "intent": {"kind": "tool_plan", "task_type": "documentation"}})
    r = parse_coding_turn("dev", None, body)
    assert not isinstance(r, TurnParseError)
    assert r.envelope.schema_version == "coding_turn.v1"


def test_role_mismatch():
    body = _env("reviewer", {"kind": "review_verdict", "reviewed_head": "h",
                "approved": True}, task_id="t1")
    r = parse_coding_turn("dev", "t1", body)
    assert isinstance(r, TurnParseError) and r.code == TurnErrorCode.role_mismatch


def test_task_mismatch_for_non_pm():
    body = _env("dev", {"kind": "tool_plan", "task_type": "documentation"}, task_id="OTHER")
    r = parse_coding_turn("dev", "t1", body)
    assert isinstance(r, TurnParseError) and r.code == TurnErrorCode.task_mismatch


def test_pm_task_id_not_required():
    body = _env("pm", {"kind": "plan", "done": False,
                "tasks": [{"title": "x", "role": "dev"}]})
    assert isinstance(parse_coding_turn("pm", None, body), ParsedTurn)


def test_prose_then_fenced_envelope_parses():
    # CLI models (claude_cli/codex_cli) reason in prose then emit the envelope as
    # a fenced ```json block at the END. The PM's done=true completion signal
    # arrives exactly this way — it must parse, not be rejected as turn_non_json
    # (which stalled real runs at no_progress instead of completing).
    env = _env("pm", {"kind": "plan", "done": True, "completion_summary": "all met"})
    body = ("Looking at the project state, all required files exist and the north "
            "star appears to be fully met.\n\n```json\n" + env + "\n```")
    r = parse_coding_turn("pm", None, body)
    assert isinstance(r, ParsedTurn)
    assert r.intent.done is True


def test_prose_then_bare_envelope_parses():
    # Same idea without a fence — the JSON object is embedded after prose.
    env = _env("reviewer", {"kind": "review_verdict", "reviewed_head": "h",
               "approved": True}, task_id="t1")
    body = "I reviewed the diff and it looks correct. " + env
    assert isinstance(parse_coding_turn("reviewer", "t1", body), ParsedTurn)


def test_prose_with_no_json_is_turn_non_json():
    # Pure prose with no JSON object at all still fails closed.
    r = parse_coding_turn("pm", None, "The project looks complete to me, nice work!")
    assert isinstance(r, TurnParseError) and r.code == TurnErrorCode.turn_non_json


def test_incidental_prose_braces_do_not_mask_the_envelope():
    # A response mentioning a non-JSON {brace} in prose, then the real envelope:
    # the envelope (schema_version) wins over the incidental object.
    env = _env("pm", {"kind": "plan", "done": False,
               "tasks": [{"title": "x", "role": "dev"}]})
    body = "Consider the mapping {key -> value} as context.\n" + env
    r = parse_coding_turn("pm", None, body)
    assert isinstance(r, ParsedTurn) and r.intent.done is False


def test_fenced_json_parses():
    body = "```json\n" + _env("tester", {"kind": "test_plan",
            "command_ids": ["unit"], "scope": "full_project"}, task_id="t1") + "\n```"
    assert isinstance(parse_coding_turn("tester", "t1", body), ParsedTurn)


# --- PM ---------------------------------------------------------------------


def test_pm_valid():
    body = _env("pm", {"kind": "plan", "done": False,
                "tasks": [{"title": "impl", "role": "dev"}]})
    r = parse_coding_turn("pm", None, body)
    assert isinstance(r, ParsedTurn) and isinstance(r.intent, PMPlanIntent)


def test_pm_missing_done_is_mismatch():
    body = _env("pm", {"kind": "plan", "tasks": [{"title": "x", "role": "dev"}]})
    assert parse_coding_turn("pm", None, body).code == TurnErrorCode.turn_schema_mismatch


def test_pm_done_false_empty_tasks_is_mismatch():
    body = _env("pm", {"kind": "plan", "done": False, "tasks": []})
    assert parse_coding_turn("pm", None, body).code == TurnErrorCode.turn_schema_mismatch


def test_pm_cannot_create_pm_tasks():
    body = _env("pm", {"kind": "plan", "done": False,
                "tasks": [{"title": "x", "role": "pm"}]})
    assert parse_coding_turn("pm", None, body).code == TurnErrorCode.turn_schema_mismatch


def test_pm_done_true_requires_completion_summary():
    body = _env("pm", {"kind": "plan", "done": True})
    assert parse_coding_turn("pm", None, body).code == TurnErrorCode.turn_schema_mismatch
    ok = _env("pm", {"kind": "plan", "done": True, "completion_summary": "all met"})
    assert isinstance(parse_coding_turn("pm", None, ok), ParsedTurn)


def test_pm_extra_summary_is_ignored():
    body = _env("pm", {"kind": "plan", "done": False,
                "tasks": [{"title": "impl", "role": "dev"}],
                "summary": "plan prose"})
    r = parse_coding_turn("pm", None, body)
    assert isinstance(r, ParsedTurn) and isinstance(r.intent, PMPlanIntent)


# --- Developer --------------------------------------------------------------


def test_dev_valid():
    body = _env("dev", {"kind": "tool_plan", "task_type": "implementation",
                "tool_calls": [{"tool": "code_write", "args": {"path": "a.py"}}]},
                task_id="t1")
    r = parse_coding_turn("dev", "t1", body)
    assert isinstance(r, ParsedTurn) and isinstance(r.intent, DeveloperToolPlanIntent)


def test_dev_implementation_requires_tool_calls():
    body = _env("dev", {"kind": "tool_plan", "task_type": "implementation",
                "tool_calls": []}, task_id="t1")
    assert parse_coding_turn("dev", "t1", body).code == TurnErrorCode.turn_schema_mismatch


def test_dev_documentation_allows_no_tools():
    body = _env("dev", {"kind": "tool_plan", "task_type": "documentation"}, task_id="t1")
    assert isinstance(parse_coding_turn("dev", "t1", body), ParsedTurn)


def test_dev_extra_summary_is_ignored():
    body = _env("dev", {"kind": "tool_plan", "task_type": "implementation",
                "tool_calls": [{"tool": "code_write", "args": {"path": "a.py"}}],
                "summary": "implemented a.py"}, task_id="t1")
    r = parse_coding_turn("dev", "t1", body)
    assert isinstance(r, ParsedTurn) and isinstance(r.intent, DeveloperToolPlanIntent)


def test_dev_bad_task_type_is_mismatch():
    body = _env("dev", {"kind": "tool_plan", "task_type": "wizardry"}, task_id="t1")
    assert parse_coding_turn("dev", "t1", body).code == TurnErrorCode.turn_schema_mismatch


def test_dev_cannot_self_report_passing_test():
    # has_passing_test is an actionable claim, not harmless prose. Even with
    # extra-field leniency for summary-like fields, this remains fail-closed.
    body = _env("dev", {"kind": "tool_plan", "task_type": "implementation",
                "tool_calls": [{"tool": "code_write", "args": {}}],
                "has_passing_test": True}, task_id="t1")
    assert parse_coding_turn("dev", "t1", body).code == TurnErrorCode.turn_schema_mismatch


# --- Reviewer ---------------------------------------------------------------


def test_reviewer_valid():
    body = _env("reviewer", {"kind": "review_verdict", "reviewed_head": "h",
                "approved": True}, task_id="t1")
    r = parse_coding_turn("reviewer", "t1", body)
    assert isinstance(r, ParsedTurn) and isinstance(r.intent, ReviewerVerdictIntent)


def test_reviewer_missing_approved_does_not_approve():
    body = _env("reviewer", {"kind": "review_verdict", "reviewed_head": "h",
                "findings": []}, task_id="t1")
    assert parse_coding_turn("reviewer", "t1", body).code == TurnErrorCode.turn_schema_mismatch


def test_reviewer_reject_requires_findings():
    body = _env("reviewer", {"kind": "review_verdict", "reviewed_head": "h",
                "approved": False, "findings": []}, task_id="t1")
    assert parse_coding_turn("reviewer", "t1", body).code == TurnErrorCode.turn_schema_mismatch


def test_reviewer_severity_synonyms_are_normalized_not_rejected():
    # Real models say critical/high/low/block/nit/… — normalize to the canonical
    # minor/major/blocking instead of throwing out the whole review verdict (which
    # left every PR stuck `changes_requested`). Unknown labels default to major.
    body = _env("reviewer", {"kind": "review_verdict", "reviewed_head": "h",
                "approved": False, "findings": [
                    {"severity": "critical", "path": "a", "title": "x"},
                    {"severity": "low", "path": "b", "title": "y"},
                    {"severity": "block", "path": "c", "title": "z"},
                    {"severity": "meh", "path": "d", "title": "w"},
                ]}, task_id="t1")
    r = parse_coding_turn("reviewer", "t1", body)
    assert isinstance(r, ParsedTurn)
    assert [f.severity for f in r.intent.findings] == ["blocking", "minor", "blocking", "major"]


def test_reviewer_string_findings_are_coerced():
    body = _env("reviewer", {"kind": "review_verdict", "reviewed_head": "h",
                "approved": False, "findings": ["missing edge-case test"]},
                task_id="t1")
    r = parse_coding_turn("reviewer", "t1", body)
    assert isinstance(r, ParsedTurn)
    assert r.intent.findings[0].summary == "missing edge-case test"
    assert r.intent.findings[0].title == "missing edge-case test"


def test_reviewer_finding_aliases_and_nested_location_are_normalized():
    body = _env("reviewer", {"kind": "review_verdict", "reviewed_head": "h",
                "approved": False, "findings": [{
                    "severity": "high",
                    "description": "The move validation is incomplete.",
                    "location": {"path": "game.py", "line": "248"},
                }]}, task_id="t1")
    r = parse_coding_turn("reviewer", "t1", body)
    assert isinstance(r, ParsedTurn)
    finding = r.intent.findings[0]
    assert finding.severity == "blocking"
    assert finding.summary == "The move validation is incomplete."
    assert finding.title == "The move validation is incomplete."
    assert finding.body == "The move validation is incomplete."
    assert finding.path == "game.py"
    assert finding.line == 248


def test_reviewer_finding_preserves_description_when_title_is_present():
    body = _env("reviewer", {"kind": "review_verdict", "reviewed_head": "h",
                "approved": False, "findings": [{
                    "title": "Incomplete validation",
                    "description": "The complete actionable explanation.",
                }]}, task_id="t1")
    r = parse_coding_turn("reviewer", "t1", body)
    assert isinstance(r, ParsedTurn)
    finding = r.intent.findings[0]
    assert finding.title == "Incomplete validation"
    assert finding.summary == "Incomplete validation"
    assert finding.body == "The complete actionable explanation."


def test_reviewer_malformed_flat_or_nested_line_fails_soft():
    for finding_shape in (
        {"description": "x", "line": "248-260"},
        {"description": "x", "line": True},
        {"description": "x", "location": {"path": "a.py", "line": "N/A"}},
    ):
        body = _env("reviewer", {"kind": "review_verdict", "reviewed_head": "h",
                    "approved": False, "findings": [finding_shape]}, task_id="t1")
        r = parse_coding_turn("reviewer", "t1", body)
        assert isinstance(r, ParsedTurn)
        assert r.intent.findings[0].line is None


def test_reviewer_verdict_tolerates_extra_fields():
    body = _env("reviewer", {"kind": "review_verdict", "reviewed_head": "h",
                "approved": True, "summary": "lgtm", "comments": ["nice"]},
                task_id="t1")
    assert isinstance(parse_coding_turn("reviewer", "t1", body), ParsedTurn)


# --- Tester -----------------------------------------------------------------


def test_tester_valid():
    body = _env("tester", {"kind": "test_plan", "command_ids": ["unit"],
                "scope": "changed_files"}, task_id="t1")
    r = parse_coding_turn("tester", "t1", body)
    assert isinstance(r, ParsedTurn) and isinstance(r.intent, TesterPlanIntent)


def test_tester_empty_commands_is_mismatch():
    body = _env("tester", {"kind": "test_plan", "command_ids": [],
                "scope": "changed_files"}, task_id="t1")
    assert parse_coding_turn("tester", "t1", body).code == TurnErrorCode.turn_schema_mismatch


def test_tester_bad_scope_is_mismatch():
    body = _env("tester", {"kind": "test_plan", "command_ids": ["unit"],
                "scope": "everything"}, task_id="t1")
    assert parse_coding_turn("tester", "t1", body).code == TurnErrorCode.turn_schema_mismatch


def test_tester_cannot_assert_pass():
    # a 'passed' verdict is not part of the tester schema -> mismatch, never a pass.
    body = _env("tester", {"kind": "test_plan", "command_ids": ["unit"],
                "scope": "changed_files", "passed": True}, task_id="t1")
    assert parse_coding_turn("tester", "t1", body).code == TurnErrorCode.turn_schema_mismatch


# --- golden fixtures lock coding_turn.v1 ------------------------------------


def test_golden_fixtures():
    roles = {"pm": (PMPlanIntent, None), "dev": (DeveloperToolPlanIntent, "t-1"),
             "reviewer": (ReviewerVerdictIntent, "t-2"),
             "tester": (TesterPlanIntent, "t-3")}
    for role, (intent_cls, task_id) in roles.items():
        valid = (FIX / f"{role}.valid.json").read_text()
        r = parse_coding_turn(role, task_id, valid)
        assert isinstance(r, ParsedTurn), f"{role}.valid did not parse: {r}"
        assert isinstance(r.intent, intent_cls)
        invalid = (FIX / f"{role}.invalid.json").read_text()
        bad = parse_coding_turn(role, task_id, invalid)
        assert isinstance(bad, TurnParseError), f"{role}.invalid should fail"
