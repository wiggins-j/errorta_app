"""F087-09 — strict, versioned turn schemas for Coding Mode.

A model can be creative in its reasoning, but its ACTION surface must be typed,
explicit, and fail-closed. Every member response validates against a role schema
(``coding_turn.v1``) before it can drive tools or mutate the ledger. Missing
required fields never default to success; malformed output yields a structured
diagnostic, not a silent stall.

This module is self-contained (Pydantic models + ``parse_coding_turn``). Wiring
it into the runner + persisting validated intents is the integration JOIN with
F087-08 (the tool-backed-execution slice) and is intentionally not done here.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, List, Literal, Optional, Union

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

SCHEMA_VERSION = "coding_turn.v1"

CodingRole = Literal["pm", "dev", "reviewer", "tester"]
Confidence = Literal["low", "medium", "high"]


class TurnErrorCode(str, Enum):
    turn_non_json = "turn_non_json"
    # F127: the response is agent tool-call markup (`<function_calls>`/`<invoke>`)
    # with no usable JSON — the model tried to ACT like an agent instead of
    # returning the turn envelope. Distinct from generic non-JSON so the escalation
    # ladder can recognize a capability/format mismatch.
    turn_tool_markup_only = "turn_tool_markup_only"
    turn_schema_mismatch = "turn_schema_mismatch"
    role_mismatch = "role_mismatch"
    task_mismatch = "task_mismatch"
    # Raised downstream (F087-08 allowlist / F087-10 test config), defined here
    # so the taxonomy is single-sourced.
    tool_not_allowed = "tool_not_allowed"
    stale_review_head = "stale_review_head"
    invalid_test_command = "invalid_test_command"
    # F140: a dev code_write that would DESTROY an existing file — a placeholder/
    # "keep existing file" sentinel written literally, or a substantial file
    # collapsed to an empty file / tiny stub. Blocked before it lands; the turn is
    # unproductive so the F136/F127 escalate-up ladder engages instead of the
    # deletion reaching a PR.
    destructive_write_blocked = "destructive_write_blocked"


@dataclass(frozen=True)
class TurnParseError:
    code: TurnErrorCode
    detail: str


# --- envelope ---------------------------------------------------------------


class CodingTurnEnvelope(BaseModel):
    model_config = {"extra": "ignore"}
    schema_version: Literal["coding_turn.v1"]
    role: CodingRole
    task_id: Optional[str] = None
    intent: dict[str, Any]
    confidence: Confidence = "medium"
    notes: str = ""


# --- role intents -----------------------------------------------------------


class PMTask(BaseModel):
    model_config = {"extra": "forbid"}
    title: str
    role: Literal["dev", "reviewer", "tester"]  # PM cannot create 'pm' tasks
    detail: str = ""
    depends_on: List[str] = []
    task_type: Literal[
        "implementation", "bugfix", "refactor", "test", "documentation",
        "investigation", "review", "design",
    ] = "implementation"
    difficulty_tier: Literal["light", "mid", "strong"] = "mid"
    preferred_member_id: str = ""
    preferred_route_id: str = ""
    assignment_rationale: str = ""

    @model_validator(mode="after")
    def _title_nonempty(self) -> "PMTask":
        if not self.title.strip():
            raise ValueError("task title is required")
        return self


class PMDecision(BaseModel):
    model_config = {"extra": "ignore"}
    title: str
    choice: str
    rationale: str = ""


class PMPlanIntent(BaseModel):
    model_config = {"extra": "ignore"}
    kind: Literal["plan"]
    done: bool
    tasks: List[PMTask] = []
    decisions: List[PMDecision] = []
    completion_summary: str = ""

    @model_validator(mode="after")
    def _done_rules(self) -> "PMPlanIntent":
        if self.done and not self.completion_summary.strip():
            raise ValueError("done=true requires a completion_summary")
        if not self.done and not self.tasks:
            raise ValueError("done=false requires at least one task")
        return self


class ToolCall(BaseModel):
    model_config = {"extra": "ignore"}
    tool: str
    args: dict[str, Any] = {}

    @model_validator(mode="after")
    def _tool_nonempty(self) -> "ToolCall":
        if not self.tool.strip():
            raise ValueError("tool name is required")
        return self


_DEV_NEEDS_TOOLS = ("implementation", "test_only", "refactor")


class DeveloperToolPlanIntent(BaseModel):
    model_config = {"extra": "ignore"}
    kind: Literal["tool_plan"]
    task_type: Literal[
        "implementation", "test_only", "refactor", "documentation", "investigation"
    ]
    tool_calls: List[ToolCall] = []
    expected_test_command_ids: List[str] = []

    @model_validator(mode="before")
    @classmethod
    def _reject_actionable_extra_claims(cls, data: Any) -> Any:
        if isinstance(data, dict) and "has_passing_test" in data:
            raise ValueError("has_passing_test is not a valid dev intent field")
        return data

    @model_validator(mode="after")
    def _tools_required(self) -> "DeveloperToolPlanIntent":
        if self.task_type in _DEV_NEEDS_TOOLS and not self.tool_calls:
            raise ValueError(f"{self.task_type} requires at least one tool_call")
        return self


class ContextRequestScope(BaseModel):
    model_config = {"extra": "ignore"}
    paths: List[str] = Field(default_factory=list)
    symbols: List[str] = Field(default_factory=list)
    corpus_query: str = ""
    sources: List[Literal["memory", "corpus"]] = Field(
        default_factory=lambda: ["memory", "corpus"])


class DeveloperContextRequestIntent(BaseModel):
    # F088-09: a typed, READ-ONLY mid-run request for grounding context. It is a
    # dev intent kind (not a tool) so it stays fail-closed in parse_coding_turn
    # and can never write files / mutate durable truth.
    model_config = {"extra": "ignore"}
    kind: Literal["context_request"]
    reason: Literal[
        "missing_api_contract", "ambiguous_requirement", "missing_test_expectation",
        "wip_overlap", "corpus_lookup", "other",
    ] = "other"
    question: str
    scope: ContextRequestScope = Field(default_factory=ContextRequestScope)
    needed_for: Literal[
        "implementation", "test_update", "refactor", "investigation"
    ] = "implementation"
    max_items: int = 6

    @model_validator(mode="after")
    def _question_required(self) -> "DeveloperContextRequestIntent":
        if not self.question.strip():
            raise ValueError("context_request requires a question")
        return self


# Real models say "critical"/"high"/"low"/"block"/"nit"/… for severity; the
# canonical set is minor/major/blocking. Map synonyms instead of rejecting the
# WHOLE review verdict (which previously left every PR stuck `changes_requested`
# because the reviewer turn failed schema validation and never approved).
_SEVERITY_ALIASES = {
    "blocking": "blocking", "block": "blocking", "blocker": "blocking",
    "critical": "blocking", "crit": "blocking", "high": "blocking",
    "fatal": "blocking", "error": "blocking", "severe": "blocking", "p0": "blocking",
    "major": "major", "medium": "major", "moderate": "major", "med": "major",
    "warning": "major", "warn": "major", "p1": "major", "normal": "major",
    "minor": "minor", "low": "minor", "info": "minor", "trivial": "minor",
    "nit": "minor", "nitpick": "minor", "suggestion": "minor", "style": "minor",
    "note": "minor", "cosmetic": "minor", "p2": "minor", "p3": "minor",
}


class Finding(BaseModel):
    model_config = {"extra": "ignore"}
    severity: Literal["minor", "major", "blocking"] = "major"
    summary: str = ""
    path: str = ""
    line: Optional[int] = None
    title: str = ""
    body: str = ""

    @model_validator(mode="before")
    @classmethod
    def _normalize_shape(cls, data: Any) -> Any:
        # Real reviewer models emit shapes the strict field set doesn't declare —
        # a nested {"location": {"path", "line"}} instead of flat path/line, and
        # the finding text under "description"/"message"/"detail"/"text"/"comment"
        # instead of title/body. With extra="ignore" those keys were silently
        # dropped, leaving BLANK findings: the reviewer's real reason vanished, the
        # rework task had no actionable detail, and the board showed empty
        # "changes requested" cards. Fold the common aliases into the canonical
        # fields so a finding is never emptied by shape alone.
        if not isinstance(data, dict):
            return data
        d = dict(data)
        loc = d.get("location")
        path = d.get("path")
        if not (isinstance(path, str) and path.strip()):
            path = None
        if isinstance(loc, dict):
            path = path or loc.get("path") or loc.get("file")
        elif isinstance(loc, str):
            path = path or loc
        path = path or d.get("file")
        if isinstance(path, str):
            d["path"] = path.strip()
        elif "path" in d:
            d["path"] = ""

        # Normalize both flat and nested line values. A range ("248-260"),
        # "N/A", bool, or other non-int value must not hard-fail the whole
        # verdict and recreate the parse wedge this tolerance exists to remove.
        line = d.get("line")
        if line is None and isinstance(loc, dict):
            line = loc.get("line")
        if isinstance(line, bool):
            d["line"] = None
        elif isinstance(line, int):
            d["line"] = line
        elif isinstance(line, str) and line.strip().isdigit():
            d["line"] = int(line.strip())
        elif line is not None:
            d["line"] = None

        def _text(key: str) -> str:
            value = d.get(key)
            return value.strip() if isinstance(value, str) and value.strip() else ""

        alias = ""
        for key in ("description", "message", "detail", "text", "comment"):
            alias = _text(key)
            if alias:
                break
        title = _text("title")
        body = _text("body")
        summary = _text("summary")
        if not summary and (title or body or alias):
            d["summary"] = title or body or alias
            summary = d["summary"]
        if not title and (summary or alias or body):
            d["title"] = (summary or alias or body).splitlines()[0][:120]
            title = d["title"]
        if not body and (alias or summary or title):
            # Prefer the detailed model-spoken alias over a short title. This
            # preserves actionable context for common {title, description}
            # findings instead of copying the title into body and dropping the
            # description as an ignored extra field.
            d["body"] = alias or summary or title
        return d

    @field_validator("severity", mode="before")
    @classmethod
    def _normalize_severity(cls, value: Any) -> str:
        # Coerce any model-spoken severity to the canonical set; unknown -> major
        # so a finding is never dropped on its label alone.
        return _SEVERITY_ALIASES.get(str(value or "").strip().lower(), "major")


class ReviewerVerdictIntent(BaseModel):
    # Tolerate extra keys a model may add (e.g. "summary", "comments") instead of
    # failing the whole verdict — the review's approve/findings decision is what
    # matters, not strict field exclusivity.
    model_config = {"extra": "ignore"}
    kind: Literal["review_verdict"]
    reviewed_head: str
    approved: bool
    findings: List[Finding] = []

    @field_validator("findings", mode="before")
    @classmethod
    def _coerce_string_findings(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return value
        coerced = []
        for item in value:
            if isinstance(item, str):
                text = item.strip()
                coerced.append({"summary": text, "title": text, "body": text})
            else:
                coerced.append(item)
        return coerced

    @model_validator(mode="after")
    def _verdict_rules(self) -> "ReviewerVerdictIntent":
        if not self.reviewed_head.strip():
            raise ValueError("reviewed_head is required")
        if not self.approved and not self.findings:
            raise ValueError("approved=false requires at least one finding")
        return self


class TesterPlanIntent(BaseModel):
    # The tester chooses commands; it CANNOT assert pass/fail (a 'passed' key is
    # simply not part of the schema — F087-10 derives the verdict from real runs).
    __test__ = False  # not a pytest test class (name starts with "Test")
    model_config = {"extra": "forbid"}
    kind: Literal["test_plan"]
    command_ids: List[str]
    scope: Literal["changed_files", "full_project", "targeted"]
    rationale: str = ""
    # F142 WS-C: the tester MAY declare that no registered command meaningfully
    # exercises THIS task's slice (the project is not yet runnable end-to-end),
    # in which case the test gate is non-blocking for this slice. This is a
    # wire/prompt-schema field only (defaults False, backward-compatible); it is
    # NOT persisted on a Task, so it introduces no DB schema/migration. It is
    # honored ONLY when command_ids is empty — a non-empty plan always runs and
    # real exit codes govern (a command that ran and failed can never be masked).
    not_applicable: bool = False

    @model_validator(mode="after")
    def _commands_present_unless_not_applicable(self) -> "TesterPlanIntent":
        if not self.command_ids and not self.not_applicable:
            raise ValueError(
                "command_ids must be non-empty unless not_applicable is true")
        return self


RoleIntent = Union[
    PMPlanIntent, DeveloperToolPlanIntent, ReviewerVerdictIntent, TesterPlanIntent
]

_INTENT_BY_ROLE: dict[str, type[BaseModel]] = {
    "pm": PMPlanIntent,
    "dev": DeveloperToolPlanIntent,
    "reviewer": ReviewerVerdictIntent,
    "tester": TesterPlanIntent,
}


@dataclass(frozen=True)
class ParsedTurn:
    envelope: CodingTurnEnvelope
    intent: BaseModel
    repairs: tuple[str, ...] = ()


# --- parsing ----------------------------------------------------------------


def _balanced_objects(text: str) -> List[str]:
    """Return every balanced top-level ``{...}`` substring, string-aware (braces
    inside JSON string literals don't count). Used to pull an action envelope out
    of a model response that wraps it in prose. NOT a greedy regex — it tracks
    brace depth and string state so nested objects are captured whole."""
    objs: List[str] = []
    depth = 0
    start = -1
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    objs.append(text[start:i + 1])
    return objs


# F127: agent tool-call scaffolding a CLI-backed model may emit instead of (or
# wrapping) the JSON turn. We strip the KNOWN tags only (never arbitrary `<...>`,
# which could live inside a JSON string) so any embedded envelope survives the
# balanced-brace scan, and so we can tell a tool-markup-only turn from plain prose.
_AGENT_TAG_RE = re.compile(
    r"</?\s*(?:antml:)?"
    r"(?:function_calls|invoke|parameter|function_results|result|thinking|tool_use|tool_call)"
    r"\b[^>]*>",
    re.IGNORECASE,
)


def _has_agent_markup(text: str) -> bool:
    return bool(_AGENT_TAG_RE.search(text or ""))


def _strip_agent_markup(text: str) -> str:
    """Remove known agent tags outside JSON strings.

    A plain regex substitution can corrupt a valid embedded envelope when a
    string value documents the literal ``<function_calls>`` syntax. Track JSON
    object/string state while scanning so only the surrounding agent scaffold is
    removed; tag-like text inside an object string remains byte-for-byte intact.
    """
    source = text or ""
    out: list[str] = []
    depth = 0
    in_string = False
    escaped = False
    index = 0
    while index < len(source):
        char = source[index]
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if depth > 0 and char == '"':
            in_string = True
            out.append(char)
            index += 1
            continue
        if char == "{":
            depth += 1
        elif char == "}" and depth > 0:
            depth -= 1
        if char == "<":
            match = _AGENT_TAG_RE.match(source, index)
            if match is not None:
                out.append(" ")
                index = match.end()
                continue
        out.append(char)
        index += 1
    return "".join(out)


def _load_json(
    text: str, repairs: Optional[list[str]] = None
) -> Optional[dict[str, Any]]:
    """Parse a member turn's action JSON. Fast path: the whole message is a JSON
    object (optionally wrapped in a single ```json fence). Robust path: CLI-backed
    members (claude_cli / codex_cli / cursor_cli) routinely REASON in prose and then emit the
    envelope as a fenced ```json block at the END — so the action JSON is not the
    whole message. We then extract the embedded envelope (string-aware brace
    scan), preferring a candidate that looks like a coding_turn (schema_version,
    or role+intent). Without this the PM's perfectly-valid done=true signal —
    emitted after a prose preamble — was rejected as turn_non_json and the run
    stalled at no_progress instead of completing."""
    s = (text or "").strip()
    direct = s
    if direct.startswith("```"):
        # drop the opening fence line (``` or ```json) and a trailing fence
        direct = direct[3:]
        if "\n" in direct:
            first, rest = direct.split("\n", 1)
            if first.strip().lower() in ("", "json"):
                direct = rest
        direct = direct.strip()
        if direct.endswith("```"):
            direct = direct[:-3].strip()
    try:
        obj = json.loads(direct)
        if isinstance(obj, dict):
            return obj
    except (ValueError, TypeError):
        pass
    # Robust extraction from prose. Scan from the end (the answer usually comes
    # last) and prefer a real envelope shape over an incidental prose object.
    # F127: strip agent tool-call tags first so an envelope wrapped in
    # `<function_calls>`/`<parameter>` markup still surfaces.
    fallback: Optional[dict[str, Any]] = None
    stripped = _strip_agent_markup(text or "")
    markup_removed = stripped != (text or "")
    for cand in reversed(_balanced_objects(stripped)):
        try:
            obj = json.loads(cand)
        except (ValueError, TypeError):
            continue
        if not isinstance(obj, dict):
            continue
        if "schema_version" in obj or ("role" in obj and "intent" in obj):
            if markup_removed and repairs is not None:
                repairs.append("agent_markup_removed")
            return obj
        if fallback is None:
            fallback = obj
    if fallback is not None and markup_removed and repairs is not None:
        repairs.append("agent_markup_removed")
    return fallback


def parse_coding_turn(
    role: str, task_id: Optional[str], text: str
) -> Union[ParsedTurn, TurnParseError]:
    """Parse + validate a member turn. Returns a ParsedTurn or a structured
    TurnParseError (fail-closed: never a partial / defaulted-success result)."""
    repairs: list[str] = []
    raw = _load_json(text, repairs)
    if raw is None:
        # F127: distinguish "tried to act like an agent" (tool-call markup, no
        # JSON) from plain non-JSON so the escalation ladder can react.
        if _has_agent_markup(text):
            return TurnParseError(
                TurnErrorCode.turn_tool_markup_only,
                "response was agent tool-call markup with no JSON envelope")
        return TurnParseError(TurnErrorCode.turn_non_json,
                              "no parseable JSON object")
    # F127: stamp a missing schema_version when role+intent are unambiguous — a
    # pure label fix (no fabrication); the intent is still validated strictly.
    if (isinstance(raw, dict) and "schema_version" not in raw
            and "role" in raw and "intent" in raw):
        raw["schema_version"] = SCHEMA_VERSION
        repairs.append("schema_version_stamped")
    try:
        envelope = CodingTurnEnvelope.model_validate(raw)
    except ValidationError as exc:
        return TurnParseError(TurnErrorCode.turn_schema_mismatch,
                              f"invalid envelope: {exc.errors()[:3]}")

    if envelope.role != role:
        return TurnParseError(
            TurnErrorCode.role_mismatch,
            f"envelope role {envelope.role!r} != scheduled {role!r}")

    # PM plan turns are not bound to a single task_id; every other role must
    # answer for exactly the assigned task.
    if role != "pm" and envelope.task_id != task_id:
        return TurnParseError(
            TurnErrorCode.task_mismatch,
            f"envelope task_id {envelope.task_id!r} != assigned {task_id!r}")

    intent_cls = _INTENT_BY_ROLE.get(role)
    if intent_cls is None:
        return TurnParseError(TurnErrorCode.turn_schema_mismatch,
                              f"unknown role {role!r}")
    # F088-09: the dev intent is a kind-discriminated union — a read-only
    # context_request routes to its own model (else the default tool_plan).
    if role == "dev" and isinstance(envelope.intent, dict):
        kind = envelope.intent.get("kind")
        if kind == "context_request":
            intent_cls = DeveloperContextRequestIntent
        elif kind not in ("tool_plan", "context_request") and \
                str(envelope.intent.get("question", "")).strip():
            # F127: a dev that asked a question under an unknown/extra kind label
            # (e.g. "explore"/"investigate") is a context_request. Relabel only —
            # no fabrication; reason/question are still validated strictly, so a
            # malformed body still fails.
            envelope.intent["kind"] = "context_request"
            intent_cls = DeveloperContextRequestIntent
            repairs.append("dev_kind_relabelled_to_context_request")
    try:
        intent = intent_cls.model_validate(envelope.intent)
    except ValidationError as exc:
        return TurnParseError(TurnErrorCode.turn_schema_mismatch,
                              f"invalid {role} intent: {exc.errors()[:3]}")

    return ParsedTurn(envelope=envelope, intent=intent, repairs=tuple(repairs))


__all__ = [
    "SCHEMA_VERSION", "CodingTurnEnvelope", "PMPlanIntent",
    "DeveloperToolPlanIntent", "DeveloperContextRequestIntent",
    "ContextRequestScope", "ReviewerVerdictIntent", "TesterPlanIntent",
    "PMTask", "ToolCall", "Finding", "ParsedTurn", "TurnParseError",
    "TurnErrorCode", "parse_coding_turn",
]
