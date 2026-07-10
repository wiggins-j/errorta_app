"""F100 - strict schemas for Coding governance turns."""
from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, List, Literal, Optional, Union

from pydantic import BaseModel, ValidationError, model_validator

SCHEMA_VERSION = "governance_turn.v1"

GovernanceRole = Literal["pm", "reviewer"]


class GovernanceTurnErrorCode(str, Enum):
    turn_non_json = "turn_non_json"
    turn_schema_mismatch = "turn_schema_mismatch"
    role_mismatch = "role_mismatch"


@dataclass(frozen=True)
class GovernanceTurnParseError:
    code: GovernanceTurnErrorCode
    detail: str


class GovernanceTurnEnvelope(BaseModel):
    model_config = {"extra": "ignore"}
    schema_version: Literal["governance_turn.v1"]
    role: GovernanceRole
    intent: dict[str, Any]
    confidence: Literal["low", "medium", "high"] = "medium"
    notes: str = ""


class GovernanceSourceMixin(BaseModel):
    source_refs: List[str] = []


class PMBrainstormDraftIntent(GovernanceSourceMixin):
    model_config = {"extra": "forbid"}
    kind: Literal["brainstorm_draft"]
    title: str = "Project brainstorm"
    problem: str
    audience: str = ""
    constraints: List[str] = []
    non_goals: List[str] = []
    open_questions: List[str] = []
    recommended_direction: str
    body_markdown: str = ""

    @model_validator(mode="after")
    def _required_text(self) -> "PMBrainstormDraftIntent":
        if not self.problem.strip() or not self.recommended_direction.strip():
            raise ValueError("brainstorm requires problem and recommended_direction")
        return self

    def artifact_body(self) -> dict[str, Any]:
        return {
            "problem": self.problem,
            "audience": self.audience,
            "constraints": list(self.constraints),
            "non_goals": list(self.non_goals),
            "open_questions": list(self.open_questions),
            "recommended_direction": self.recommended_direction,
        }

    def markdown(self) -> str:
        if self.body_markdown.strip():
            return self.body_markdown
        parts = [
            f"# {self.title}",
            "",
            "## Problem",
            self.problem,
            "",
            "## Recommended direction",
            self.recommended_direction,
        ]
        if self.open_questions:
            parts += ["", "## Open questions", *[f"- {q}" for q in self.open_questions]]
        return "\n".join(parts).strip()


class PMSpecDraftIntent(GovernanceSourceMixin):
    model_config = {"extra": "forbid"}
    kind: Literal["spec_draft", "spec_revision"]
    title: str
    body_markdown: str
    acceptance_criteria: List[str]
    supersedes_artifact_id: Optional[str] = None

    @model_validator(mode="after")
    def _required_spec(self) -> "PMSpecDraftIntent":
        if not self.title.strip() or not self.body_markdown.strip():
            raise ValueError("spec requires title and body_markdown")
        if not [a for a in self.acceptance_criteria if a.strip()]:
            raise ValueError("spec requires acceptance_criteria")
        return self

    def artifact_body(self) -> dict[str, Any]:
        return {"acceptance_criteria": list(self.acceptance_criteria)}

    def markdown(self) -> str:
        """Render a readable spec body. The schema already requires a non-blank
        ``body_markdown`` for a clean parse, but this fallback guarantees a human
        can still read the spec (title + acceptance criteria) even if the body is
        somehow blank — so the viewer never shows an empty box."""
        if self.body_markdown.strip():
            return self.body_markdown
        parts = [f"# {self.title}", ""]
        crit = [a for a in self.acceptance_criteria if a.strip()]
        if crit:
            parts += ["## Acceptance criteria", *[f"- {a}" for a in crit]]
        return "\n".join(parts).strip()


class GovernancePlanSliceIntent(BaseModel):
    model_config = {"extra": "forbid"}
    slice_id: str
    title: str
    detail: str = ""
    depends_on: List[str] = []
    files: List[str] = []
    done_when: List[str]
    tests: List[str]
    review_focus: List[str]

    @model_validator(mode="after")
    def _required_slice(self) -> "GovernancePlanSliceIntent":
        if not self.slice_id.strip() or not self.title.strip():
            raise ValueError("plan slice requires slice_id and title")
        if not self.done_when:
            raise ValueError("plan slice requires done_when")
        if not self.tests:
            raise ValueError("plan slice requires tests")
        if not self.review_focus:
            raise ValueError("plan slice requires review_focus")
        return self


class PMPlanDraftIntent(GovernanceSourceMixin):
    model_config = {"extra": "forbid"}
    kind: Literal["plan_draft", "plan_revision", "plan_amendment"]
    title: str = "Implementation plan"
    body_markdown: str = ""
    slices: List[GovernancePlanSliceIntent]
    supersedes_artifact_id: Optional[str] = None

    @model_validator(mode="after")
    def _required_plan(self) -> "PMPlanDraftIntent":
        if not self.slices:
            raise ValueError("plan requires slices")
        ids = [s.slice_id for s in self.slices]
        if len(ids) != len(set(ids)):
            raise ValueError("plan slice ids must be unique")
        return self

    def artifact_body(self) -> dict[str, Any]:
        return {"slices": [s.model_dump() for s in self.slices]}

    def markdown(self) -> str:
        if self.body_markdown.strip():
            return self.body_markdown
        lines = [f"# {self.title}", ""]
        for s in self.slices:
            lines += [
                f"## {s.slice_id} - {s.title}",
                s.detail,
                "",
                "Done when:",
                *[f"- {v}" for v in s.done_when],
                "",
                "Tests:",
                *[f"- {v}" for v in s.tests],
                "",
            ]
        return "\n".join(lines).strip()


