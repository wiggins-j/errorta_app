"""Consensus deliberation topology.

Round 1: each enabled member answers the original prompt independently
(blind to peer answers — the scheduler honors ``transcript_cursor=0``
on the proposal so no MEMBER_MESSAGE events leak into round-1 contexts).

Round 2+: each member sees prior round messages and is expected to refine
or hold their position. After every member has spoken in a round, the
topology inspects each member's most recent message for a "no-changed-views"
signal. If at least ``consensus_threshold`` of N enabled members signal no
change, the run completes with ``consensus_reached``.

Hard stop: ``policy.max_rounds`` always wins (deterministic upper bound on
cost; honors invariant 9 — no automatic retry, no silent expansion).

Consensus signal:
- ``digest_v1`` outputs: ``digest.delta`` ∈ {None, "", "no_changed_views"}
  counts as "I am holding my position." Anything else (the model wrote a
  delta string) counts as "I refined."
- Plain-prose outputs: a trailing line beginning with ``no_changed_views``
  also counts. This lets non-digest rooms get consensus stop too.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from errorta_council.gateway_local import THINKING_TRACE_MARKER
from errorta_council.limits import ReasonCode, SchedulerPolicy
from errorta_council.state import RunCounters

from .round_robin import RunCompletion, TurnProposal

_NO_CHANGE_DELTAS = {None, "", "no_changed_views", "no change", "no_change"}
_FIRST_JSON_OBJ_RE = re.compile(r"\{.*\}", re.S)


def _first_json_object(text: str) -> dict | None:
    if not text:
        return None
    m = _FIRST_JSON_OBJ_RE.search(text)
    if not m:
        return None
    try:
        candidate = json.loads(m.group(0))
        if isinstance(candidate, dict):
            return candidate
    except (json.JSONDecodeError, ValueError):
        return None
    return None


def _is_no_change_signal(content: str) -> bool:
    """Return True when the member's message signals no change in position.

    A thinking-burn (reasoning trace, no visible answer) is NOT agreement: the
    member produced no visible position, so it must not count toward consensus —
    otherwise a reasoning model that ran out of output budget would silently
    push the council into a hollow "consensus" it never actually voiced. Such a
    burn counts as "still deliberating" (handled by the caller as not-no-change),
    so the run keeps going until the member answers or the round cap is hit.
    The real fix for a chronically-burning model is a larger output budget
    (per-member Max output tokens); see _default_output_tokens_for_model.
    """
    if not content:
        return False
    if content.lstrip().startswith(THINKING_TRACE_MARKER):
        return False
    # digest_v1 path
    obj = _first_json_object(content)
    if obj is not None and obj.get("v") == "digest_v1":
        delta = obj.get("delta")
        return delta in _NO_CHANGE_DELTAS
    # plain-prose fallback
    stripped = content.strip().lower()
    for marker in ("no_changed_views", "no changed views", "i hold my position"):
        if marker in stripped:
            return True
    return False


def _latest_messages_by_member(
    events: list[Any],
    *,
    enabled_ids: list[str],
    in_round: int | None = None,
) -> dict[str, str]:
    """Return the most-recent message content for each enabled member."""
    latest: dict[str, str] = {}
    seq_by_member: dict[str, int] = {}
    for ev in events:
        etype = getattr(ev, "type", None)
        # Accept both EventType enum and string ("member_message")
        if hasattr(etype, "value"):
            etype = etype.value
        if etype != "member_message":
            continue
        mid = getattr(ev, "member_id", None)
        if not mid or mid not in enabled_ids:
            continue
        if in_round is not None and getattr(ev, "round", None) != in_round:
            continue
        seq = getattr(ev, "sequence", 0) or 0
        if mid in seq_by_member and seq <= seq_by_member[mid]:
            continue
        seq_by_member[mid] = seq
        payload = getattr(ev, "payload", None) or {}
        content = payload.get("content") if isinstance(payload, dict) else None
        if isinstance(content, str):
            latest[mid] = content
    return latest


def _run_started_cursor(events: list[Any]) -> int:
    """Index of the first event AFTER run_started.

    Round 1 of the consensus topology freezes visibility to this cursor so
    every member sees the run-started prompt but no peer member_messages.
    """
    for idx, ev in enumerate(events):
        etype = getattr(ev, "type", None)
        if hasattr(etype, "value"):
            etype = etype.value
        if etype == "run_started":
            return idx + 1
    return 0


@dataclass(frozen=True)
class ConsensusDeliberationTopology:
    """Phase-A deliberation: blind round 1, refining rounds 2+, stop on consensus.

    ``consensus_threshold`` is the minimum count of enabled members that
    must signal no-change in the most recent round before the run stops.
    Defaults to "all enabled members" by leaving it None at construction
    and resolving to ``len(enabled)`` at propose time.
    """

    consensus_threshold: int | None = None

    def propose_next(
        self, run: dict[str, Any], transcript: list[Any]
    ) -> TurnProposal | RunCompletion:
        members: list[dict[str, Any]] = run["members"]
        counters: RunCounters = run["counters"]
        policy: SchedulerPolicy = run["policy"]
        events: list[Any] = run.get("events") or []

        enabled = [m for m in members if m.get("enabled", True)]
        if not enabled:
            return RunCompletion(reason=ReasonCode.NO_ELIGIBLE_MEMBERS.value)

        # Total-message cap pre-empts everything.
        if (
            policy.max_total_member_messages is not None
            and counters.total_messages_completed >= policy.max_total_member_messages
        ):
            return RunCompletion(reason=ReasonCode.LIMITS_EXHAUSTED.value)

        enabled_ids = [m["id"] for m in enabled]

        def _attempts(mid: str) -> int:
            attempts = counters.attempts_by_member.get(mid, 0)
            completed = counters.completed_messages_by_member.get(mid, 0)
            return max(attempts, completed)

        per_member_cap = policy.max_messages_per_member

        # Round we are building for: the lowest attempt count + 1 across
        # enabled members. When all members are equal at K, we're starting
        # round K+1. (Indexes from 1 to match round_robin semantics.)
        attempt_counts = [_attempts(m["id"]) for m in enabled]
        target_round = min(attempt_counts) + 1
        if policy.max_rounds is not None and target_round > policy.max_rounds:
            return RunCompletion(reason=ReasonCode.LIMITS_EXHAUSTED.value)

        # Check whether the previous round just finished (every enabled
        # member has attempts >= target_round - 1). If so, evaluate
        # consensus on prev_round BEFORE dispatching the next round.
        prev_round = target_round - 1
        if prev_round >= 1 and all(_attempts(mid) >= prev_round for mid in enabled_ids):
            latest = _latest_messages_by_member(
                events, enabled_ids=enabled_ids, in_round=prev_round
            )
            agreed_ids = [
                mid for mid in enabled_ids
                if mid in latest and _is_no_change_signal(latest[mid])
            ]
            threshold = (
                self.consensus_threshold
                if self.consensus_threshold is not None
                else len(enabled)
            )
            if len(agreed_ids) >= threshold:
                # F064: retain WHO agreed + the threshold/round so the UI can
                # explain the consensus instead of just announcing it.
                return RunCompletion(
                    reason="consensus_reached",
                    detail={
                        "agreed_member_ids": agreed_ids,
                        "threshold": threshold,
                        # The round in which members HELD their position (the
                        # agreement round) — distinct from the synthesizer's
                        # round on the FINAL_ANSWER event. The UI badges turns
                        # in this round.
                        "round": prev_round,
                        "member_count": len(enabled),
                    },
                )

        eligible = [
            m for m in enabled
            if (per_member_cap is None or _attempts(m["id"]) < per_member_cap)
            and _attempts(m["id"]) < target_round
        ]
        if not eligible:
            return RunCompletion(reason=ReasonCode.LIMITS_EXHAUSTED.value)

        # Pick the next member: lowest attempt count, original enabled order.
        min_count = min(_attempts(m["id"]) for m in eligible)
        next_member = next(m for m in eligible if _attempts(m["id"]) == min_count)

        # Round-1 cursor freeze: every member sees the same blind view.
        # Rounds 2+ get the full transcript (default cursor — None).
        cursor: int | None = None
        if target_round == 1:
            cursor = _run_started_cursor(events)

        return TurnProposal(
            member_id=next_member["id"],
            round=target_round,
            turn_index=counters.total_messages_completed,
            transcript_cursor=cursor,
        )
