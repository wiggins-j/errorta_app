"""F039 slice 7 — build_review topology (the agentic coding loop).

Sequences a coding council: a ``programmer`` member proposes/runs work (via
code_* tool grants), then the other members review it, repeating until the
reviewers sign off or a hard iteration cap hits. Pure ordering + stop logic —
no I/O, no event writes (invariant 2). The actual tool calls happen because the
programmer's turn emits tool-call JSON the scheduler handles; this topology only
decides who speaks next and when to stop.

Order within an iteration: programmer first, then each reviewer in order.
Sign-off: when every reviewer's latest message in the just-finished iteration
signals approval, the run completes ``review_signed_off``. Otherwise the
programmer revises in the next iteration, up to ``max_iterations``.

Auto-apply (writing the approved patch under a git worktree + checkpoint) is a
follow-up — code_write stays propose_only here, so a build_review run proposes
diffs + runs tests + collects sign-off without mutating the user's tree.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from errorta_council.limits import ReasonCode, SchedulerPolicy
from errorta_council.state import RunCounters

from .round_robin import RunCompletion, TurnProposal

REVIEW_SIGNED_OFF = "review_signed_off"

# Self-contained safety net: used only if a caller supplies no cap at all.
# build_and_run always validates a positive max_rounds, so this is never hit in
# production — it just guarantees the topology can't loop forever if misused.
_DEFAULT_ITERATION_CAP = 25

# Explicit sign-off markers — deliberately strict so "I do not approve" or a
# passing mention of "approve" in prose does NOT count.
_SIGNOFF_TOKENS = frozenset({"approve", "approved", "lgtm", "sign-off", "signed off"})
_REQUEST_CHANGES_TOKENS = frozenset({"request_changes", "request changes", "changes requested"})


def _is_signoff(content: str) -> bool:
    text = (content or "").strip()
    if not text:
        return False
    # Structured: {"review_verdict": "approve"} or {"verdict": "approve"}.
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            verdict = str(obj.get("review_verdict") or obj.get("verdict") or "").strip().lower()
            if verdict in {"approve", "approved", "lgtm"}:
                return True
            if verdict:
                return False
    except (ValueError, TypeError):
        pass
    low = text.lower()
    if any(tok in low for tok in _REQUEST_CHANGES_TOKENS):
        return False
    # Prose: a standalone marker line (avoids matching "do not approve").
    for line in low.splitlines():
        if line.strip().strip(".!*_# ") in _SIGNOFF_TOKENS:
            return True
    return False


@dataclass
class BuildReviewTopology:
    max_iterations: int | None = None

    def propose_next(
        self, run: dict[str, Any], transcript: list[Any]
    ) -> TurnProposal | RunCompletion:
        members: list[dict[str, Any]] = [
            m for m in run["members"] if m.get("enabled", True)
        ]
        if not members:
            return RunCompletion(reason=ReasonCode.NO_ELIGIBLE_MEMBERS.value)
        counters: RunCounters = run["counters"]
        policy: SchedulerPolicy = run["policy"]
        events: list[dict[str, Any]] = run.get("events", [])

        programmer = next(
            (m for m in members if str(m.get("role", "")).lower() == "programmer"),
            members[0],
        )
        reviewers = [m for m in members if m["id"] != programmer["id"]]
        order = [programmer] + reviewers

        def _attempts(m_id: str) -> int:
            return max(
                counters.attempts_by_member.get(m_id, 0),
                counters.completed_messages_by_member.get(m_id, 0),
            )

        # Global total-message cap pre-empts everything.
        if (
            policy.max_total_member_messages is not None
            and counters.total_messages_completed >= policy.max_total_member_messages
        ):
            return RunCompletion(reason=ReasonCode.LIMITS_EXHAUSTED.value)

        completed_iterations = min(_attempts(m["id"]) for m in order)
        # At an iteration boundary (everyone has spoken `completed_iterations`
        # times), decide whether to sign off, cap out, or start another round.
        at_boundary = all(_attempts(m["id"]) == completed_iterations for m in order)
        if at_boundary and completed_iterations >= 1:
            if reviewers and self._reviewers_signed_off(reviewers, events):
                return RunCompletion(reason=REVIEW_SIGNED_OFF)
            if completed_iterations >= self._iteration_cap(policy):
                return RunCompletion(reason=ReasonCode.LIMITS_EXHAUSTED.value)

        # Next speaker: first member in [programmer, *reviewers] who is behind.
        next_member = next(
            (m for m in order if _attempts(m["id"]) <= completed_iterations and
             _attempts(m["id"]) == min(_attempts(x["id"]) for x in order)),
            order[0],
        )
        new_round = completed_iterations + 1
        if new_round > self._iteration_cap(policy):
            return RunCompletion(reason=ReasonCode.LIMITS_EXHAUSTED.value)

        return TurnProposal(
            member_id=next_member["id"],
            round=new_round,
            turn_index=counters.total_messages_completed,
        )

    def _iteration_cap(self, policy: SchedulerPolicy) -> int:
        caps = [c for c in (self.max_iterations, policy.max_rounds) if c is not None]
        # Defense in depth: even if a caller passes an all-None policy (the
        # engine always validates max_rounds, so this can't happen via
        # build_and_run), the loop is self-contained-safe and never runs away.
        return min(caps) if caps else _DEFAULT_ITERATION_CAP

    def _reviewers_signed_off(
        self, reviewers: list[dict[str, Any]], events: list[Any]
    ) -> bool:
        latest: dict[str, str] = {}
        for ev in events:
            etype = _ev_field(ev, "type")
            if hasattr(etype, "value"):
                etype = etype.value
            if etype != "member_message":
                continue
            mid = str(_ev_field(ev, "member_id") or "")
            payload = _ev_field(ev, "payload") or {}
            content = payload.get("content") if isinstance(payload, dict) else None
            if isinstance(content, str):
                latest[mid] = content  # events are in order; keep the last
        for r in reviewers:
            if not _is_signoff(latest.get(r["id"], "")):
                return False
        return True


def _ev_field(ev: Any, name: str, default: Any = None) -> Any:
    """Read a field from a CouncilEvent object OR a plain dict (tests)."""
    if isinstance(ev, dict):
        return ev.get(name, default)
    return getattr(ev, name, default)
