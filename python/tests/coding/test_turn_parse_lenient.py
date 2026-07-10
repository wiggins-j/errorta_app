"""F127 Workstream A — lenient turn parsing/repair.

A weaker worker model occasionally wraps the JSON turn in Claude-Code agent
tool-call markup, or omits it entirely. The parser must recover a wrapped
envelope and cleanly classify a markup-only / non-JSON turn (never fake one)."""
from __future__ import annotations

import json

from errorta_council.coding.schemas import (
    TurnErrorCode,
    parse_coding_turn,
)

# A minimal valid dev turn (tool_plan intent — read the kinds from the schema if
# this drifts).
_DEV_ENVELOPE = {
    "schema_version": "coding_turn.v1",
    "role": "dev",
    "task_id": "t-1",
    "intent": {"kind": "tool_plan", "task_type": "investigation", "tool_calls": []},
}


def _wrap_in_markup(obj: dict) -> str:
    body = json.dumps(obj)
    return (
        "I need to understand the task first. Let me act.\n"
        '<function_calls> <invoke name="emit">'
        f'<parameter name="turn">{body}</parameter>'
        "</invoke> </function_calls>"
    )


def test_recovers_envelope_wrapped_in_agent_markup() -> None:
    text = _wrap_in_markup(_DEV_ENVELOPE)
    parsed = parse_coding_turn("dev", "t-1", text)
    # It parses to the embedded envelope, not a TurnParseError.
    assert getattr(parsed, "envelope", None) is not None
    assert parsed.envelope.role == "dev"
    assert "agent_markup_removed" in parsed.repairs


def test_markup_only_no_json_is_tool_markup_only() -> None:
    text = (
        "I need to understand the context before proceeding.\n"
        '<function_calls> <invoke name="Task">'
        '<parameter name="subagent_type">Explore</parameter>'
        '<parameter name="query">find the bug</parameter>'
        "</invoke> </function_calls>"
    )
    parsed = parse_coding_turn("dev", "t-1", text)
    assert parsed.__class__.__name__ == "TurnParseError"
    assert parsed.code == TurnErrorCode.turn_tool_markup_only  # not faked


def test_plain_prose_no_json_is_non_json() -> None:
    parsed = parse_coding_turn("dev", "t-1", "I'm not sure how to do this task.")
    assert parsed.__class__.__name__ == "TurnParseError"
    assert parsed.code == TurnErrorCode.turn_non_json


def test_fenced_json_after_prose_still_parses() -> None:
    text = "Here is my plan.\n```json\n" + json.dumps(_DEV_ENVELOPE) + "\n```"
    parsed = parse_coding_turn("dev", "t-1", text)
    assert getattr(parsed, "envelope", None) is not None


def test_does_not_strip_angle_brackets_inside_json_strings() -> None:
    # A JSON string value containing `<…>` that is NOT an agent tag must survive
    # the markup pre-pass.
    env = json.loads(json.dumps(_DEV_ENVELOPE))
    env["notes"] = "render <div> safely"
    text = "```json\n" + json.dumps(env) + "\n```"
    parsed = parse_coding_turn("dev", "t-1", text)
    assert getattr(parsed, "envelope", None) is not None
    assert parsed.envelope.notes == "render <div> safely"


def test_does_not_strip_agent_tag_literal_inside_embedded_json_string() -> None:
    env = json.loads(json.dumps(_DEV_ENVELOPE))
    env["notes"] = "The literal <function_calls> tag is forbidden."
    parsed = parse_coding_turn("dev", "t-1", _wrap_in_markup(env))
    assert getattr(parsed, "envelope", None) is not None
    assert parsed.envelope.notes == env["notes"]


# --- Slice 2: safe schema coercions ---

def test_missing_schema_version_is_stamped() -> None:
    env = {"role": "dev", "task_id": "t-1",
           "intent": {"kind": "tool_plan", "task_type": "investigation", "tool_calls": []}}
    parsed = parse_coding_turn("dev", "t-1", json.dumps(env))
    assert getattr(parsed, "envelope", None) is not None
    assert parsed.envelope.schema_version == "coding_turn.v1"
    assert "schema_version_stamped" in parsed.repairs


def test_unknown_dev_kind_with_question_coerces_to_context_request() -> None:
    env = {
        "schema_version": "coding_turn.v1", "role": "dev", "task_id": "t-1",
        "intent": {"kind": "explore", "question": "What is the API contract for login?"},
    }
    parsed = parse_coding_turn("dev", "t-1", json.dumps(env))
    assert getattr(parsed, "envelope", None) is not None
    assert parsed.intent.kind == "context_request"
    assert parsed.intent.question.startswith("What is")
    assert "dev_kind_relabelled_to_context_request" in parsed.repairs


def test_unknown_dev_kind_without_question_is_rejected() -> None:
    # No question -> not a recoverable context_request shape -> reject, don't guess.
    env = {
        "schema_version": "coding_turn.v1", "role": "dev", "task_id": "t-1",
        "intent": {"kind": "explore", "target": "the codebase"},
    }
    parsed = parse_coding_turn("dev", "t-1", json.dumps(env))
    assert parsed.__class__.__name__ == "TurnParseError"
    assert parsed.code == TurnErrorCode.turn_schema_mismatch
