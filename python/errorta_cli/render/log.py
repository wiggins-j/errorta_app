"""Team Log view — colorized by role (F147 §9; ``GET /team-log`` → ``entries``).

Each entry: ``{at, role, member, kind, message}`` (``team_log.build_team_log``).
"""
from __future__ import annotations

from typing import Any

from rich.text import Text

from . import muted, render, role_style, ts


def _filter(entries: list, filters: dict) -> list:
    role = (filters.get("role") or "").lower()
    member = (filters.get("member") or "").lower()
    grep = (filters.get("grep") or "").lower()
    out = []
    for e in entries:
        if role and str(e.get("role") or "").lower() != role:
            continue
        if member and member not in str(e.get("member") or "").lower():
            continue
        if grep and grep not in str(e.get("message") or "").lower():
            continue
        out.append(e)
    return out


def _entry_text(entry: dict) -> Text:
    """One team-log entry as a colorized line (F151: shared by the block renderer
    and the `--watch` tail)."""
    role = str(entry.get("role") or "system")
    member = str(entry.get("member") or "")
    kind = str(entry.get("kind") or "")
    message = str(entry.get("message") or "")
    line = Text()
    line.append(f"{ts(entry.get('at')):>8} ", style="cli.muted")
    who = role if not member else f"{role}:{member}"
    line.append(f"{who:<16} ", style=role_style(role))
    if kind:
        line.append(f"[{kind}] ", style="cli.muted")
    line.append(message)
    return line


def filtered_entries(payload: Any) -> list:
    """The entries a --watch tail should consider: raw entries after the
    ``--role/--member/--grep`` filter (F151)."""
    entries = (payload or {}).get("entries") or []
    filters = (payload or {}).get("_filters") or {}
    return _filter(entries, filters) if filters else list(entries)


def render_entries(entries: list) -> list[str]:
    """Render each entry to its own line-string (F151 tail: append one at a time)."""
    return [render(_entry_text(e)) for e in entries]


def render_log(payload: Any, verbosity: Any) -> str:
    entries = filtered_entries(payload)
    if not entries:
        return render(muted("(team log empty)"))
    return render(*[_entry_text(e) for e in entries])
