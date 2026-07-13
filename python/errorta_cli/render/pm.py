"""PM read views — ``pm chat`` (``GET /pm-chat``) + ``pm changes`` (``GET /pm-changes``).

Read-only (applying/accept/decline is S6). The call layer stamps ``_sub`` so this
renderer knows which sub-view to draw.
"""
from __future__ import annotations

from typing import Any

from rich.table import Table
from rich.text import Text

from . import heading, muted, render, role_style, truncate, ts


def render_pm(payload: Any, verbosity: Any) -> str:
    sub = (payload or {}).get("_sub")
    if sub == "changes":
        return _render_changes(payload)
    return _render_chat(payload)


def _chat_line(msg: dict) -> Text:
    """One PM-chat turn as a colorized line (shared by the block render + the
    F158 `pm chat --watch` tail)."""
    role = str(msg.get("role") or "user")
    body = msg.get("message") or msg.get("text") or msg.get("content") or ""
    line = Text()
    line.append(f"{ts(msg.get('at')):>8} ", style="cli.muted")
    line.append(f"{role:<8} ", style=role_style(role))
    line.append(truncate(body, 120))
    return line


def _render_chat(payload: Any) -> str:
    thread = (payload or {}).get("thread") or []
    if not thread:
        return render(muted("(no PM chat history)"))
    return render(heading("PM chat"), *[_chat_line(m) for m in thread])


# F158 stream hooks — extractor + per-turn renderer for `pm chat --watch`.
def thread_entries(payload: Any) -> list:
    return (payload or {}).get("thread") or []


def render_chat_entries(entries: list) -> list[str]:
    return [render(_chat_line(m)) for m in entries]


def _render_changes(payload: Any) -> str:
    pending = (payload or {}).get("pending") or []
    recent = (payload or {}).get("recent") or []
    parts = [heading("PM changes")]
    if pending:
        parts.append(muted(f"pending ({len(pending)}):"))
        parts.append(_change_table(pending))
    else:
        parts.append(muted("(no pending PM changes)"))
    if recent:
        parts.append(muted(f"recent ({len(recent)}):"))
        parts.append(_change_table(recent))
    return render(*parts)


def _change_table(changes: list[dict]) -> Table:
    table = Table(show_edge=False, pad_edge=False, box=None)
    table.add_column("id", style="cli.key", no_wrap=True)
    table.add_column("kind", no_wrap=True)
    table.add_column("status", no_wrap=True)
    table.add_column("summary")
    for c in changes:
        if not isinstance(c, dict):
            continue
        table.add_row(
            str(c.get("change_id") or c.get("id") or ""),
            str(c.get("kind") or c.get("action") or c.get("type") or ""),
            str(c.get("status") or ""),
            truncate(c.get("summary") or c.get("description") or c.get("title"), 70),
        )
    return table
