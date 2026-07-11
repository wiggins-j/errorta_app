"""Governance view (``GET /governance`` → ``governance`` summary + ``status``).

Read-only (approvals/settings are S6). We surface the mode/phase + the
plain-language status stage/stepper without dumping the full artifact bodies.
"""
from __future__ import annotations

from typing import Any

from rich.table import Table
from rich.text import Text

from . import heading, muted, render, truncate


def render_governance(payload: Any, verbosity: Any) -> str:
    governance = (payload or {}).get("governance") or {}
    status = (payload or {}).get("status") or {}
    # The route returns ``{"governance": GovernanceStore.summary(...), "status": ...}``
    # and the settings live under ``summary()["state"]`` (``GovernanceState.to_dict``),
    # NOT at the governance top level (which carries state/artifacts/reviews/approvals).
    state = governance.get("state") or {}
    if not state and not status:
        return render(muted("(no governance state)"))
    table = Table(show_edge=False, pad_edge=False, box=None, show_header=False)
    table.add_column("k", style="cli.key", no_wrap=True)
    table.add_column("v")
    for key in ("mode", "phase", "human_code_approval", "max_review_rounds",
                "block_on_problems", "monitor"):
        if state.get(key) not in (None, ""):
            table.add_row(key, str(state.get(key)))
    parts = [heading("Governance"), table]
    stage = status.get("stage")
    stat = status.get("status")
    if stage or stat:
        parts.append(Text(f"stage: {stage or '?'}  status: {stat or '?'}", style="cli.muted"))
    # ``governance_status()`` emits ``headline`` (never ``label``/``message``).
    headline = status.get("headline")
    if headline:
        parts.append(muted(truncate(headline, 100)))
    # ``summary()`` carries ``approvals`` (a list of GovernanceApproval dicts, each
    # with a ``state`` field); count the pending ones.
    pending = [
        a for a in (governance.get("approvals") or [])
        if isinstance(a, dict) and a.get("state") == "pending"
    ]
    if pending:
        parts.append(Text(f"pending approvals: {len(pending)}", style="cli.warn"))
    return render(*parts)
