"""F128 — completion gate: the read-only truth source for "is there open work?"

A PM ``done=true`` claim must be verified against the backlog before it is
accepted (``runner.py`` done-claim chokepoint). This module answers the only
question that gate needs: which tasks/PRs are still open?

READ-ONLY and pure — it never mutates the ledger. Fail-closed: any read error
returns a non-empty sentinel so the caller treats the project as NOT done (a
run that can't prove it's finished must not claim it is).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

# D2 — terminal = finished/abandoned and does NOT block completion.
# Everything else is "open" (blocks done). `blocked` is open by design.
_TERMINAL_TASK_STATES = frozenset({"done", "dropped", "cancelled", "superseded"})
_TERMINAL_PR_STATES = frozenset(
    {"merged", "abandoned", "superseded", "closed", "dropped"}
)

# Open items a human (not the team) must resolve — surfaced distinctly so the
# UI/Problem can route them to a person instead of implying the team can retry.
_HUMAN_REQUIRED_TASK_STATES = frozenset({"blocked"})
_HUMAN_REQUIRED_PR_STATES = frozenset({"conflict", "blocked"})


@dataclass(frozen=True)
class OpenItem:
    """One backlog item that blocks completion."""
    kind: Literal["task", "pr", "unknown"]
    id: str
    title: str
    state: str
    human_required: bool


# Fail-closed sentinel: an unreadable backlog can't prove the project is done.
_UNREADABLE = (OpenItem(kind="unknown", id="", title="backlog unreadable",
                        state="", human_required=True),)


def pending_completion_work(ledger: Any) -> list[OpenItem]:
    """Return the items that block completion: non-terminal tasks + open PRs.

    READ-ONLY. Fail-closed — a read exception returns the ``_UNREADABLE``
    sentinel (non-empty) so the caller refuses a ``done`` claim rather than
    silently completing a project whose state it couldn't verify.
    """
    items: list[OpenItem] = []
    list_tasks = getattr(ledger, "list_tasks_strict", None)
    if not callable(list_tasks):
        list_tasks = getattr(ledger, "list_tasks", None)
    if not callable(list_tasks):
        return list(_UNREADABLE)
    try:
        for t in list_tasks():
            state = str(getattr(t, "state", "") or "")
            if state in _TERMINAL_TASK_STATES:
                continue
            items.append(OpenItem(
                kind="task",
                id=str(getattr(t, "task_id", "") or ""),
                title=str(getattr(t, "title", "") or getattr(t, "task_id", "") or "task"),
                state=state,
                human_required=state in _HUMAN_REQUIRED_TASK_STATES,
            ))
    except Exception:  # noqa: BLE001 — fail closed
        return list(_UNREADABLE)

    list_prs = getattr(ledger, "list_prs_strict", None)
    if not callable(list_prs):
        list_prs = getattr(ledger, "list_prs", None)
    if not callable(list_prs):
        return list(_UNREADABLE)
    try:
        for p in list_prs():
            status = str(p.get("status", "") or "")
            if status in _TERMINAL_PR_STATES:
                continue
            items.append(OpenItem(
                kind="pr",
                id=str(p.get("pr_id", "") or ""),
                title=str(p.get("branch") or p.get("task_id") or p.get("pr_id") or "PR"),
                state=status,
                human_required=status in _HUMAN_REQUIRED_PR_STATES,
            ))
    except Exception:  # noqa: BLE001 — fail closed
        return list(_UNREADABLE)

    return items


def summarize_open_items(items: list[OpenItem], cap: int = 8) -> str:
    """A compact, human-readable list of open items for a decision rationale /
    PM prompt / Problem summary. Caps the list and notes how many were dropped
    so a 250-deep backlog doesn't blow up the string (no silent truncation)."""
    if not items:
        return "no open items"
    shown = items[:cap]
    parts = []
    for it in shown:
        flag = " (human-required)" if it.human_required else ""
        parts.append(f"{it.kind} {it.title} [{it.state}]{flag}")
    extra = len(items) - len(shown)
    if extra > 0:
        parts.append(f"+{extra} more")
    return "; ".join(parts)


def count_human_required(items: list[OpenItem]) -> int:
    return sum(1 for it in items if it.human_required)


__all__ = [
    "OpenItem",
    "pending_completion_work",
    "summarize_open_items",
    "count_human_required",
]
