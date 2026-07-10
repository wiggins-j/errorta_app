"""Dataclasses for the F031-07 transform pipeline.

format_version: 1 (invariant 11). Every persisted shape carries it.
Artifacts are content-addressed; raw `content` strings are in-memory only
and never persisted (F031-07 §"Auditability").
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

TRANSFORM_FORMAT_VERSION = 1

_ARTIFACT_KINDS = {"redacted_summary", "redacted_snippets", "summary_only"}
_STATUSES = {"allowed", "degraded", "blocked", "error"}


@dataclass(frozen=True)
class SourceEnvelope:
    class_: str            # "user_prompt" | "task_instructions" | "retrieved_snippet" | "transcript_event" | "summary"
    corpus_id: str | None
    chunk_id: str | None
    citation_id: str | None
    content: str           # in-memory only; never persisted
    content_sha256: str
    tokens: int | None
    sensitivity: str       # "known_local" | "may_contain_corpus" | "unknown" | "remote_eligible"


@dataclass(frozen=True)
class SummaryFreshnessAnchors:
    transcript_cursor: int
    retrieval_cursor: int
    source_hashes: list[str]
    corpus_policy_version: int
    redaction_version: int
    summarizer_version: int
    created_at: str        # UTC ISO-8601 'Z'


@dataclass(frozen=True)
class TransformPolicy:
    requested_context_access: str
    destination_scope: str
    redact_first_then_summarize_then_redact: bool = True
    fallback_on_summarizer_fatal: str = "block"
    max_summary_tokens: int = 512
    redaction_version: int = 1
    summarizer_version: int = 1
    corpus_policy_version: int = 1


@dataclass(frozen=True)
class TransformRequest:
    run_id: str
    turn_id: str
    member_id: str
    destination_scope: str
    requested_context_access: str
    requested_egress_class: str
    corpus_ids: list[str]
    source_envelopes: list[SourceEnvelope]
    transcript_cursor: int
    retrieval_cursor: int
    max_output_tokens: int
    policy: TransformPolicy


@dataclass(frozen=True)
class TransformResult:
    status: str
    artifact_id: str | None
    artifact_kind: str | None
    content: str | None
    content_sha256: str | None
    egress_class: str
    destination_scope: str
    token_estimate: dict[str, Any]
    manifest_id: str
    blocked_reason: str | None
    message_code: str | None
    warnings: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.status not in _STATUSES:
            raise ValueError(f"bad status: {self.status!r}")
        if self.artifact_kind is not None and self.artifact_kind not in _ARTIFACT_KINDS:
            raise ValueError(f"bad artifact_kind: {self.artifact_kind!r}")


@dataclass(frozen=True)
class TransformManifest:
    format_version: int
    manifest_id: str
    run_id: str
    turn_id: str
    member_id: str
    created_at: str
    artifact_kind: str | None
    status: str
    source_refs: list[dict[str, Any]]
    redaction_rule_counts: dict[str, int]
    summarizer_route_id: str | None
    freshness_anchors: SummaryFreshnessAnchors | None
    payload_sha256: str | None
    blocked_reason: str | None
    warnings: list[str] = field(default_factory=list)


__all__ = [
    "TRANSFORM_FORMAT_VERSION",
    "SourceEnvelope",
    "SummaryFreshnessAnchors",
    "TransformPolicy",
    "TransformRequest",
    "TransformResult",
    "TransformManifest",
]
