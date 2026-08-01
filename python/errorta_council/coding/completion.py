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


# SPEC-35 G2 — the acceptance gate's verdict for the `done` chokepoint.
AcceptanceGateStatus = Literal["no_gate", "green", "red", "stale"]


def acceptance_gate_status(ledger: Any, current_head: str) -> AcceptanceGateStatus:
    """SPEC-35 G2: classify the project's acceptance gate at ``current_head``.

    * ``no_gate``  — no acceptance-scoped command is registered (nothing to gate).
    * ``green``    — the acceptance gate's own latest result is a pass AT this head.
    * ``red``      — a result exists AT this head and it did not pass (fixable).
    * ``stale``    — a gate is registered but its latest result is at a different
                     head, or it has never run (needs a fresh run before `done`).

    READ-ONLY and fail-open: any read error, or an unresolvable ``current_head``,
    returns ``no_gate`` so this can never INVENT a block (the module's fail-closed
    rule applies to open *work*; a `done`-block must fail *open* — a spurious block
    with no recovery is the wedge SPEC-34's review forbids). Recovery is structural:
    the in-loop gate re-runs the registered command every merge, so a ``red``/``stale``
    verdict flips to ``green`` on its own once the tree is fixed.
    """
    head = str(current_head or "")
    if not head:
        return "no_gate"  # cannot bind a result to a head -> never block
    try:
        cmds = ledger.get_test_commands() or {}
        has_acc = any(
            str((spec or {}).get("scope", "unit")) == "acceptance"
            for spec in cmds.values())
    except Exception:  # noqa: BLE001 — never invent a block
        return "no_gate"
    if not has_acc:
        return "no_gate"
    try:
        from .gate_state import latest_acceptance_result
        res = latest_acceptance_result(ledger)
    except Exception:  # noqa: BLE001
        return "no_gate"
    if not res:
        return "stale"  # registered but never run -> force a fresh run
    if str(res.get("head") or "") != head:
        return "stale"  # a result, but not for the tree we are about to call done
    return "green" if res.get("passed") else "red"


__all__ = [
    "OpenItem",
    "pending_completion_work",
    "summarize_open_items",
    "count_human_required",
    "acceptance_gate_status",
    "AcceptanceGateStatus",
]
