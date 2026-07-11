"""Team view — resolved members from the run-config projection.

The only read-only surface for the team is ``GET /model-usage`` (``usage``):
``{multi_members:[{member_id, role, model_mode, pool, assignments, escalations}],
single_members:[{member_id, route_id}]}``. A coding project stores no room — it
stores a ``run_config`` of members; ``model-usage`` is its read-only projection.
(Full member editing — mode/enable/pool set — is S4.)
"""
from __future__ import annotations

from typing import Any

from rich.table import Table
from rich.text import Text

from . import heading, muted, render, role_style, truncate


def render_team(payload: Any, verbosity: Any) -> str:
    usage = (payload or {}).get("usage") or {}
    multi = usage.get("multi_members") or []
    single = usage.get("single_members") or []
    if not multi and not single:
        return render(muted("(no team configured — set one via run setup / wizard)"))
    table = Table(show_edge=False, pad_edge=False, box=None)
    table.add_column("member", style="cli.key", no_wrap=True)
    table.add_column("role", no_wrap=True)
    table.add_column("mode", no_wrap=True)
    table.add_column("route / pool")
    for m in single:
        table.add_row(
            str(m.get("member_id") or ""),
            Text(str(m.get("role") or ""), style=role_style(m.get("role"))),
            "single",
            truncate(m.get("route_id"), 48),
        )
    for m in multi:
        pool = m.get("pool") or []
        table.add_row(
            str(m.get("member_id") or ""),
            Text(str(m.get("role") or ""), style=role_style(m.get("role"))),
            "multi",
            truncate(", ".join(str(r) for r in pool), 48),
        )
    return render(heading("Team"), table)
