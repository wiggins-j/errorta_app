"""F102 RC2 — the accept/delivered marker used to gate GitHub publishing.

PURE + COUNCIL-SIDE: reads the project decision log only (no egress). A project
becomes "delivered" the moment the worktree/accept route records a
``choice="delivered"`` decision (the existing accept event). GitHub push paths
(P3/P4) gate on (delivered) AND the merge gate being allowed; manual export
(P1) stays available regardless. ``status`` semantics are untouched — "done" is
PM-completion, a DIFFERENT event (RC2).
"""
from __future__ import annotations

from typing import Any


def delivered_decision(store: Any) -> dict[str, Any] | None:
    """The most recent ``choice="delivered"`` decision, or None. Best-effort:
    any read failure reads as not-delivered (fail-closed for the gate)."""
    try:
        decisions = store.list_decisions()
    except Exception:
        return None
    latest: dict[str, Any] | None = None
    for rec in decisions:
        if isinstance(rec, dict) and rec.get("choice") == "delivered":
            latest = rec  # last-writer-wins (append-only log is chronological)
    return latest


def is_project_delivered(store: Any) -> bool:
    """True once the project has been accepted + delivered at the merge gate."""
    return delivered_decision(store) is not None


def delivered_at(store: Any) -> str | None:
    """The timestamp of the (latest) delivered decision, or None."""
    rec = delivered_decision(store)
    return str(rec.get("at")) if rec and rec.get("at") else None


__all__ = ["is_project_delivered", "delivered_at", "delivered_decision"]
