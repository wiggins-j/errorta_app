"""Scheduler-shaped facade over ContextRouter (Phase 3 Task 12).

The scheduler protocol calls ``context_builder.build(run_meta=, member=,
transcript=)`` and reads ``context["context_id"]`` / ``context["messages"]``.
ContextRouter has a richer Phase 3 API (ContextBuildRequest →
ContextPayload | BlockedContextResult).  RouterContextAdapter is the
seam: it constructs a ContextBuildRequest per turn, calls the router,
and returns a dict shape the scheduler already understands — with two
additions:

- ``manifest_id``: surfaced from the router so the scheduler can stamp
  the CONTEXT_BUILT event payload with it (Phase 3 inspection endpoint
  reads this).
- ``blocked`` / ``blocked_reason``: when the router returns a
  BlockedContextResult (invariant 4 fail-closed), the adapter degrades
  to a sentinel dict; the scheduler emits MEMBER_SKIPPED instead of
  CONTEXT_BUILT and falls through its existing stop/ask/continue
  policy branches.

Phase 0 minimal-payload additivity: the returned dict always carries
``context_id`` + ``messages`` first (the only keys the scheduler reads
into ``LocalCouncilModelRequest``); extra keys are additive.
"""
from __future__ import annotations

from typing import Any, Callable

from errorta_council.schema import CouncilEvent, RunMeta

from .router import (
    BlockedContextResult,
    ContextBuildRequest,
    ContextRouter,
)


class RouterContextAdapter:
    """Adapt ContextRouter to the scheduler's _ContextBuilder protocol."""

    def __init__(
        self,
        *,
        router: ContextRouter,
        room_id: str,
        max_input_tokens: int = 8192,
        destination_scope_for: Callable[[dict], str] | None = None,
    ) -> None:
        self._router = router
        self._room_id = room_id
        self._max_input_tokens = max_input_tokens
        self._destination_scope_for = (
            destination_scope_for or _default_destination_scope_for
        )

    async def build(
        self,
        *,
        run_meta: RunMeta,
        member: dict,
        transcript: list[CouncilEvent],
    ) -> dict[str, Any]:
        round_n = _round_for(member["id"], transcript)
        sequence = len(transcript) + 1
        turn_id = f"{member['id']}-r{round_n}"

        prompt_text = run_meta.prompt or ""
        request = ContextBuildRequest(
            run_id=run_meta.id,
            turn_id=turn_id,
            room_id=self._room_id,
            member_id=str(member["id"]),
            round=round_n,
            sequence=sequence,
            prompt={
                "display_text": prompt_text,
                "normalized_text": prompt_text,
                "signature": "",
            },
            corpus_ids=list(run_meta.corpus_ids or []),
            requested_context_access=str(
                member.get("context_access") or "prompt_only"
            ),
            requested_transcript_access=str(
                member.get("transcript_access") or "own_messages"
            ),
            destination_scope=self._destination_scope_for(member),
            max_input_tokens=int(
                member.get("max_input_tokens") or self._max_input_tokens
            ),
            transcript_cursor=len(transcript),
            summary_cursor=0,
            gateway_route_id=str(
                member.get("gateway_route_id")
                or member.get("route_id")
                or f"{_provider(member)}/{member.get('model') or member['id']}"
            ),
            metadata={
                "force_deliberation_dialect": str(
                    member.get("force_deliberation_dialect") or ""
                ),
                "force_tool_result_compaction": str(
                    member.get("force_tool_result_compaction") or ""
                ),
            },
        )
        result = await self._router.build(request)

        if isinstance(result, BlockedContextResult):
            return {
                "context_id": result.context_id,
                "messages": [],
                "manifest_id": result.manifest_id,
                "blocked": True,
                "blocked_reason": result.blocked_reason or "context_blocked",
            }

        return {
            "context_id": result.context_id,
            "messages": list(result.messages),
            "manifest_id": result.metadata.get("manifest_id"),
            "blocked": False,
            # Surface the router's policy verdict so the scheduler can
            # stamp it into the gateway request metadata; the gateway
            # then re-validates it against the resolved route (invariant
            # 5 boundary re-check, P1 review-finding lock).
            "destination_scope": result.metadata.get("destination_scope")
            or request.destination_scope,
            "egress_class": result.egress_class or "local",
            "metadata": dict(result.metadata),
            "cache_hints": list(result.cache_hints),
        }

    def reconcile_usage(
        self,
        *,
        context_id: str,
        provider: str,
        model: str,
        reported_input_tokens: int | None,
    ) -> float | None:
        return self._router.reconcile_usage(
            context_id=context_id,
            provider=provider,
            model=model,
            reported_input_tokens=reported_input_tokens,
        )


_LOCAL_PROVIDERS = frozenset({"local", "fake"})


def _default_destination_scope_for(member: dict) -> str:
    """Pick the destination scope from the route prefix.

    F034 ships ``anthropic.*`` / ``openai.*`` / ``google.*`` / ``custom.*``
    routes; those members must carry ``destination_scope="remote"`` or the
    gateway-boundary re-check at :func:`verify_payload_route_alignment`
    fails closed with ``payload_route_mismatch``. The earlier "fake or
    local, always" default flattened remote members onto the local egress
    path and made them unreachable in production. Routes that begin with
    ``local.`` or ``fake.`` stay local; anything else is treated as remote.
    """
    p = _provider_class_from_member(member)
    if p == "fake":
        return "fake"
    if p == "local":
        return "local"
    return "remote"


def _provider_class_from_member(member: dict) -> str:
    """Resolve provider class from route prefix first, then provider field.

    Route prefix is authoritative because the F033 room editor writes the
    route id directly; ``provider``/``provider_class`` on the member may
    drift if the room was saved before F034 widened the catalog.
    """
    route_id = str(
        member.get("gateway_route_id") or member.get("route_id") or ""
    )
    if route_id:
        head_dot = route_id.split(".", 1)[0]
        head_slash = route_id.split("/", 1)[0]
        prefix = head_dot if len(head_dot) <= len(head_slash) else head_slash
        if prefix:
            return prefix
    return _provider(member)


def _provider(member: dict) -> str:
    return str(member.get("provider") or member.get("provider_class") or "local")


def _round_for(member_id: str, transcript: list[CouncilEvent]) -> int:
    """Return the round number of the upcoming turn for ``member_id``.

    Counts prior MEMBER_MESSAGE events for this member and adds 1; that
    matches the scheduler's round-robin semantics (round_robin advances
    on completed messages, not started turns).
    """
    from errorta_council.schema import EventType

    n = sum(
        1
        for e in transcript
        if e.type == EventType.MEMBER_MESSAGE and e.member_id == member_id
    )
    return n + 1


__all__ = ["RouterContextAdapter"]
