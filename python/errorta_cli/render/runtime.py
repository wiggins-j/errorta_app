"""Runtime view (``GET /runtime/profiles`` + optional ``.../sessions/{sid}``).

Profile: ``{profile_id, kind, runtime_mode, working_dir, setup, start, stop,
health, ports, sandbox, ...}`` (``runtime.RuntimeProfile.to_dict``). Read-only —
runtime control (setup/start/stop) is S7.
"""
from __future__ import annotations

from typing import Any

from rich.table import Table

from . import heading, muted, render, truncate


def _argv(value: Any) -> str:
    if isinstance(value, list):
        if value and isinstance(value[0], list):  # list[argv]
            return "; ".join(" ".join(str(x) for x in step) for step in value)
        return " ".join(str(x) for x in value)
    return truncate(value, 60)


def render_runtime(payload: Any, verbosity: Any) -> str:
    profiles = (payload or {}).get("profiles") or []
    session = (payload or {}).get("session")
    parts = [heading("Runtime profiles")]
    if not profiles:
        parts.append(muted("(no runtime profiles — /runtime detect proposes some)"))
    else:
        table = Table(show_edge=False, pad_edge=False, box=None)
        table.add_column("profile", style="cli.key", no_wrap=True)
        table.add_column("kind", no_wrap=True)
        table.add_column("mode", no_wrap=True)
        table.add_column("start")
        table.add_column("sandbox", style="cli.muted", no_wrap=True)
        for p in profiles:
            table.add_row(
                str(p.get("profile_id") or ""),
                str(p.get("kind") or ""),
                str(p.get("runtime_mode") or ""),
                truncate(_argv(p.get("start")), 48),
                str(p.get("sandbox") or ""),
            )
        parts.append(table)

    if isinstance(session, dict) and session:
        parts.append(heading("session"))
        stable = Table(show_edge=False, pad_edge=False, box=None, show_header=False)
        stable.add_column("k", style="cli.key", no_wrap=True)
        stable.add_column("v")
        # Real ``RuntimeSession.to_dict`` fields (runtime.py): state/pgid/
        # allocated_ports (NOT status/pid/port/url). Join the port list.
        for key in ("session_id", "profile_id", "state", "pgid", "allocated_ports",
                    "sandbox_backend", "started_at", "ended_at", "exit_code", "error"):
            if key not in session:
                continue
            value = session.get(key)
            if value in (None, "", []):
                continue
            if key == "allocated_ports" and isinstance(value, list):
                value = ", ".join(str(p) for p in value)
            stable.add_row(key, str(value))
        parts.append(stable)
    return render(*parts)
