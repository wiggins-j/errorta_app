"""Trusted-gate view (``GET /trusted-gate`` -> tier/path/present/valid/code/
commands/env_passthrough). Read-only — the file lives at
``$ERRORTA_HOME/gates/<project_id>.yaml`` and only an operator may write it
(spec 2026-08-23-trusted-gate; see ``tests/test_gates_dir_is_operator_only.py``).
"""
from __future__ import annotations

from typing import Any

from rich.table import Table
from rich.text import Text

from . import heading, muted, render, truncate


def _kv(key: str, value: Any) -> Text:
    t = Text()
    t.append(f"{key}: ", style="cli.key")
    t.append(str(value))
    return t


def render_trusted_gate(payload: Any, verbosity: Any) -> str:
    payload = payload or {}
    tier = str(payload.get("tier") or "none")
    path = str(payload.get("path") or "")
    parts = [heading("Trusted gate"), _kv("tier", tier), _kv("path", path)]

    if not payload.get("present"):
        parts.append(muted("none — no operator-declared gate file"))
        return render(*parts)

    if not payload.get("valid"):
        parts.append(muted(f"invalid: {payload.get('code') or 'unknown'}"))
        return render(*parts)

    commands = payload.get("commands") or []
    if not commands:
        parts.append(muted("(no commands)"))
    else:
        table = Table(show_edge=False, pad_edge=False, box=None)
        table.add_column("id", style="cli.key", no_wrap=True)
        table.add_column("argv")
        table.add_column("scope", no_wrap=True)
        table.add_column("timeout", no_wrap=True)
        for c in commands:
            if not isinstance(c, dict):
                continue
            argv = " ".join(str(a) for a in (c.get("argv") or []))
            table.add_row(str(c.get("id") or ""), truncate(argv, 60),
                          str(c.get("scope") or ""), str(c.get("timeout_seconds") or ""))
        parts.append(table)

    env = payload.get("env_passthrough") or []
    if env:
        parts.append(_kv("env_passthrough", ", ".join(str(e) for e in env)))

    return render(*parts)
