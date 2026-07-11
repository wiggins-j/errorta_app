"""Diff formatting through the user's ``delta``/pager when available (spec §9).

``format_diff`` renders a unified diff through ``delta`` (captured, no paging) when
it's on PATH; otherwise the caller falls back to the CLI's own colorized diff. This
is a pure helper — it never touches the network and never spawns a pager that would
steal the terminal in a captured/piped context.
"""
from __future__ import annotations

import shutil
import subprocess


def format_diff(diff_text: str) -> str | None:
    """Return ``delta``-formatted diff text, or ``None`` if delta is unavailable.

    ``--paging=never`` so we capture the output instead of handing the terminal to
    a pager (the interactive front-end owns paging decisions).
    """
    if not diff_text.strip():
        return None
    delta = shutil.which("delta")
    if not delta:
        return None
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
            [delta, "--paging=never", "--color=always"],
            input=diff_text,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode == 0 and proc.stdout:
        return proc.stdout.rstrip("\n")
    return None
