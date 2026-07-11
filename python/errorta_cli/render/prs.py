"""PR views (``GET /prs`` → ``prs``; ``pr <id>`` also ``GET /worktree`` → diff).

PR: ``{pr_id, task_id, branch, base, dev_member, head, status, reviewed_head,
reviewer_approved, pm_reviewed_head, pm_reviewer_approved, tested_head,
tests_passed, conflicts, superseded_by_pr_id, created_at, updated_at}``.

The worktree preview carries a unified ``diff`` string + structured ``file_diffs``
(``{path, changeType, addedLines, removedLines, hunks:[{header, lines}]}``) + a
merge ``gate``. The diff is rendered through the user's pager/``delta`` when present
(handled by the command layer); this module renders the metadata + a plain diff.
"""
from __future__ import annotations

from typing import Any

from rich.table import Table
from rich.text import Text

from . import heading, muted, render, truncate

_STATUS_STYLE = {
    "open": "cli.warn",
    "mergeable": "cli.ok",
    "merged": "cli.ok",
    "superseded": "cli.muted",
    "conflict": "cli.bad",
}


def _mark(value: Any) -> Text:
    if value is True:
        return Text("✓", style="cli.ok")
    if value is False:
        return Text("✗", style="cli.bad")
    return Text("·", style="cli.muted")


def render_prs(payload: Any, verbosity: Any) -> str:
    prs = (payload or {}).get("prs") or []
    if not prs:
        return render(muted("(no pull requests)"))
    table = Table(show_edge=False, pad_edge=False, box=None)
    table.add_column("pr", style="cli.key", no_wrap=True)
    table.add_column("status", no_wrap=True)
    table.add_column("branch", no_wrap=True)
    table.add_column("rev", justify="center", no_wrap=True)
    table.add_column("test", justify="center", no_wrap=True)
    table.add_column("task", style="cli.muted", no_wrap=True)
    for pr in prs:
        status = str(pr.get("status") or "")
        table.add_row(
            str(pr.get("pr_id") or ""),
            Text(status, style=_STATUS_STYLE.get(status, "white")),
            truncate(pr.get("branch"), 32),
            _mark(pr.get("reviewer_approved")),
            _mark(pr.get("tests_passed")),
            str(pr.get("task_id") or ""),
        )
    return render(table)


def render_pr_detail(payload: Any, verbosity: Any) -> str:
    """``pr <id>`` — the matched PR's fields + gate summary (diff handled separately)."""
    pr = (payload or {}).get("pr")
    if not pr:
        return render(muted("(pull request not found)"))
    gate = (payload or {}).get("gate") or {}
    parts = [heading(f"PR {pr.get('pr_id') or ''}")]
    table = Table(show_edge=False, pad_edge=False, box=None, show_header=False)
    table.add_column("k", style="cli.key", no_wrap=True)
    table.add_column("v")
    for key in (
        "status", "branch", "base", "head", "task_id", "dev_member",
        "reviewer_approved", "tests_passed", "conflicts", "superseded_by_pr_id",
    ):
        if key in pr:
            table.add_row(key, str(pr.get(key)))
    parts.append(table)
    if gate:
        allowed = gate.get("allowed")
        parts.append(
            Text(
                f"merge gate: {'allowed' if allowed else 'blocked'}",
                style="cli.ok" if allowed else "cli.bad",
            )
        )
        for b in gate.get("blockers") or []:
            parts.append(muted(f"  - {b.get('code')}: {truncate(b.get('detail'), 90)}"))
    out = render(*parts)

    diff = (payload or {}).get("diff")
    if isinstance(diff, str) and diff.strip():
        from .. import pager

        delta_out = pager.format_diff(diff)
        diff_block = delta_out if delta_out is not None else render_diff(diff)
        out = f"{out}\n{render(heading('diff'))}\n{diff_block}"
    return out


def unified_diff_text(worktree: Any) -> str:
    """Extract the raw unified diff string from a ``/worktree`` payload (for pager/delta)."""
    if not isinstance(worktree, dict):
        return ""
    diff = worktree.get("diff")
    return diff if isinstance(diff, str) else ""


def render_diff(diff_text: str) -> str:
    """Colorize a plain unified diff (fallback when no pager/delta is available)."""
    if not diff_text.strip():
        return render(muted("(empty diff)"))
    lines: list[Text] = []
    for raw in diff_text.splitlines():
        if raw.startswith("+") and not raw.startswith("+++"):
            style = "cli.ok"
        elif raw.startswith("-") and not raw.startswith("---"):
            style = "cli.bad"
        elif raw.startswith("@@"):
            style = "cli.key"
        elif raw.startswith(("diff ", "index ", "+++", "---")):
            style = "cli.head"
        else:
            style = None
        lines.append(Text(raw, style=style) if style else Text(raw))
    return render(*lines)
