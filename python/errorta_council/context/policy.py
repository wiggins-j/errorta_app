"""EffectiveContextPolicy — min_policy() across member/room/topology/corpus/residency/caps.

Fail-closed (invariant 4): unknown values BLOCK; never approximate to a known one.
The policy result captures BOTH requested and effective so the manifest can
render narrowing reasons in the inspection drawer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

_CONTEXT_ACCESS_ORDER = (
    "none", "prompt_only", "transcript_only", "summary_only", "redacted_summary",
    "retrieved_snippets", "redacted_snippets", "answer_context", "full_context",
)
_RANK = {name: i for i, name in enumerate(_CONTEXT_ACCESS_ORDER)}


@dataclass(frozen=True)
class EffectivePolicy:
    requested_context_access: str
    effective_context_access: str
    requested_transcript_access: str
    effective_transcript_access: str
    destination_scope: str
    egress_class: str
    narrowed_by: list[str] = field(default_factory=list)
    blocked_reason: str | None = None
    max_input_tokens: int = 8192


class EffectiveContextPolicy:
    @staticmethod
    def compute(
        *,
        member_request: dict[str, Any],
        room: dict[str, Any],
        topology: dict[str, Any],
        corpus_policy: dict[str, Any],
        residency: dict[str, Any],
        token_caps: dict[str, Any],
    ) -> EffectivePolicy:
        requested = str(member_request.get("context_access", "prompt_only"))
        requested_transcript = str(member_request.get("transcript_access", "none"))
        destination_scope = str(residency.get("destination_scope", "local"))

        if requested not in _RANK:
            return EffectivePolicy(
                requested_context_access=requested,
                effective_context_access="blocked",
                requested_transcript_access=requested_transcript,
                effective_transcript_access="none",
                destination_scope=destination_scope,
                egress_class="blocked",
                blocked_reason="unknown_context_access",
                max_input_tokens=int(token_caps.get("max_input_tokens", 8192)),
            )

        narrowed_by: list[str] = []
        effective = requested

        room_ceiling = str(room.get("context_access_ceiling", "full_context"))
        if room_ceiling in _RANK and _RANK[effective] > _RANK[room_ceiling]:
            effective = room_ceiling
            narrowed_by.append("room.context_access_ceiling")

        topo_ceiling = str(topology.get("context_access_ceiling", "full_context"))
        if topo_ceiling in _RANK and _RANK[effective] > _RANK[topo_ceiling]:
            effective = topo_ceiling
            narrowed_by.append("topology.context_access_ceiling")

        if effective == "full_context" and not bool(room.get("allow_full_context", False)):
            effective = "redacted_summary"
            narrowed_by.append("room.allow_full_context=false")

        max_egress = str(corpus_policy.get("max_egress_class", "remote_eligible"))
        blocked_reason: str | None = None
        if destination_scope == "remote" and max_egress == "local_only":
            if effective in {"retrieved_snippets", "answer_context", "full_context"}:
                # F031-05 §"Examples" — corpus_egress_blocked is the
                # default fail-closed result. Degradation to
                # ``redacted_summary`` is only allowed when the corpus
                # policy explicitly opts into a fallback (the field
                # honors both ``corpus_egress_fallback`` and the older
                # ``fallback_on_corpus_egress_blocked`` name for
                # forward-compat with future router-spec edits).
                fallback = (
                    corpus_policy.get("corpus_egress_fallback")
                    or corpus_policy.get("fallback_on_corpus_egress_blocked")
                )
                if fallback == "redacted_summary":
                    effective = "redacted_summary"
                    narrowed_by.append(
                        "corpus.max_egress_class=local_only:fallback=redacted_summary"
                    )
                else:
                    # Fail-closed: block this context build before any
                    # provider initialization (invariant 4).
                    return EffectivePolicy(
                        requested_context_access=requested,
                        effective_context_access="blocked",
                        requested_transcript_access=requested_transcript,
                        effective_transcript_access="none",
                        destination_scope=destination_scope,
                        egress_class="blocked",
                        narrowed_by=narrowed_by
                        + ["corpus.max_egress_class=local_only"],
                        blocked_reason="corpus_egress_blocked",
                        max_input_tokens=int(token_caps.get("max_input_tokens", 8192)),
                    )

        egress_class = "local" if destination_scope == "local" else max_egress

        return EffectivePolicy(
            requested_context_access=requested,
            effective_context_access=effective,
            requested_transcript_access=requested_transcript,
            effective_transcript_access=requested_transcript,
            destination_scope=destination_scope,
            egress_class=egress_class,
            narrowed_by=narrowed_by,
            blocked_reason=blocked_reason,
            max_input_tokens=int(token_caps.get("max_input_tokens", 8192)),
        )


__all__ = ["EffectiveContextPolicy", "EffectivePolicy"]
