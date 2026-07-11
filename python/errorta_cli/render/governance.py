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
    if not governance and not status:
        return render(muted("(no governance state)"))
    table = Table(show_edge=False, pad_edge=False, box=None, show_header=False)
    table.add_column("k", style="cli.key", no_wrap=True)
    table.add_column("v")
    for key in ("mode", "phase", "human_code_approval", "max_review_rounds",
                "block_on_problems", "monitor"):
        if governance.get(key) not in (None, ""):
            table.add_row(key, str(governance.get(key)))
    parts = [heading("Governance"), table]
    stage = status.get("stage")
    state = status.get("status")
    if stage or state:
        parts.append(Text(f"stage: {stage or '?'}  status: {state or '?'}", style="cli.muted"))
    label = status.get("label") or status.get("message")
    if label:
        parts.append(muted(truncate(label, 100)))
    approvals = governance.get("pending_approvals") or status.get("pending_approvals")
    if approvals:
        parts.append(Text(f"pending approvals: {approvals}", style="cli.warn"))
    return render(*parts)
