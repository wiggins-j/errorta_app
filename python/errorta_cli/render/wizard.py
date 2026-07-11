"""Rich rendering for the ``wizard`` model-list short-circuit (F147 §7.3).

The interactive chat prints incrementally through the command's ``write`` seam;
this renderer only formats the terminal sentinels (the model list a
non-interactive / ``--json`` call surfaces, and the created/aborted summaries).
"""
from __future__ import annotations

from typing import Any

from rich.table import Table

from . import heading, muted, render, truncate


def render_wizard(payload: Any) -> str:
    kind = (payload or {}).get("_kind")
    if kind != "models":
        return render(muted("wizard: nothing to show"))
    routes = ((payload or {}).get("models") or {}).get("routes") or []
    if not routes:
        return render(muted("(no wizard-capable model routes — connect a provider first)"))
    table = Table(show_edge=False, pad_edge=False, box=None)
    table.add_column("route", style="cli.key", no_wrap=True)
    table.add_column("label")
    for r in routes:
        if isinstance(r, dict):
            table.add_row(str(r.get("route_id") or ""), truncate(r.get("label"), 48))
    return render(
        heading("Wizard models"),
        table,
        muted("start a chat: errorta wizard --model <route>"),
    )
