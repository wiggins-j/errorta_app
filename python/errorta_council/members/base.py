"""Council member seam (F001-SEAM equivalent for Council).

Phase 0 ships the minimal Phase-0 ``ContextPayload`` shape resolved by the
architecture spec OQ#1: ``{context_id, messages}``. Phase 3 (F031-05)
extends it additively with ``classes``, ``egress_class``, ``source_refs``,
and ``metadata`` — no breaking rename within ``format_version: 1``
(invariant 11).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class SourceRef:
    """Reference to a context source; no raw content stored here (F031-05)."""
    class_: str
    corpus_id: str | None
    chunk_id: str | None
    citation_id: str | None
    content_sha256: str | None
    tokens: int | None
    transcript_event_id: str | None = None
    sequence: int | None = None
    packed: str | None = None
    # F039 tool-result provenance. Optional and hash-only; raw tool output
    # lives in the tool-result side store, never in ContextManifest.
    tool_call_id: str | None = None
    tool_id: str | None = None
    args_sha256: str | None = None
    produced_at: str | None = None
    tool_egress_class: str | None = None
    result_ref: dict[str, Any] | None = None


@dataclass(frozen=True)
class ContextPayload:
    # Phase 0/1 fields preserved (byte-identical for the minimal case).
    context_id: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    # Phase 3 additive (optional with safe defaults — invariant 11).
    classes: list[str] = field(default_factory=list)
    egress_class: str | None = None
    source_refs: list[SourceRef] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    cache_hints: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class TurnBudget:
    max_input_tokens: int | None
    max_output_tokens: int | None


@dataclass(frozen=True)
class CancellationToken:
    is_cancelled: bool = False


@dataclass(frozen=True)
class MemberTurnResult:
    content: str
    finish_reason: str
    usage: dict[str, Any]
    audit_id: str | None = None


@runtime_checkable
class CouncilMember(Protocol):
    member_id: str
    provider_class: str   # "local" | "remote" | "fake"

    async def generate(
        self,
        payload: ContextPayload,
        *,
        budget: TurnBudget,
        cancel: CancellationToken,
    ) -> MemberTurnResult: ...
