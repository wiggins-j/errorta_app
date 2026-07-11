""""What the PM learned" view — ``GET /model-learning`` + ``GET /model-usage``.

model-learning: ``{learning:{summary{total_attempts, distinct_routes, ...},
thresholds{min_attempts, demotion_rate, preferred_rate}, routes:[...]}}``.
model-usage: ``{usage:{multi_members, single_members}}`` (per-project assignments).
"""
from __future__ import annotations

from typing import Any

from rich.table import Table

from . import heading, muted, render, truncate


def render_models(payload: Any, verbosity: Any) -> str:
    learning = (payload or {}).get("learning") or {}
    usage = (payload or {}).get("usage") or {}
    summary = learning.get("summary") or {}
    routes = learning.get("routes") or []
    parts = [heading("Model learning (cross-project)")]

    if summary:
        parts.append(
            muted(
                "attempts={a}  routes={r}  window={w}d".format(
                    a=summary.get("total_attempts", 0),
                    r=summary.get("distinct_routes", 0),
                    w=summary.get("window_days", "?"),
                )
            )
        )
    if routes:
        table = Table(show_edge=False, pad_edge=False, box=None)
        table.add_column("route", style="cli.key", no_wrap=True)
        table.add_column("attempts", justify="right", no_wrap=True)
        table.add_column("accepted", justify="right", no_wrap=True)
        for r in routes[:20]:
            if not isinstance(r, dict):
                continue
            table.add_row(
                truncate(r.get("route_id") or r.get("route"), 40),
                str(r.get("attempts", "")),
                _rate(r),
            )
        parts.append(table)
    else:
        parts.append(muted("(no learning corpus yet)"))

    multi = usage.get("multi_members") or []
    escalated = [m for m in multi if m.get("escalations")]
    if escalated:
        parts.append(heading("this project — escalations"))
        for m in escalated:
            parts.append(
                muted(f"  {m.get('member_id')}: {len(m.get('escalations') or [])} escalation(s)")
            )
    return render(*parts)


def _rate(route: dict) -> str:
    rate = route.get("accepted_rate")
    if rate is None:
        accepted = route.get("accepted")
        attempts = route.get("attempts")
        if isinstance(accepted, (int, float)) and attempts:
            rate = accepted / attempts
    if isinstance(rate, (int, float)):
        return f"{float(rate) * 100:.0f}%"
    return ""
