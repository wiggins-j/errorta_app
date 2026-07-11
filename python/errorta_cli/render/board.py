"""Task views — compact ``tasks`` table + columnar ``board`` (``GET /backlog``).

Task: ``{task_id, title, role, state(todo|doing|blocked|done|dropped), detail,
depends_on, ...}`` (``ledger.Task.to_dict``). Plain Rich only — the full-screen
Textual board (``/board --tui``) is explicitly out of S2.
"""
from __future__ import annotations

from typing import Any

from rich.columns import Columns
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import muted, render, role_style, truncate

# Board columns (dropped is intentionally hidden — it's not active work).
_COLUMNS = ("todo", "doing", "blocked", "done")
_STATE_STYLE = {
    "todo": "cli.muted",
    "doing": "cli.warn",
    "blocked": "cli.bad",
    "done": "cli.ok",
    "dropped": "cli.muted",
}


def _tasks(payload: Any) -> list[dict]:
    return list((payload or {}).get("tasks") or [])


def render_tasks(payload: Any, verbosity: Any) -> str:
    tasks = _tasks(payload)
    if not tasks:
        return render(muted("(no tasks)"))
    table = Table(show_edge=False, pad_edge=False, box=None)
    table.add_column("state", no_wrap=True)
    table.add_column("role", no_wrap=True)
    table.add_column("task", no_wrap=True, style="cli.muted")
    table.add_column("title")
    for t in tasks:
        state = str(t.get("state") or "")
        table.add_row(
            Text(state, style=_STATE_STYLE.get(state, "white")),
            Text(str(t.get("role") or ""), style=role_style(t.get("role"))),
            str(t.get("task_id") or ""),
            truncate(t.get("title"), 72),
        )
    return render(table)


def render_board(payload: Any, verbosity: Any) -> str:
    tasks = _tasks(payload)
    if not tasks:
        return render(muted("(no tasks)"))
    by_state: dict[str, list[dict]] = {c: [] for c in _COLUMNS}
    for t in tasks:
        by_state.get(str(t.get("state") or ""), []).append(t)
    panels = []
    for col in _COLUMNS:
        items = by_state[col]
        body = Text()
        if not items:
            body.append("—", style="cli.muted")
        for i, t in enumerate(items):
            if i:
                body.append("\n")
            body.append("• ", style=_STATE_STYLE.get(col, "white"))
            body.append(truncate(t.get("title"), 28))
        panels.append(
            Panel(
                body,
                title=f"{col} ({len(items)})",
                title_align="left",
                border_style=_STATE_STYLE.get(col, "white"),
                width=34,
            )
        )
    return render(Columns(panels, equal=False, expand=False))
