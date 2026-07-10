"""F031 Council schema (Phase 0).

All persisted objects carry ``format_version: 1`` (invariant 11).
Dataclasses are frozen value types; (de)serialization goes through
``to_dict``/``from_dict`` which tolerate unknown fields and reject
unsupported future format versions.

Errors on the wire use the sanitized ``CouncilEventError`` shape
(invariant 12): tests assert on ``code``, never on ``message`` text.
"""
from __future__ import annotations

import dataclasses
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

FORMAT_VERSION = 1


class UnsupportedFormatVersion(ValueError):
    """Raised when a stored object reports a format_version we do not know.

    Carries the observed version and the supported version so the message
    is actionable in logs and diagnostics.
    """

    def __init__(self, observed: int, supported: int = FORMAT_VERSION) -> None:
        super().__init__(
            f"unsupported format_version={observed}; this build supports {supported}"
        )
        self.observed = observed
        self.supported = supported


def _split_unknown(cls: type, raw: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Partition ``raw`` into (known-field kwargs, unknown extras).

    Supports invariant 11: readers tolerate unknown fields and preserve them
    in ``_extras`` so a write-back round-trip is lossless when a future
    writer adds an additive field within the same ``format_version``.

    The ``_extras`` sentinel field itself is never treated as a known input
    key; callers pass it explicitly.
    """
    known_names = {f.name for f in dataclasses.fields(cls) if f.name != "_extras"}
    known: dict[str, Any] = {}
    extras: dict[str, Any] = {}
    for k, v in raw.items():
        if k in known_names:
            known[k] = v
        elif k != "_extras":
            extras[k] = v
    return known, extras


def _emit_nested(obj: Any) -> dict[str, Any]:
    """asdict() a nested dataclass and inline its ``_extras`` map.

    asdict() includes the ``_extras`` field as a literal key in the output.
    Persisted form should look like the future writer's output: known keys
    plus the previously-unknown keys at the same level. Pop and merge.
    """
    d = asdict(obj)
    extras = d.pop("_extras", {}) or {}
    d.update(extras)
    return d


class EventType(str, Enum):
    RUN_STARTED = "run_started"
    RUN_STATUS_CHANGED = "run_status_changed"
    MEMBER_QUEUED = "member_queued"
    CONTEXT_BUILD_STARTED = "context_build_started"
    CONTEXT_BUILT = "context_built"
    BUDGET_CHECK_STARTED = "budget_check_started"
    BUDGET_BLOCKED = "budget_blocked"
    MEMBER_CALL_STARTED = "member_call_started"
    MEMBER_MESSAGE = "member_message"
    MEMBER_SKIPPED = "member_skipped"
    MEMBER_FAILED = "member_failed"
    MEMBER_CANCELLED = "member_cancelled"
    FINALIZATION_STARTED = "finalization_started"
    FINAL_ANSWER = "final_answer"
    VERDICT_RECORDED = "verdict_recorded"
    GROUNDING_RECORDED = "grounding_recorded"
    RUN_CANCEL_REQUESTED = "run_cancel_requested"
    RUN_CANCELLED = "run_cancelled"
    RUN_FAILED = "run_failed"
    RUN_COMPLETED = "run_completed"
    DIAGNOSTIC_NOTE = "diagnostic_note"
    # --- F129 PM-driven per-task model assignment ---
    PM_PLAN = "pm_plan"
    MODEL_ASSIGNED = "model_assigned"
    TASK_ESCALATED = "task_escalated"
    # --- F059 mobile companion commands ---
    MOBILE_MESSAGE = "mobile_message"
    # Reserved future types — declared so readers do not crash.
    MEMBER_DELTA = "member_delta"
    TOOL_CALL_REQUESTED = "tool_call_requested"
    TOOL_CALL_APPROVED = "tool_call_approved"
    TOOL_CALL_BLOCKED = "tool_call_blocked"
    TOOL_CALL_STARTED = "tool_call_started"
    TOOL_CALL_COMPLETED = "tool_call_completed"
    TOOL_CALL_FAILED = "tool_call_failed"
    # Legacy reserved placeholder kept readable for old event logs.
    TOOL_CALL_RESULT = "tool_call_result"
    MODERATOR_DECISION = "moderator_decision"
    VOTE_RECORDED = "vote_recorded"
    REPLAY_LINKED = "replay_linked"
    # Phase 1 additions (F031-1a):
    LOCAL_RESOURCE_CHECK_STARTED = "local_resource_check_started"
    LOCAL_RESOURCE_RELEASED = "local_resource_released"
    DIALECT_DOWNGRADED = "dialect_downgraded"
    # --- F037 expert callouts ---
    CALLOUT_REQUESTED = "callout_requested"
    CALLOUT_APPROVAL_REQUIRED = "callout_approval_required"
    CALLOUT_APPROVED = "callout_approved"
    CALLOUT_REJECTED = "callout_rejected"
    CALLOUT_STARTED = "callout_started"
    CALLOUT_COMPLETED = "callout_completed"
    CALLOUT_FAILED = "callout_failed"
    # --- F038 council steward ---
    STEWARD_PACKET_REQUESTED = "steward_packet_requested"
    STEWARD_PACKET_CREATED = "steward_packet_created"
    STEWARD_PACKET_FAILED = "steward_packet_failed"
    STEWARD_PACKET_USED = "steward_packet_used"
    STEWARD_PACKET_INVALIDATED = "steward_packet_invalidated"
    STEWARD_RECOMMENDATION = "steward_recommendation"
    # --- F041 policy engine / pending decisions ---
    POLICY_DECISION_CREATED = "policy_decision_created"
    POLICY_DECISION_APPROVED = "policy_decision_approved"
    POLICY_DECISION_REJECTED = "policy_decision_rejected"
    POLICY_DECISION_EXPIRED = "policy_decision_expired"
    # --- F042 child runs / async inbox ---
    CHILD_RUN_STARTED = "child_run_started"
    CHILD_RUN_INBOX_MESSAGE = "child_run_inbox_message"
    CHILD_RUN_COMPLETED = "child_run_completed"
    CHILD_RUN_FAILED = "child_run_failed"
    CHILD_RUN_CANCELLED = "child_run_cancelled"
    # --- F049 live user interjection ---
    USER_INTERJECTION = "user_interjection"
    # --- F078 Credibility mode ---
    CREDIBILITY_RESEARCH_STARTED = "credibility_research_started"
    CREDIBILITY_SOURCE_CAPTURED = "credibility_source_captured"
    CREDIBILITY_RESEARCH_COMPLETED = "credibility_research_completed"
    CREDIBILITY_CLAIM_PACKET_SUBMITTED = "credibility_claim_packet_submitted"
    CREDIBILITY_CLAIM_PACKET_REJECTED = "credibility_claim_packet_rejected"
    CREDIBILITY_CREDIDATION_STARTED = "credibility_credidation_started"
    CREDIBILITY_CREDIDATION_REVIEW_SUBMITTED = "credibility_credidation_review_submitted"
    CREDIBILITY_CREDIDATION_COMPLETED = "credibility_credidation_completed"
    CREDIBILITY_REPAIR_REQUESTED = "credibility_repair_requested"
    CREDIBILITY_REPAIR_SUBMITTED = "credibility_repair_submitted"
    CREDIBILITY_CLAIM_ADMITTED = "credibility_claim_admitted"
    CREDIBILITY_CLAIM_EXCLUDED = "credibility_claim_excluded"
    CREDIBILITY_FINALIZATION_STARTED = "credibility_finalization_started"
    CREDIBILITY_REPORT_CREATED = "credibility_report_created"
    # --- F080 neutral leader-judge ---
    JUDGE_EVALUATION_STARTED = "judge_evaluation_started"
    JUDGE_VERDICT = "judge_verdict"
    # --- F081 credibility debate structure ---
    CREDIBILITY_ENTAILMENT_CHECKED = "credibility_entailment_checked"
    CREDIBILITY_PHASE_STARTED = "credibility_phase_started"
    # --- F082 credibility gate hardening ---
    CREDIBILITY_VALIDITY_CHECKED = "credibility_validity_checked"


class EventStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    # Phase 1 additions (F031-1a Fix 3):
    PAUSED = "paused"
    RESUMED = "resumed"
    AWAITING_USER_DECISION = "awaiting_user_decision"


# Run metadata status vocabulary, mirroring the spec.
TERMINAL_RUN_STATUSES: frozenset[str] = frozenset(
    {"completed", "cancelled", "failed", "interrupted"}
)
NON_TERMINAL_RUN_STATUSES: frozenset[str] = frozenset(
    {"created", "running", "paused", "finalizing", "awaiting_user_decision"}
)


@dataclass(frozen=True)
class MemberSnapshot:
    member_id: str
    name: str
    role: str
    provider_display: str
    model_display: str
    locality: str            # "local" | "remote" | "fake"
    context_access: str
    transcript_access: str
    catalog_version: str | None = None
    _extras: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class CouncilEventError:
    """Sanitized error payload (invariant 12).

    Tests and UIs branch on ``code``. ``message`` is human-readable but
    must not contain secrets, raw provider exceptions, or hidden context.
    """
    code: str
    message: str
    retryable: bool
    details: dict[str, Any] = field(default_factory=dict)
    _extras: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class CouncilEvent:
    format_version: int
    id: str
    run_id: str
    sequence: int
    type: EventType
    status: EventStatus
    created_at: str
    payload: dict[str, Any]
    member_id: str | None = None
    member_snapshot: MemberSnapshot | None = None
    round: int | None = None
    turn_index: int | None = None
    parent_event_ids: list[str] = field(default_factory=list)
    usage: dict[str, Any] | None = None
    context: dict[str, Any] | None = None
    audit: dict[str, Any] | None = None
    error: CouncilEventError | None = None
    _extras: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "format_version": self.format_version,
            "id": self.id,
            "run_id": self.run_id,
            "sequence": self.sequence,
            "type": self.type.value,
            "status": self.status.value,
            "created_at": self.created_at,
            "payload": dict(self.payload),
        }
        if self.member_id is not None:
            d["member_id"] = self.member_id
        if self.member_snapshot is not None:
            d["member_snapshot"] = _emit_nested(self.member_snapshot)
        if self.round is not None:
            d["round"] = self.round
        if self.turn_index is not None:
            d["turn_index"] = self.turn_index
        if self.parent_event_ids:
            d["parent_event_ids"] = list(self.parent_event_ids)
        if self.usage is not None:
            d["usage"] = dict(self.usage)
        if self.context is not None:
            d["context"] = dict(self.context)
        if self.audit is not None:
            d["audit"] = dict(self.audit)
        if self.error is not None:
            d["error"] = _emit_nested(self.error)
        d.update(self._extras)
        return d

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "CouncilEvent":
        fv = int(raw.get("format_version", 0))
        if fv != FORMAT_VERSION:
            raise UnsupportedFormatVersion(fv)
        snap_raw = raw.get("member_snapshot")
        err_raw = raw.get("error")
        _, extras = _split_unknown(cls, raw)
        snapshot: MemberSnapshot | None = None
        if snap_raw:
            snap_known, snap_extras = _split_unknown(MemberSnapshot, snap_raw)
            snapshot = MemberSnapshot(**snap_known, _extras=snap_extras)
        error_obj: CouncilEventError | None = None
        if err_raw:
            err_known, err_extras = _split_unknown(CouncilEventError, err_raw)
            error_obj = CouncilEventError(**err_known, _extras=err_extras)
        return cls(
            format_version=fv,
            id=str(raw["id"]),
            run_id=str(raw["run_id"]),
            sequence=int(raw["sequence"]),
            type=EventType(raw["type"]),
            status=EventStatus(raw["status"]),
            created_at=str(raw["created_at"]),
            payload=dict(raw.get("payload") or {}),
            member_id=raw.get("member_id"),
            member_snapshot=snapshot,
            round=raw.get("round"),
            turn_index=raw.get("turn_index"),
            parent_event_ids=list(raw.get("parent_event_ids") or []),
            usage=dict(raw["usage"]) if raw.get("usage") is not None else None,
            context=dict(raw["context"]) if raw.get("context") is not None else None,
            audit=dict(raw["audit"]) if raw.get("audit") is not None else None,
            error=error_obj,
            _extras=extras,
        )


@dataclass(frozen=True)
class CouncilMember:
    # All fields except ``id`` carry defaults so a partial member dict — e.g.
    # the minimal shape the room editor emits when a member is freshly added
    # (id/name/provider/route/access/system_prompt, no catalog metadata) —
    # deserializes instead of raising. This mirrors EscalationRosterEntry,
    # which is intentionally lenient for the same reason. Field ORDER is
    # unchanged, so positional construction and to_dict output are unaffected;
    # existing rooms (which carry every field) round-trip byte-identically.
    id: str
    name: str = ""
    role: str = "answerer"
    enabled: bool = True
    gateway_route_id: str | None = None
    # F129: Single keeps the historical fixed route. Multi carries an explicit
    # route pool and receives a concrete route for each task/turn.
    model_mode: str = "single"
    model_pool: list[str] = field(default_factory=list)
    provider_kind: str = "unknown"   # "local" | "remote" | "custom" | "unknown"
    provider_display: str = ""
    model_display: str = ""
    catalog_version: str | None = None
    context_access: str = "prompt_only"
    transcript_access: str = "own_messages"
    turn_limits: dict[str, Any] = field(default_factory=dict)
    generation: dict[str, Any] = field(default_factory=dict)
    system_prompt: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    _extras: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class TopologyPolicy:
    kind: str
    max_rounds: int | None
    max_total_turns: int | None
    max_messages_per_member: int | None
    speaker_order: list[str] = field(default_factory=list)
    allow_user_interjection: bool = False
    stop_when: dict[str, Any] = field(default_factory=dict)
    _extras: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class ContextPolicy:
    default_context_access: str
    default_transcript_access: str
    allow_full_context: bool
    require_confirmation_for_remote_context: bool
    require_confirmation_for_full_context: bool
    member_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    redaction_profile_id: str | None = None
    summary_profile_id: str | None = None
    _extras: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class BudgetPolicy:
    max_rounds: int | None
    max_messages_per_member: int | None
    max_total_model_calls: int | None
    max_remote_calls_per_run: int | None
    max_remote_calls_per_day: int | None
    max_input_tokens_per_turn: int | None
    max_output_tokens_per_turn: int | None
    max_context_tokens_per_member: int | None
    max_estimated_usd_per_run: float | None
    max_estimated_usd_per_month: float | None
    warn_at_fraction: list[float] = field(default_factory=list)
    on_budget_exhausted: str = "stop"
    require_confirmation_before_first_remote_call: bool = True
    require_confirmation_above_estimated_usd: float | None = None
    # F037 expert-callout caps. Additive; default to None/0 so existing
    # rooms with no callout config keep today's budget semantics.
    max_callouts_per_run: int | None = None
    max_callouts_per_round: int | None = None
    max_remote_callouts_per_run: int | None = None
    max_estimated_callout_usd_per_run: float | None = None
    require_confirmation_before_first_remote_callout: bool = True
    require_confirmation_above_callout_estimated_usd: float | None = None
    # F038 Council Steward caps. External stewards can add model calls; remote
    # stewards are opt-in and default to zero remote budget.
    max_steward_calls_per_run: int | None = None
    max_remote_steward_calls_per_run: int | None = 0
    max_estimated_steward_usd_per_run: float | None = None
    require_confirmation_before_remote_steward: bool = True
    _extras: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class FinalizationPolicy:
    mode: str
    finalizer_member_id: str | None = None
    judge_member_ids: list[str] = field(default_factory=list)
    require_judge_verdict: bool = False
    allow_minority_report: bool = True
    allow_grounding_write: bool = False
    grounding_requires_user_accept: bool = True
    _extras: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class EscalationRosterEntry:
    """F037 expert-callout target.

    Intentionally mirrors :class:`CouncilMember` (including ``metadata``) so
    the context builder and gateway request path are reused verbatim. Roster
    entries are NOT ordinary enabled members; they are virtual turn targets
    the scheduler uses only when a callout is admitted. All fields except
    ``id`` carry defaults so partial config JSON is accepted.
    """
    id: str
    name: str = ""
    role: str = "expert"
    gateway_route_id: str | None = None
    provider_kind: str = "unknown"
    provider_display: str = ""
    model_display: str = ""
    catalog_version: str | None = None
    context_access: str = "redacted_summary"
    transcript_access: str = "summary_only"
    system_prompt: str = ""
    turn_limits: dict[str, Any] = field(default_factory=dict)
    generation: dict[str, Any] = field(default_factory=dict)
    # callout sub-bag: {"advisory": bool, "allow_recursive_callouts": bool}
    callout: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    _extras: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "EscalationRosterEntry":
        known, extras = _split_unknown(cls, raw or {})
        return cls(**known, _extras=extras)

    def to_dict(self) -> dict[str, Any]:
        return _emit_nested(self)


@dataclass(frozen=True)
class EscalationPolicy:
    """F037 room-level escalation policy. Default-off."""
    enabled: bool = False
    requester_mode: str = "user_only"
    requester_member_ids: list[str] = field(default_factory=list)
    requester_roles: list[str] = field(default_factory=list)
    approval_mode: str = "ask_user"
    auto_approve_under_estimated_usd: float | None = None
    require_user_approval_before_first_remote_callout: bool = True
    max_callouts_per_run: int = 1
    max_callouts_per_round: int = 1
    max_remote_callouts_per_run: int = 1
    max_estimated_callout_usd_per_run: float | None = None
    on_callout_rejected: str = "continue"
    on_callout_failed: str = "continue"
    allow_topology_triggers: bool = False
    auto_after_no_consensus_rounds: int | None = None
    allow_steward_recommendations: bool = True
    default_context_access: str = "redacted_summary"
    default_transcript_access: str = "summary_only"
    allow_voting_callouts: bool = False
    _extras: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "EscalationPolicy":
        known, extras = _split_unknown(cls, raw or {})
        return cls(**known, _extras=extras)

    def to_dict(self) -> dict[str, Any]:
        return _emit_nested(self)


@dataclass(frozen=True)
class StewardAssignment:
    """F038 steward assignment.

    ``mode="member"`` points at an existing enabled Council member.
    ``mode="external"`` points at a gateway route used only to maintain
    Steward Packets. Defaults stay local and non-voting.
    """
    mode: str = "external"
    member_id: str | None = None
    gateway_route_id: str | None = "local.summary-model"
    provider_kind: str = "local"
    name: str = "Council Steward"
    _extras: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return _emit_nested(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "StewardAssignment":
        if raw is None:
            return cls()
        known, extras = _split_unknown(cls, raw)
        return cls(**known, _extras=extras)


@dataclass(frozen=True)
class StewardPolicy:
    """F038 room-level Steward policy. Default-off."""
    enabled: bool = False
    assignment: StewardAssignment = field(default_factory=StewardAssignment)
    packet_mode: str = "hybrid"
    recipient_mode: str = "shared"
    cadence: str = "after_each_round"
    recent_full_messages: int = 2
    max_packet_tokens: int = 1200
    include_member_positions: bool = True
    include_open_disagreements: bool = True
    include_risk_flags: bool = True
    include_callout_recommendation: bool = True
    allow_raw_expansion: bool = True
    show_packet_audit_to_user: bool = True
    fallback_on_failure: str = "full_transcript"
    remote_steward_allowed: bool = False
    _extras: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        extras = d.pop("_extras", {}) or {}
        d["assignment"] = self.assignment.to_dict()
        d.update(extras)
        return d

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "StewardPolicy":
        if raw is None:
            return cls()
        known, extras = _split_unknown(cls, raw)
        assignment = StewardAssignment.from_dict(known.pop("assignment", None))
        return cls(**known, assignment=assignment, _extras=extras)


@dataclass(frozen=True)
class ToolEnabledPolicy:
    enabled: bool = False
    _extras: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return _emit_nested(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "ToolEnabledPolicy":
        known, extras = _split_unknown(cls, raw or {})
        return cls(**known, _extras=extras)


@dataclass(frozen=True)
class ToolWebFetchPolicy:
    enabled: bool = False
    allowed_domains: list[str] = field(default_factory=list)
    _extras: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return _emit_nested(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "ToolWebFetchPolicy":
        known, extras = _split_unknown(cls, raw or {})
        return cls(**known, _extras=extras)


@dataclass(frozen=True)
class ToolCodeReadPolicy:
    enabled: bool = False
    workspace_path: str | None = None
    _extras: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return _emit_nested(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "ToolCodeReadPolicy":
        known, extras = _split_unknown(cls, raw or {})
        return cls(**known, _extras=extras)


@dataclass(frozen=True)
class ToolCodeWritePolicy:
    enabled: bool = False
    mode: str = "propose_only"
    _extras: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return _emit_nested(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "ToolCodeWritePolicy":
        known, extras = _split_unknown(cls, raw or {})
        return cls(**known, _extras=extras)


@dataclass(frozen=True)
class ToolCodeExecPolicy:
    enabled: bool = False
    network: bool = False
    timeout_seconds: int = 120
    _extras: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return _emit_nested(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "ToolCodeExecPolicy":
        known, extras = _split_unknown(cls, raw or {})
        return cls(**known, _extras=extras)


@dataclass(frozen=True)
class ToolExecutionPolicy:
    location: str = "local"
    sandbox: str = "none"
    sandbox_image: str | None = None
    _extras: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return _emit_nested(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "ToolExecutionPolicy":
        known, extras = _split_unknown(cls, raw or {})
        return cls(**known, _extras=extras)


@dataclass(frozen=True)
class ToolBudgetPolicy:
    max_tool_calls_per_run: int | None = None
    max_tool_cost_usd_per_run: float | None = None
    _extras: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return _emit_nested(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "ToolBudgetPolicy":
        known, extras = _split_unknown(cls, raw or {})
        return cls(**known, _extras=extras)


@dataclass(frozen=True)
class ToolLoopPolicy:
    max_iterations: int = 4
    require_human_accept_final: bool = True
    _extras: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return _emit_nested(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "ToolLoopPolicy":
        known, extras = _split_unknown(cls, raw or {})
        return cls(**known, _extras=extras)


@dataclass(frozen=True)
class ToolPolicy:
    """F039 room-level tool policy. Default-off and fail-closed."""

    web_fetch: ToolWebFetchPolicy = field(default_factory=ToolWebFetchPolicy)
    web_search: ToolEnabledPolicy = field(default_factory=ToolEnabledPolicy)
    code_read: ToolCodeReadPolicy = field(default_factory=ToolCodeReadPolicy)
    code_write: ToolCodeWritePolicy = field(default_factory=ToolCodeWritePolicy)
    code_exec: ToolCodeExecPolicy = field(default_factory=ToolCodeExecPolicy)
    execution: ToolExecutionPolicy = field(default_factory=ToolExecutionPolicy)
    budget: ToolBudgetPolicy = field(default_factory=ToolBudgetPolicy)
    loop: ToolLoopPolicy = field(default_factory=ToolLoopPolicy)
    require_first_use_consent: bool = True
    _extras: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        extras = d.pop("_extras", {}) or {}
        for key, value in list(d.items()):
            if isinstance(value, dict):
                value.pop("_extras", None)
        d.update(extras)
        return d

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "ToolPolicy":
        if raw is None:
            return cls()
        known, extras = _split_unknown(cls, raw)
        return cls(
            web_fetch=ToolWebFetchPolicy.from_dict(known.pop("web_fetch", None)),
            web_search=ToolEnabledPolicy.from_dict(known.pop("web_search", None)),
            code_read=ToolCodeReadPolicy.from_dict(known.pop("code_read", None)),
            code_write=ToolCodeWritePolicy.from_dict(known.pop("code_write", None)),
            code_exec=ToolCodeExecPolicy.from_dict(known.pop("code_exec", None)),
            execution=ToolExecutionPolicy.from_dict(known.pop("execution", None)),
            budget=ToolBudgetPolicy.from_dict(known.pop("budget", None)),
            loop=ToolLoopPolicy.from_dict(known.pop("loop", None)),
            **known,
            _extras=extras,
        )

    def enabled_tool_ids(self) -> set[str]:
        enabled: set[str] = set()
        if self.web_fetch.enabled:
            enabled.add("web_fetch")
        if self.web_search.enabled:
            enabled.add("web_search")
        if self.code_read.enabled:
            enabled.add("code_read")
        if self.code_write.enabled:
            enabled.add("code_write")
        if self.code_exec.enabled:
            enabled.add("code_exec")
        return enabled


@dataclass(frozen=True)
class CredibilityPolicy:
    """F078 Credibility-mode policy. Default-off and fail-closed; serialized on
    the room only when non-default (mirrors ToolPolicy).

    Flat by design — every field is a scalar so the room editor can round-trip
    it without nested forms, and ``_extras`` preserves forward-compat keys.
    """

    enabled: bool = False
    strictness: str = "normal"            # light | normal | strict
    leader_member_id: str | None = None   # None ⇒ resolve to last speaker
    require_search: bool = True
    require_fetch: bool = True
    min_fetched_sources_per_member: int = 2
    min_sources_per_key_claim: int = 1
    min_independent_sources_per_high_risk_claim: int = 2
    require_primary_sources_when_available: bool = True
    allow_secondary_sources: bool = True
    allow_news_sources: bool = True
    allow_blogs: bool = False
    allow_forums: bool = False
    allow_search_snippet_evidence: bool = False
    recency_days: int | None = None
    max_credibility_cycles: int = 1
    max_searches_per_member: int = 3
    max_fetches_per_member: int = 5
    max_review_fetches_per_member: int = 2
    max_claims_per_member: int = 12
    max_reviews_per_member: int = 16
    max_repair_passes: int = 1
    require_two_reviewers_for_key_claims: bool = False
    include_excluded_claims_in_final: bool = True
    allow_downgrade_consent: bool = False
    fallback_on_tool_failure: str = "report_incomplete"
    # F081 debate structure (additive, default-off so pre-F081 rooms are
    # byte-identical). rigor: lenient (today) | standard (entailment gate +
    # novelty termination) | adversarial (+ auto-opponent + phases).
    rigor: str = "lenient"
    require_entailment: bool = False
    novelty_exhaustion_rounds: int = 2
    auto_assign_opponent: bool = False
    # F082: route "inference"-graded claims (source silent) to the argument-
    # validity judge. Default off so pre-F082 rooms are byte-identical.
    route_inference_to_validity: bool = False
    _extras: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return _emit_nested(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "CredibilityPolicy":
        known, extras = _split_unknown(cls, raw or {})
        return cls(**known, _extras=extras)


# Strictness values for CredibilityPolicy.strictness.
CREDIBILITY_STRICTNESS = frozenset({"light", "normal", "strict"})
# Tool-failure fallback modes for CredibilityPolicy.fallback_on_tool_failure.
CREDIBILITY_FALLBACKS = frozenset({
    "report_incomplete", "stop_run", "downgrade_to_normal",
})


@dataclass(frozen=True)
class JudgePolicy:
    """F080 neutral leader-judge. Default-off; serialized on the room only when
    non-default (mirrors ToolPolicy / CredibilityPolicy).

    A judge is a designated member that NEVER takes a deliberation turn (it is
    excluded from the speaker order) and holds NO opinion of its own. From
    ``start_round`` on, after each round completes the judge reads the round and
    returns a structured verdict: the members reached a conclusion (stop early),
    or keep going. When the run hits its round/budget limit without a verdict,
    the judge may break the tie (``tie_break``) by choosing strictly among the
    members' stated positions — never introducing its own answer.
    """

    enabled: bool = False
    judge_member_id: str | None = None   # None ⇒ resolve a role=="judge" member
    start_round: int = 1                 # earliest round the judge may end on
    tie_break: bool = True               # decide at the limit when undecided
    _extras: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return _emit_nested(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "JudgePolicy":
        known, extras = _split_unknown(cls, raw or {})
        return cls(**known, _extras=extras)


def resolve_judge_member_id(room: "CouncilRoom") -> str | None:
    """Resolve the neutral judge member id, or None when no judge is set.

    Explicit ``judge_policy.judge_member_id`` wins; otherwise the first member
    whose ``role`` is ``"judge"``. Returns None when the policy is disabled.
    """
    pol = room.judge_policy
    if not pol.enabled:
        return None
    if pol.judge_member_id:
        return pol.judge_member_id
    for m in room.members:
        if str(getattr(m, "role", "")) == "judge":
            return m.id
    return None


def resolve_credibility_leader(room: "CouncilRoom") -> str | None:
    """Resolve the Credibility finalizer member id.

    Explicit ``leader_member_id`` wins. When unset, fall back to the last id in
    ``topology.speaker_order`` if present, else the last enabled member, else
    the last member — mirroring the spec's ``speaker_order[-1]`` rule.
    """
    pol = room.credibility_policy
    if pol.leader_member_id:
        return pol.leader_member_id
    order = [mid for mid in room.topology.speaker_order if mid]
    if order:
        return order[-1]
    enabled = [m.id for m in room.members if getattr(m, "enabled", True)]
    if enabled:
        return enabled[-1]
    if room.members:
        return room.members[-1].id
    return None


@dataclass(frozen=True)
class CouncilRoom:
    format_version: int
    id: str
    name: str
    description: str
    members: list[CouncilMember]
    topology: TopologyPolicy
    context_policy: ContextPolicy
    budget_policy: BudgetPolicy
    finalization_policy: FinalizationPolicy
    created_at: str
    updated_at: str
    revision: int
    preset_id: str | None = None
    status_hint: str = "draft"
    ui: dict[str, Any] = field(default_factory=dict)
    last_validated_at: str | None = None
    # F037 expert callouts. Default-off empty policy + no roster so existing
    # rooms are unchanged.
    escalation_policy: "EscalationPolicy" = field(default_factory=lambda: EscalationPolicy())
    escalation_roster: list["EscalationRosterEntry"] = field(default_factory=list)
    # F038 Council Steward. Default-off so existing rooms are unchanged.
    steward_policy: "StewardPolicy" = field(default_factory=lambda: StewardPolicy())
    # F039 tool use. Default-off; omitted from serialized room JSON until non-default.
    tool_policy: "ToolPolicy" = field(default_factory=lambda: ToolPolicy())
    # F078 Credibility mode. Default-off; omitted from serialized JSON until non-default.
    credibility_policy: "CredibilityPolicy" = field(
        default_factory=lambda: CredibilityPolicy()
    )
    # F080 neutral leader-judge. Default-off; omitted from serialized JSON until
    # non-default.
    judge_policy: "JudgePolicy" = field(default_factory=lambda: JudgePolicy())
    # F095 room-level default corpus binding. The room editor and Council run
    # picker write this; a per-run ``corpus_ids`` override still wins. Empty by
    # default and omitted from serialized JSON until non-empty so pre-F095 rooms
    # round-trip byte-identically. Reads tolerate the legacy ``_extras`` home.
    corpus_ids: list[str] = field(default_factory=list)
    _extras: dict[str, Any] = field(default_factory=dict, repr=False)

    def effective_corpus_ids(self) -> list[str]:
        """Room default corpus binding, tolerating the legacy ``_extras`` home.

        F095: ``corpus_ids`` is now a typed field, but rooms seeded before it
        existed (e.g. the Phase 5 demo room) carry the list in ``_extras``.
        Prefer the typed field; fall back to ``_extras`` so old rooms keep
        their binding.
        """
        if self.corpus_ids:
            return list(self.corpus_ids)
        return list((self._extras or {}).get("corpus_ids") or [])

    def to_dict(self) -> dict[str, Any]:
        def _emit_member(member: CouncilMember) -> dict[str, Any]:
            out = _emit_nested(member)
            # Preserve byte-compatible legacy room bodies until a member uses
            # F129. A retained non-empty pool is emitted even in Single mode so
            # toggling modes in the editor is lossless.
            if member.model_mode == "single":
                out.pop("model_mode", None)
            if not member.model_pool:
                out.pop("model_pool", None)
            return out

        d: dict[str, Any] = {
            "format_version": self.format_version,
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "preset_id": self.preset_id,
            "status_hint": self.status_hint,
            "members": [_emit_member(m) for m in self.members],
            "topology": _emit_nested(self.topology),
            "context_policy": _emit_nested(self.context_policy),
            "budget_policy": _emit_nested(self.budget_policy),
            "finalization_policy": _emit_nested(self.finalization_policy),
            "ui": dict(self.ui),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_validated_at": self.last_validated_at,
            "revision": self.revision,
        }
        # F037: emit escalation config only when it carries content, so rooms
        # without callouts serialize byte-identically to pre-F037 behavior.
        if self.escalation_policy != EscalationPolicy():
            d["escalation_policy"] = _emit_nested(self.escalation_policy)
        if self.escalation_roster:
            d["escalation_roster"] = [_emit_nested(e) for e in self.escalation_roster]
        if self.steward_policy != StewardPolicy():
            d["steward_policy"] = self.steward_policy.to_dict()
        if self.tool_policy != ToolPolicy():
            d["tool_policy"] = self.tool_policy.to_dict()
        if self.credibility_policy != CredibilityPolicy():
            d["credibility_policy"] = self.credibility_policy.to_dict()
        if self.judge_policy != JudgePolicy():
            d["judge_policy"] = self.judge_policy.to_dict()
        # F095: emit only when bound so pre-F095 rooms stay byte-identical.
        extras = dict(self._extras)
        if self.corpus_ids:
            extras.pop("corpus_ids", None)
            d["corpus_ids"] = list(self.corpus_ids)
        d.update(extras)
        return d

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "CouncilRoom":
        fv = int(raw.get("format_version", 0))
        if fv != FORMAT_VERSION:
            raise UnsupportedFormatVersion(fv)
        _, room_extras = _split_unknown(cls, raw)

        members: list[CouncilMember] = []
        for m_raw in raw.get("members") or []:
            m_known, m_extras = _split_unknown(CouncilMember, m_raw)
            members.append(CouncilMember(**m_known, _extras=m_extras))

        topo_known, topo_extras = _split_unknown(TopologyPolicy, raw["topology"])
        ctx_known, ctx_extras = _split_unknown(ContextPolicy, raw["context_policy"])
        bud_known, bud_extras = _split_unknown(BudgetPolicy, raw["budget_policy"])
        fin_known, fin_extras = _split_unknown(FinalizationPolicy, raw["finalization_policy"])

        esc_known, esc_extras = _split_unknown(
            EscalationPolicy, raw.get("escalation_policy") or {}
        )
        roster: list[EscalationRosterEntry] = []
        for e_raw in raw.get("escalation_roster") or []:
            e_known, e_extras = _split_unknown(EscalationRosterEntry, e_raw)
            roster.append(EscalationRosterEntry(**e_known, _extras=e_extras))
        steward = StewardPolicy.from_dict(raw.get("steward_policy"))
        tool_policy = ToolPolicy.from_dict(raw.get("tool_policy"))
        credibility_policy = CredibilityPolicy.from_dict(raw.get("credibility_policy"))
        judge_policy = JudgePolicy.from_dict(raw.get("judge_policy"))

        return cls(
            format_version=fv,
            id=str(raw["id"]),
            name=str(raw["name"]),
            description=str(raw.get("description", "")),
            preset_id=raw.get("preset_id"),
            status_hint=str(raw.get("status_hint", "draft")),
            members=members,
            topology=TopologyPolicy(**topo_known, _extras=topo_extras),
            context_policy=ContextPolicy(**ctx_known, _extras=ctx_extras),
            budget_policy=BudgetPolicy(**bud_known, _extras=bud_extras),
            finalization_policy=FinalizationPolicy(**fin_known, _extras=fin_extras),
            ui=dict(raw.get("ui") or {}),
            created_at=str(raw["created_at"]),
            updated_at=str(raw["updated_at"]),
            last_validated_at=raw.get("last_validated_at"),
            revision=int(raw["revision"]),
            escalation_policy=EscalationPolicy(**esc_known, _extras=esc_extras),
            escalation_roster=roster,
            steward_policy=steward,
            tool_policy=tool_policy,
            credibility_policy=credibility_policy,
            judge_policy=judge_policy,
            corpus_ids=list(raw.get("corpus_ids") or []),
            _extras=room_extras,
        )


@dataclass(frozen=True)
class RunMeta:
    format_version: int
    id: str
    room_id: str
    room_snapshot: dict[str, Any]
    prompt: str
    corpus_ids: list[str]
    status: str
    created_at: str
    started_at: str | None
    updated_at: str
    finished_at: str | None
    last_sequence: int
    event_count: int
    terminal_event_id: str | None
    resume_policy: str
    costs: dict[str, Any]
    capabilities: dict[str, Any]
    conversation_id: str | None = None
    conversation_turn_id: str | None = None
    # Phase 1 additions (F031-1a) — all defaulted (invariant 11):
    completed_messages_by_member: dict[str, int] = field(default_factory=dict)
    total_messages_completed: int = 0
    paused_at: str | None = None
    cancel_requested_at: str | None = None
    terminal_reason: str | None = None
    # Latest user/operator decision persisted durably so the scheduler can
    # observe it after restart and the audit UI can render it. Shape:
    # {"choice": "stop"|"skip_member"|"continue_local_only",
    #  "scope": "current_turn"|"current_round"|"remainder_of_run",
    #  "requested_by": str, "at": ISO8601}.
    last_decision: dict[str, Any] | None = None
    # Route-side control events that arrived while the scheduler held the
    # writer token. The scheduler drains this list at each checkpoint,
    # appending each event under its writer so the user's pause/resume/cancel
    # action shows up in the transcript. Each entry shape:
    # {"type": EventType.value, "status": EventStatus.value, "payload": {...}}
    pending_control_events: list[dict[str, Any]] = field(default_factory=list)
    _extras: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        extras = d.pop("_extras", {}) or {}
        d.update(extras)
        return d

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "RunMeta":
        fv = int(raw.get("format_version", 0))
        if fv != FORMAT_VERSION:
            raise UnsupportedFormatVersion(fv)
        _, extras = _split_unknown(cls, raw)
        return cls(
            format_version=fv,
            id=str(raw["id"]),
            room_id=str(raw["room_id"]),
            room_snapshot=dict(raw.get("room_snapshot") or {}),
            conversation_id=raw.get("conversation_id"),
            conversation_turn_id=raw.get("conversation_turn_id"),
            prompt=str(raw.get("prompt", "")),
            corpus_ids=list(raw.get("corpus_ids") or []),
            status=str(raw["status"]),
            created_at=str(raw["created_at"]),
            started_at=raw.get("started_at"),
            updated_at=str(raw["updated_at"]),
            finished_at=raw.get("finished_at"),
            last_sequence=int(raw.get("last_sequence", 0)),
            event_count=int(raw.get("event_count", 0)),
            terminal_event_id=raw.get("terminal_event_id"),
            resume_policy=str(raw.get("resume_policy", "mark_interrupted")),
            costs=dict(raw.get("costs") or {}),
            capabilities=dict(raw.get("capabilities") or {}),
            completed_messages_by_member=dict(raw.get("completed_messages_by_member") or {}),
            total_messages_completed=int(raw.get("total_messages_completed", 0)),
            paused_at=raw.get("paused_at"),
            cancel_requested_at=raw.get("cancel_requested_at"),
            terminal_reason=raw.get("terminal_reason"),
            last_decision=raw.get("last_decision"),
            pending_control_events=list(raw.get("pending_control_events") or []),
            _extras=extras,
        )


@dataclass(frozen=True)
class RunSummary:
    id: str
    room_id: str
    status: str
    updated_at: str
    event_count: int
    last_sequence: int
