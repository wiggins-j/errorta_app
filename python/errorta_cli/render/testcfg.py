"""Test-command / test-settings / test-run views (F147 §8).

Distinct from the ``runtime test`` action: these are the registered project TEST
COMMANDS (the merge-gate suite), their sandbox setting, and the recorded runs.
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


def _cmd_argv(value: Any) -> str:
    if isinstance(value, list):
        return " ".join(str(x) for x in value)
    return str(value or "")


def render_test_commands(payload: Any) -> str:
    commands = (payload or {}).get("commands") or []
    if not commands:
        return render(heading("Test commands"), muted("(no test commands configured)"))
    table = Table(show_edge=False, pad_edge=False, box=None)
    table.add_column("label", style="cli.key", no_wrap=True)
    table.add_column("command")
    for c in commands:
        if isinstance(c, dict):
            table.add_row(str(c.get("label") or c.get("id") or ""),
                          truncate(_cmd_argv(c.get("command") or c.get("argv")), 70))
        else:
            table.add_row("", truncate(_cmd_argv(c), 70))
    return render(heading("Test commands"), table)


def render_test_settings(payload: Any) -> str:
    require = bool((payload or {}).get("require_sandbox"))
    return render(heading("Test settings"),
                  _kv("require_sandbox", "yes" if require else "no"))


def render_test_runs(payload: Any) -> str:
    runs = (payload or {}).get("runs") or []
    if not runs:
        return render(heading("Test runs"), muted("(no recorded test runs)"))
    table = Table(show_edge=False, pad_edge=False, box=None)
    table.add_column("label", style="cli.key", no_wrap=True)
    table.add_column("passed", no_wrap=True)
    table.add_column("head", style="cli.muted", no_wrap=True)
    for r in runs:
        passed = r.get("passed")
        table.add_row(str(r.get("label") or r.get("command") or ""),
                      "yes" if passed else ("no" if passed is not None else "?"),
                      str(r.get("head") or "")[:12])
    return render(heading("Test runs"), table)
