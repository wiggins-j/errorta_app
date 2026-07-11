"""Attention / Problems view (``GET /attention`` → ``signals`` + ``blocks_stage``).

Signal: ``{id, kind(problem|alert), blocking, source, stage, title, summary,
state, ...}`` (``attention.AttentionSignal.to_dict``).
"""
from __future__ import annotations

from typing import Any

from rich.table import Table
from rich.text import Text

from . import heading, muted, render, truncate

_KIND_STYLE = {"problem": "cli.bad", "alert": "cli.warn"}


def render_attention(payload: Any, verbosity: Any) -> str:
    signals = (payload or {}).get("signals") or []
    blocks_stage = (payload or {}).get("blocks_stage")
    if not signals:
        base = render(muted("(nothing needs attention)"))
        return base
    table = Table(show_edge=False, pad_edge=False, box=None)
    table.add_column("kind", no_wrap=True)
    table.add_column("blk", justify="center", no_wrap=True)
    table.add_column("stage", style="cli.muted", no_wrap=True)
    table.add_column("title")
    table.add_column("state", style="cli.muted", no_wrap=True)
    for s in signals:
        kind = str(s.get("kind") or "")
        blocking = bool(s.get("blocking"))
        table.add_row(
            Text(kind, style=_KIND_STYLE.get(kind, "white")),
            Text("!", style="cli.bad") if blocking else Text("·", style="cli.muted"),
            str(s.get("stage") or ""),
            truncate(s.get("title"), 64),
            str(s.get("state") or ""),
        )
    parts = [heading("Attention"), table]
    if blocks_stage:
        parts.append(Text("a blocking problem is gating the current stage", style="cli.bad"))
    return render(*parts)