class PMSliceAcceptanceIntent(BaseModel):
    model_config = {"extra": "forbid"}
    kind: Literal["slice_acceptance", "completion_proposal"]
    source_slice_id: str = ""
    accepted: bool = False
    rationale: str

    @model_validator(mode="after")
    def _rationale(self) -> "PMSliceAcceptanceIntent":
        if not self.rationale.strip():
            raise ValueError("slice acceptance requires rationale")
        return self


class GovernanceFindingIntent(BaseModel):
    # F100 bugfix (2026-06-22): tolerate unknown finding keys (e.g. `category`,
    # `location`) emitted by real reviewer models. SAFE — this model is referenced
    # only by ``ReviewerArtifactReviewIntent.findings`` (no other intent shares
    # it), and the normalizer below already lifts the useful aliases into the
    # canonical fields before validation. Canonical fields stay strict.
    model_config = {"extra": "ignore"}
    severity: Literal["low", "medium", "high", "critical"] = "medium"
    title: str
    body: str = ""
    blocking: bool = False

    @model_validator(mode="after")
    def _title(self) -> "GovernanceFindingIntent":
        if not self.title.strip():
            raise ValueError("finding title is required")
        return self


class ReviewerArtifactReviewIntent(BaseModel):
    model_config = {"extra": "forbid"}
    kind: Literal["artifact_review", "approval_readiness_check"]
    artifact_id: str
    verdict: Literal["approved", "request_changes", "blocked"]
    findings: List[GovernanceFindingIntent] = []

    @model_validator(mode="after")
    def _findings_when_not_approved(self) -> "ReviewerArtifactReviewIntent":
        if self.verdict != "approved" and not self.findings:
            raise ValueError("non-approved artifact review requires findings")
        return self


PMIntent = Union[
    PMBrainstormDraftIntent,
    PMSpecDraftIntent,
    PMPlanDraftIntent,
    PMSliceAcceptanceIntent,
]
ReviewerIntent = ReviewerArtifactReviewIntent


@dataclass(frozen=True)
class ParsedGovernanceTurn:
    envelope: GovernanceTurnEnvelope
    intent: BaseModel


def _balanced_objects(text: str) -> List[str]:
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


def _load_json(text: str) -> Optional[dict[str, Any]]:
    s = (text or "").strip()
    direct = s
    if direct.startswith("```"):
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
    fallback: Optional[dict[str, Any]] = None
    for cand in reversed(_balanced_objects(text or "")):
        try:
            obj = json.loads(cand)
        except (ValueError, TypeError):
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("schema_version") == SCHEMA_VERSION or ("role" in obj and "intent" in obj):
            return obj
        if fallback is None:
            fallback = obj
    return fallback


# F100 bugfix (2026-06-22): lenient/normalizing parse for artifact reviews.
# Real reviewer models (and the strict PM dual-review) emit reasonable-but-
# imperfect intents — natural verdict synonyms ("needs_work") and richer finding
# shapes ({severity, category, location, description}) — that the strict schema
# rejected, dead-ending the run with `no_progress`. The normalizer below adapts
# those inputs BEFORE pydantic validation so the canonical contract stays strict
# (verdict Literal, required non-blank title, findings-when-not-approved rule)
# while real-world variants are accepted. NOTE: this is a fresh synonym map for
# the approved/request_changes/blocked space — `errorta_judge.schema_guard.
# normalize_verdict` works in a pass/partial/fail space and is not reusable here.
_VERDICT_SYNONYMS = {
    "needs_work": "request_changes",
    "changes_requested": "request_changes",
    "request changes": "request_changes",
    "request_changes": "request_changes",
    "changes": "request_changes",
    "revise": "request_changes",
    "reject": "request_changes",
    "rejected": "request_changes",
    "approve": "approved",
    "approved": "approved",
    "lgtm": "approved",
    "ok": "approved",
    "pass": "approved",
    "block": "blocked",
    "blocker": "blocked",
    "blocked": "blocked",
}

_FINDING_TITLE_CAP = 80
_SEVERITY_SYNONYMS = {
    "critical": "critical",
    "blocker": "critical",
    "high": "high",
    "major": "high",
    "medium": "medium",
    "moderate": "medium",
    "warning": "medium",
    "low": "low",
    "minor": "low",
    "info": "low",
    "informational": "low",
}


def _first_clause(text: str) -> str:
    s = " ".join((text or "").split())
    if not s:
        return ""
    for sep in (". ", "! ", "? ", "\n"):
        idx = s.find(sep)
        if idx > 0:
            s = s[:idx]
            break
    return s[:_FINDING_TITLE_CAP].strip()


