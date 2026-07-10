"""F031-06 transcript visibility resolver.

Pure function over (member, run, cursor, topology_state). No I/O, no event
writes. Decides which prior Council events a scheduled member may see for
one turn. Returns a frozen VisibilityPlan.

Invariants:
- 5 (sealed): fresh plan per call; cache keys derive from visibility_plan_id.
- 7 (caps absolute): topology ceiling clamps member request DOWN; never widens.
- 11 (additive): format_version = 1.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

VISIBILITY_VERSION = 1

# Vocabulary from F031-06 §Transcript access levels. Order is restrictive→permissive
# for the topology-ceiling clamp.
_ACCESS_ORDER: tuple[str, ...] = (
    "none",
    "user_only",
    "own_and_user",
    "previous_speaker",
    "local_member_messages",
    "role_scoped",
    "summary_only",
    "redacted_summary",
    "all_messages",
)
_ACCESS_RANK = {name: i for i, name in enumerate(_ACCESS_ORDER)}


@dataclass(frozen=True)
class VisibilityPlan:
    visibility_version: int
    visibility_plan_id: str
    requested_transcript_access: str
    topology_ceiling: str
    effective_transcript_access: str
    destination_scope: str
    transcript_cursor: int
    selected_event_ids: list[str] = field(default_factory=list)
    selected_sequences: list[int] = field(default_factory=list)
    summary_event_ids: list[str] = field(default_factory=list)
    redaction_artifact_ids: list[str] = field(default_factory=list)
    omitted: list[dict[str, Any]] = field(default_factory=list)
    blocked_reason: str | None = None
    warnings: list[str] = field(default_factory=list)


def _events_visible_to(events: Sequence[Mapping[str, Any]], cursor: int) -> list[Mapping[str, Any]]:
    return [e for e in events if int(e.get("sequence", 0)) <= cursor]


def _select(events: Sequence[Mapping[str, Any]], access: str, scheduled_member_id: str,
            members: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    user_ids = {m["member_id"] for m in members if m.get("role") == "user"}
    local_ids = {m["member_id"] for m in members if m.get("provider_class") == "local"}
    scheduled_role = next(
        (m.get("role") for m in members if m["member_id"] == scheduled_member_id),
        None,
    )
    if access == "none":
        return []
    if access == "user_only":
        return [e for e in events if e.get("member_id") in user_ids]
    if access == "own_and_user":
        return [
            e for e in events
            if e.get("member_id") in user_ids or e.get("member_id") == scheduled_member_id
        ]
    if access == "previous_speaker":
        msg = [
            e for e in events
            if e.get("type") == "member_message" and e.get("member_id") != scheduled_member_id
        ]
        return msg[-1:] if msg else []
    if access == "local_member_messages":
        return [
            e for e in events
            if e.get("member_id") in local_ids or e.get("member_id") in user_ids
        ]
    if access == "role_scoped":
        return [
            e for e in events
            if next(
                (m.get("role") for m in members if m["member_id"] == e.get("member_id")),
                None,
            ) == scheduled_role
            or e.get("member_id") in user_ids
        ]
    if access == "all_messages":
        return list(events)
    # summary_only / redacted_summary handled by transform pipeline.
    return []


class TranscriptVisibilityResolver:
    """Pure-function resolver. No I/O."""

    def resolve(
        self,
        *,
        member: Mapping[str, Any],
        run: Mapping[str, Any],
        transcript_cursor: int,
        topology_state: Mapping[str, Any],
    ) -> VisibilityPlan:
        requested = str(member.get("requested_transcript_access", "none"))
        ceiling = str(topology_state.get("transcript_access_ceiling", "all_messages"))
        destination_scope = str(member.get("destination_scope", "local"))
        scheduled_id = str(run.get("scheduled_member_id", member.get("member_id")))
        members = list(run.get("members", []))
        room_policy = dict(run.get("room_policy", {}))
        allow_unknown_local = bool(room_policy.get("allow_unknown_sensitivity_local", False))

        warnings: list[str] = []
        omitted: list[dict[str, Any]] = []

        # Invariant 7: topology can only NARROW. Clamp down; never widen.
        req_rank = _ACCESS_RANK.get(requested, 99)
        ceil_rank = _ACCESS_RANK.get(ceiling, 99)
        if req_rank > ceil_rank:
            effective = ceiling
            warnings.append(f"topology_clamp:{requested}->{ceiling}")
        else:
            effective = requested

        events_in_window = _events_visible_to(list(run.get("events", [])), transcript_cursor)

        plan_id = "vp-" + hashlib.sha256(
            f"{run.get('run_id','')}|{scheduled_id}|{transcript_cursor}|{effective}".encode()
        ).hexdigest()[:16]

        # Invariant 4: unknown-sensitivity on remote-bound turns → block whole turn.
        unknown_seen = [
            e for e in events_in_window
            if str(e.get("payload", {}).get("sensitivity", "known_local")) == "unknown"
        ]
        if destination_scope == "remote" and unknown_seen:
            return VisibilityPlan(
                visibility_version=VISIBILITY_VERSION,
                visibility_plan_id=plan_id,
                requested_transcript_access=requested,
                topology_ceiling=ceiling,
                effective_transcript_access="none",
                destination_scope=destination_scope,
                transcript_cursor=int(transcript_cursor),
                blocked_reason="unknown_sensitivity_remote",
                warnings=warnings,
                omitted=[{"event_id": str(e["id"]), "reason": "unknown_sensitivity"}
                         for e in unknown_seen],
            )

        # Local-bound: drop unknown-sensitivity events unless policy permits.
        if destination_scope != "remote" and not allow_unknown_local:
            kept = []
            for e in events_in_window:
                if str(e.get("payload", {}).get("sensitivity", "known_local")) == "unknown":
                    omitted.append({"event_id": str(e["id"]), "reason": "unknown_sensitivity"})
                else:
                    kept.append(e)
            events_in_window = kept

        selected = _select(events_in_window, effective, scheduled_id, members)
        selected_ids = [str(e["id"]) for e in selected]
        selected_seqs = [int(e["sequence"]) for e in selected]

        return VisibilityPlan(
            visibility_version=VISIBILITY_VERSION,
            visibility_plan_id=plan_id,
            requested_transcript_access=requested,
            topology_ceiling=ceiling,
            effective_transcript_access=effective,
            destination_scope=destination_scope,
            transcript_cursor=int(transcript_cursor),
            selected_event_ids=selected_ids,
            selected_sequences=selected_seqs,
            omitted=omitted,
            warnings=warnings,
        )


__all__ = ["TranscriptVisibilityResolver", "VisibilityPlan", "VISIBILITY_VERSION"]
