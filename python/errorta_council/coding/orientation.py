"""F087-01 — derived, token-bounded orientation packet (compaction survival).

The packet is a small projection of the ledger that re-hydrates any agent after
context compaction or a restart. It is DERIVED (never stored) so it always
reflects current ledger state, and it is token-bounded: it trims oldest
decisions, then artifacts, then next_tasks until it fits, but NEVER drops the
North Star, definition of done, or in-flight tasks.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .ledger import LedgerStore

_RECENT_DECISIONS = 8
_RECENT_ARTIFACTS = 12
_NEXT_TASKS = 6


@dataclass(frozen=True)
class OrientationPacket:
    north_star: str
    definition_of_done: str
    # F135 D5: the current-focus directive. A CORE field (never trimmed), pinned
    # alongside north_star / definition_of_done so the PM's "what to do right now"
    # survives budget pressure.
    work_request: str = ""
    # F137: the ordered active Current Focus set (the operative scope). A CORE
    # field (never trimmed) — the PM's "what to build now" must survive
    # compaction. Each item: {id, title, body, order}.
    current_focus: list[dict[str, Any]] = field(default_factory=list)
    in_flight_tasks: list[dict[str, Any]] = field(default_factory=list)
    next_tasks: list[dict[str, Any]] = field(default_factory=list)
    recent_decisions: list[dict[str, Any]] = field(default_factory=list)
    recent_artifacts: list[dict[str, Any]] = field(default_factory=list)
    recent_tool_events: list[dict[str, Any]] = field(default_factory=list)
    active_blockers: list[dict[str, Any]] = field(default_factory=list)
    member_skills: dict[str, dict[str, str]] = field(default_factory=dict)
    member_parse_rates: dict[str, dict[str, Any]] = field(default_factory=dict)
    pr_state: dict[str, Any] = field(default_factory=dict)
    recent_episodes: list[dict[str, Any]] = field(default_factory=list)
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "north_star": self.north_star,
            "definition_of_done": self.definition_of_done,
            "work_request": self.work_request,
            "current_focus": self.current_focus,
            "in_flight_tasks": self.in_flight_tasks,
            "next_tasks": self.next_tasks,
            "pr_state": self.pr_state,
            "recent_episodes": self.recent_episodes,
            "recent_decisions": self.recent_decisions,
            "recent_artifacts": self.recent_artifacts,
            "recent_tool_events": self.recent_tool_events,
            "active_blockers": self.active_blockers,
            "member_skills": self.member_skills,
            "member_parse_rates": self.member_parse_rates,
            "truncated": self.truncated,
        }


def _est_tokens(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, ensure_ascii=False)) // 4


def _compact_task(t: Any) -> dict[str, Any]:
    """Orientation-sized view of a task — just what an agent needs to act, no
    timestamps/detail/result_ref (those are queryable from the ledger)."""
    return {
        "task_id": t.task_id, "title": t.title, "role": t.role,
        "state": t.state, "assignee_member_id": t.assignee_member_id,
    }


def _latest_skill_per_member(store: LedgerStore) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for rec in store.list_skill_uses():  # append order; last wins
        out[rec["member_id"]] = {"skill": rec["skill"], "phase": rec["phase"]}
    return out


def _conflicted_prs(store: LedgerStore) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for pr in store.list_prs():
        if pr.get("status") != "conflict":
            continue
        out.append({
            "pr_id": str(pr.get("pr_id", "")),
            "branch": str(pr.get("branch", "")),
            "task_id": str(pr.get("task_id", "")),
            "conflicts": list(pr.get("conflicts") or []),
            "resolve_attempts": int(pr.get("resolve_attempts") or 0),
            "action": "PM should redispatch a resolve task or block after retry cap",
        })
    return out


def _parse_rates_per_member(store: LedgerStore) -> dict[str, dict[str, Any]]:
    totals: dict[str, dict[str, Any]] = {}
    for rec in store.list_turns():
        member_id = str(rec.get("member_id") or rec.get("role") or "unknown")
        bucket = totals.setdefault(
            member_id,
            {"member_id": member_id, "role": rec.get("role", ""), "ok": 0, "total": 0},
        )
        if not bucket.get("role") and rec.get("role"):
            bucket["role"] = rec.get("role")
        bucket["total"] += 1
        if rec.get("parse_ok") is not False:
            bucket["ok"] += 1
    for bucket in totals.values():
        total = int(bucket["total"])
        bucket["rate"] = round(float(bucket["ok"]) / total, 3) if total else 1.0
    return totals


def build_orientation_packet(store: LedgerStore, *, token_budget: int) -> OrientationPacket:
    """Assemble a token-bounded orientation packet from the ledger."""
    proj = store.get_project()
    in_flight = [_compact_task(t) for t in store.list_tasks(state="doing")]
    blockers = [_compact_task(t) for t in store.list_tasks(state="blocked")]
    next_tasks = [_compact_task(t) for t in store.list_tasks(state="todo")][:_NEXT_TASKS]
    decisions = store.list_decisions()[-_RECENT_DECISIONS:]
    artifacts = store.list_artifacts()[-_RECENT_ARTIFACTS:]
    tool_events = store.list_tool_events(limit=8)
    skills = _latest_skill_per_member(store)
    parse_rates = _parse_rates_per_member(store)
    pr_state = store.pr_state_summary()        # F087-19 #1: first-class PR/test state
    pr_state["conflicted_prs"] = _conflicted_prs(store)
    episodes = store.list_episodes(limit=5)    # F087-19 #5: durable merge memory
    # F137: the ordered active Current Focus set — a never-trimmed core field.
    try:
        current_focus = [
            {"id": f.id, "title": f.title, "body": f.body, "order": f.order}
            for f in store.active_focuses()
        ]
    except Exception:
        current_focus = []
    truncated = False

    def make() -> OrientationPacket:
        return OrientationPacket(
            north_star=proj.north_star, definition_of_done=proj.definition_of_done,
            work_request=proj.work_request, current_focus=current_focus,
            in_flight_tasks=in_flight, next_tasks=next_tasks,
            recent_decisions=decisions, recent_artifacts=artifacts,
            recent_tool_events=tool_events, pr_state=pr_state,
            recent_episodes=episodes,
            active_blockers=blockers, member_skills=skills,
            member_parse_rates=parse_rates, truncated=truncated,
        )

    # Trim order: decisions -> artifacts -> tool events -> episodes -> next_tasks.
    # Core fields (North Star, DoD, in-flight, PR state) are NEVER trimmed; PR
    # state + the freshest episode are the durable integration memory.
    while _est_tokens(make().to_dict()) > token_budget and (
        decisions or artifacts or tool_events or len(episodes) > 1 or next_tasks
    ):
        truncated = True
        if decisions:
            decisions = decisions[1:]
        elif artifacts:
            artifacts = artifacts[1:]
        elif tool_events:
            tool_events = tool_events[1:]
        elif len(episodes) > 1:
            episodes = episodes[1:]
        else:
            next_tasks = next_tasks[:-1]
    return make()
