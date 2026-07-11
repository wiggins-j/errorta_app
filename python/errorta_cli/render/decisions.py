"""Decisions view — the raw ``choice`` event stream (``GET /decisions``).

Item: ``{decision_id, title, context, choice, rationale, alternatives,
related_task_ids, at, ...extra}`` (``ledger.record_decision``). We surface the
enumerated fields only — never the free-form ``extra`` blob.
"""
from __future__ import annotations

import fnmatch
from typing import Any

from rich.table import Table

from . import muted, render, ts


def render_decisions(payload: Any, verbosity: Any) -> str:
    decisions = (payload or {}).get("decisions") or []
    kind = (payload or {}).get("_kind")
    if kind:
        decisions = [d for d in decisions if fnmatch.fnmatch(str(d.get("choice") or ""), kind)]
    if not decisions:
        return render(muted("(no decisions)"))
    table = Table(show_edge=False, pad_edge=False, box=None, expand=False)
    table.add_column("time", style="cli.muted", no_wrap=True)
    table.add_column("choice", style="cli.key", no_wrap=True)
    table.add_column("title")
    for d in decisions:
        table.add_row(
            ts(d.get("at")),
            str(d.get("choice") or ""),
            str(d.get("title") or ""),
        )
    return render(table)