def _coerce_finding(raw: Any) -> Any:
    if not isinstance(raw, dict):
        return raw
    finding = dict(raw)
    severity = finding.get("severity")
    if isinstance(severity, str):
        canonical = _SEVERITY_SYNONYMS.get(severity.strip().lower())
        if canonical is not None:
            finding["severity"] = canonical
        else:
            finding.pop("severity", None)
    # description/detail -> body (only if body not already populated).
    if not str(finding.get("body", "") or "").strip():
        for alias in ("description", "detail"):
            alt = finding.get(alias)
            if isinstance(alt, str) and alt.strip():
                finding["body"] = alt
                break
    # Synthesize a title when missing/blank: prefer category (humanized), else
    # the first clause of body/description. Never leave it empty.
    if not str(finding.get("title", "") or "").strip():
        category = finding.get("category")
        if isinstance(category, str) and category.strip():
            finding["title"] = (
                category.replace("_", " ").replace("-", " ").strip().title()
            )
        else:
            clause = _first_clause(
                str(finding.get("body", "") or finding.get("description", "") or "")
            )
            finding["title"] = clause or "Finding"
    return finding


def _normalize_review_intent(intent: Any) -> dict[str, Any]:
    """Return a cleaned copy of a reviewer/PM artifact-review intent dict with
    verdict synonyms canonicalized and findings coerced. Unknown verdicts are
    left as-is so genuine garbage still fails validation."""
    if not isinstance(intent, dict):
        return intent  # type: ignore[return-value]
    cleaned = dict(intent)
    verdict = cleaned.get("verdict")
    if isinstance(verdict, str):
        canonical = _VERDICT_SYNONYMS.get(verdict.strip().lower())
        if canonical is not None:
            cleaned["verdict"] = canonical
    findings = cleaned.get("findings")
    if isinstance(findings, list):
        cleaned["findings"] = [_coerce_finding(f) for f in findings]
    return cleaned


def _pm_intent(raw: dict[str, Any]) -> type[BaseModel]:
    kind = raw.get("kind")
    if kind == "brainstorm_draft":
        return PMBrainstormDraftIntent
    if kind in {"spec_draft", "spec_revision"}:
        return PMSpecDraftIntent
    if kind in {"plan_draft", "plan_revision", "plan_amendment"}:
        return PMPlanDraftIntent
    if kind in {"slice_acceptance", "completion_proposal"}:
        return PMSliceAcceptanceIntent
    if kind in {"artifact_review", "approval_readiness_check"}:
        # F100 strict mode: the PM also reviews each artifact (dual review).
        return ReviewerArtifactReviewIntent
    return PMBrainstormDraftIntent


def parse_governance_turn(
    role: str,
    text: str,
) -> ParsedGovernanceTurn | GovernanceTurnParseError:
    raw = _load_json(text)
    if raw is None:
        return GovernanceTurnParseError(
            GovernanceTurnErrorCode.turn_non_json, "no parseable JSON object"
        )
    try:
        envelope = GovernanceTurnEnvelope.model_validate(raw)
    except ValidationError as exc:
        return GovernanceTurnParseError(
            GovernanceTurnErrorCode.turn_schema_mismatch,
            f"invalid envelope: {exc.errors()[:3]}",
        )
    if envelope.role != role:
        return GovernanceTurnParseError(
            GovernanceTurnErrorCode.role_mismatch,
            f"envelope role {envelope.role!r} != scheduled {role!r}",
        )
    try:
        intent_cls: type[BaseModel]
        if role == "pm":
            intent_cls = _pm_intent(envelope.intent)
        elif role == "reviewer":
            intent_cls = ReviewerArtifactReviewIntent
        else:
            return GovernanceTurnParseError(
                GovernanceTurnErrorCode.role_mismatch,
                f"unsupported governance role {role!r}",
            )
        intent_payload: Any = envelope.intent
        # F100 bugfix: normalize artifact-review intents (verdict synonyms +
        # finding aliases) before validation. This covers BOTH role="reviewer"
        # AND the PM dual-review, since _pm_intent maps artifact_review /
        # approval_readiness_check -> ReviewerArtifactReviewIntent.
        if intent_cls is ReviewerArtifactReviewIntent:
            intent_payload = _normalize_review_intent(envelope.intent)
        intent = intent_cls.model_validate(intent_payload)
    except ValidationError as exc:
        return GovernanceTurnParseError(
            GovernanceTurnErrorCode.turn_schema_mismatch,
            f"invalid {role} governance intent: {exc.errors()[:3]}",
        )
    return ParsedGovernanceTurn(envelope=envelope, intent=intent)


__all__ = [
    "SCHEMA_VERSION",
    "GovernanceTurnEnvelope",
    "GovernanceTurnErrorCode",
    "GovernanceTurnParseError",
    "ParsedGovernanceTurn",
    "PMBrainstormDraftIntent",
    "PMSpecDraftIntent",
    "PMPlanDraftIntent",
    "PMSliceAcceptanceIntent",
    "ReviewerArtifactReviewIntent",
    "parse_governance_turn",
]
